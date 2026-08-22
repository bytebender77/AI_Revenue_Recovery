"""feature_cross_v1 vs v2. Same exploration data, same cohort, same random numbers.

Both models are fit on the identical observation set -- v1 simply has no level that
references dom, hour or issuer_rate. The only thing that differs is what the model
is allowed to condition on.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

from rr.agent.features import IssuerFailureIndex
from rr.agent.policy import EVPolicy
from rr.baselines.outage import B25GoodRulesPlusOutage, OutageDetector
from rr.budget import EscalationBudget
from rr.config import COSTS
from rr.eval.agent_run import run_agent
from rr.eval.metrics import (
    contact_waste, delta_vector, gap_by_cause_pair, make_resamples, paired_gap_ci,
    reliability, rupees, summarize,
)
from rr.model.beta_binomial import BetaBinomialModel
from rr.model.train import in_fit_split
from rr.sim.cohort import load_latent, load_observed
from rr.sim.latent import LatentState
from rr.sim.world import run_arm
from rr.taxonomy import FailureCause

WATCHED = ("mandate_invalid", "limit_exceeded", "upi_collect_expired", "invalid_card_details")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=pathlib.Path, default=pathlib.Path("data"))
    ap.add_argument("--models", type=pathlib.Path, default=pathlib.Path("models"))
    ap.add_argument("--cohort", default="dev")
    args = ap.parse_args()
    if args.cohort != "dev":
        print("REFUSED: ablations run on dev.", file=sys.stderr); sys.exit(2)

    obs = load_observed(args.data / "dev_observed.jsonl")
    lat = [LatentState(**{**d, "true_cause": FailureCause(d["true_cause"])})
           for d in load_latent(args.data / "dev_latent.jsonl")]
    by_id = {o["intent_id"]: o for o in obs}
    n = len(obs)
    idx = make_resamples(n, reps=2000)
    issuer_index = IssuerFailureIndex.build(obs)

    # ONE detector shared across the arm -- it is a fleet-level signal. Building it
    # per intent silently degenerates B2.5 into B2.
    det = OutageDetector()
    b25_budget = EscalationBudget.for_cohort(n, COSTS.escalation_capacity_pct)
    b25 = run_arm(obs, lat, lambda o, l: B25GoodRulesPlusOutage(det, b25_budget))
    assert det._suspected_until, "outage detector never fired -- B2.5 has degenerated to B2"
    organic = BetaBinomialModel.load(args.models / "organic_model_v2.json")

    runs, models = {}, {}
    for v in ("v1", "v2"):
        m = BetaBinomialModel.load(args.models / f"success_model_{v}.json")
        models[v] = m
        runs[v], _ = run_agent(obs, lat, EVPolicy(m, organic, issuer_index=issuer_index))

    print(f"\n=== feature cross ablation -- dev, n={n} ===\n")
    print("  v1: action x cause x method x attempt_index x time_bucket x regime")
    print("  v2: v1 + day_of_month + hour_of_day + issuer_recent_failure_rate")
    print(f"  both fit on the same {137402} exploration observations "
          f"(20 replicates, 50% hash split of dev)\n")
    print("  v2 refines v1: sparse refined cells fall through to v1's exact chain.\n")

    hdr = f"{'':<10}{'incremental':>13}{'vs B2.5':>13}{'95% CI':>25}{'Brier':>9}{'debits':>8}{'contacts':>9}{'NR!':>6}{'net':>12}"
    print(hdr); print("-" * len(hdr))
    b25s = summarize("B2.5", b25, idx)
    print(f"{'B2.5':<10}{rupees(b25s.incremental_minor):>13}{'--':>13}{'--':>25}"
          f"{'--':>9}{b25s.debits:>8}{b25s.contacts:>9}{b25s.never_retry_true:>6}"
          f"{rupees(b25s.net_minor):>12}")
    rel = {}
    for v in ("v1", "v2"):
        s = summarize(v, runs[v], idx)
        d, lo, hi = paired_gap_ci(runs[v], b25, idx)
        table, brier = reliability(models[v], runs[v], by_id, issuer_index=issuer_index)
        rel[v] = (table, brier)
        print(f"{'agent ' + v:<10}{rupees(s.incremental_minor):>13}{rupees(d):>13}"
              f"{f'[{rupees(lo)}, {rupees(hi)}]':>25}{brier:>9.4f}{s.debits:>8}"
              f"{s.contacts:>9}{s.never_retry_true:>6}{rupees(s.net_minor):>12}")

    d, lo, hi = paired_gap_ci(runs["v2"], runs["v1"], idx)
    verdict = "BEATS" if lo > 0 else ("LOSES TO" if hi < 0 else "ties with")
    print(f"\n  v2 {verdict} v1, paired: INR {rupees(d)}  95% CI [{rupees(lo)}, {rupees(hi)}]")

    print("\n=== the [0.7,0.8) reliability bin (v1's worst: predicted 0.749, observed 0.130) ===\n")
    print(f"  {'':<6}{'n':>7}{'predicted':>12}{'observed':>11}{'error':>9}")
    for v in ("v1", "v2"):
        row = next((r for r in rel[v][0] if abs(r[0] - 0.7) < 1e-9), None)
        if row is None:
            print(f"  {v:<6}{'--':>7}{'--':>12}{'--':>11}   bin not populated")
        else:
            _, _, cnt, pred, ob = row
            print(f"  {v:<6}{cnt:>7}{pred:>12.3f}{ob:>11.3f}{pred - ob:>9.3f}")

    print("\n=== the four per-cause losses from M4 (agent minus B2.5) ===\n")
    gaps = {v: {c.value: g for c, _, _, _, g in gap_by_cause_pair(runs[v], b25)}
            for v in ("v1", "v2")}
    print(f"  {'cause':<24}{'v1':>12}{'v2':>12}{'change':>12}")
    for c in WATCHED:
        g1, g2 = gaps["v1"].get(c, 0), gaps["v2"].get(c, 0)
        print(f"  {c:<24}{rupees(g1):>12}{rupees(g2):>12}{rupees(g2 - g1):>12}")
    print(f"\n  {'all other causes':<24}"
          f"{rupees(sum(v for k, v in gaps['v1'].items() if k not in WATCHED)):>12}"
          f"{rupees(sum(v for k, v in gaps['v2'].items() if k not in WATCHED)):>12}")

    print("\n=== leakage check (model fit on a 50% hash split of dev) ===\n")
    held = [i for i, o in enumerate(obs) if not in_fit_split(o["intent_id"])]
    fitted = [i for i in range(n) if i not in set(held)]
    db = delta_vector(b25)
    for v in ("v1", "v2"):
        dv = delta_vector(runs[v]) - db
        print(f"  agent {v} - B2.5   fitted half INR {rupees(int(dv[fitted].sum())):>10}   "
              f"unfitted half INR {rupees(int(dv[held].sum())):>10}")
    print()


if __name__ == "__main__":
    main()
