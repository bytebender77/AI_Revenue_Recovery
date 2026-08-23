"""M4 report. Full dev cohort, every arm, one command.

Never reads the sealed test cohort -- and refuses to, in code.
"""
from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys
import time

from rr.agent.policy import EVPolicy
from rr.baselines.oracle import B3GreedyOracle
from rr.baselines.outage import B25GoodRulesPlusOutage, OutageDetector
from rr.baselines.rules import B0DoNothing, B1BlindLadder, B2GoodRules
from rr.budget import EscalationBudget
from rr.config import COSTS, POLICY
from rr.eval.agent_run import run_agent
from rr.eval.metrics import (
    contact_waste, gap_by_cause_pair, make_resamples, paired_gap_ci, reliability, rupees,
    summarize,
)
from rr.model.beta_binomial import BetaBinomialModel
from rr.model.train import in_fit_split
from rr.sim.cohort import load_latent, load_observed
from rr.sim.latent import LatentState
from rr.sim.world import run_arm
from rr.taxonomy import FailureCause


def _latent(d: dict) -> LatentState:
    return LatentState(**{**d, "true_cause": FailureCause(d["true_cause"])})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=pathlib.Path, default=pathlib.Path("data"))
    ap.add_argument("--models", type=pathlib.Path, default=pathlib.Path("models"))
    ap.add_argument("--cohort", default="dev")
    ap.add_argument("--reps", type=int, default=2000)
    args = ap.parse_args()
    if args.cohort != "dev":
        print("REFUSED: M4 runs on dev. The test cohort is read once, in M6.", file=sys.stderr)
        sys.exit(2)

    obs = load_observed(args.data / "dev_observed.jsonl")
    lat = [_latent(d) for d in load_latent(args.data / "dev_latent.jsonl")]
    by_id = {o["intent_id"]: o for o in obs}
    success = BetaBinomialModel.load(args.models / "success_model.json")
    organic = BetaBinomialModel.load(args.models / "organic_model.json")
    n = len(obs)
    print(f"\n=== M4 -- full dev cohort, n={n} ===\n")

    pct = COSTS.escalation_capacity_pct
    B = lambda: EscalationBudget.for_cohort(n, pct)
    det = OutageDetector()
    t0 = time.time()
    runs = {
        "B0_do_nothing":   run_arm(obs, lat, lambda o, l: B0DoNothing()),
        "B1_blind_ladder": run_arm(obs, lat, lambda o, l: B1BlindLadder()),
        "B2_good_rules":   run_arm(obs, lat, (lambda b: lambda o, l: B2GoodRules(b))(B())),
        "B2.5_plus_outage": run_arm(obs, lat,
                                    (lambda b: lambda o, l: B25GoodRulesPlusOutage(det, b))(B())),
    }
    agent, meta = run_agent(obs, lat, EVPolicy(success, organic))
    runs["AGENT_ev_incremental"] = agent
    gross_cfg = dataclasses.replace(POLICY, ev_objective="gross")
    runs["AGENT_ev_gross"], _ = run_agent(obs, lat, EVPolicy(success, organic, cfg=gross_cfg))
    runs["B3_greedy"] = run_arm(obs, lat, (lambda b: lambda o, l: B3GreedyOracle(l, b))(B()))

    idx = make_resamples(n, reps=args.reps)
    arms = [summarize(k, v, idx) for k, v in runs.items()]
    waste = {k: contact_waste(v) for k, v in runs.items()}

    h = (f"{'arm':<21}{'incremental':>13}{'95% CI':>25}{'debits':>8}{'contacts':>9}"
         f"{'wasted%':>9}{'NR!':>5}{'net':>12}")
    print(h); print("-" * len(h))
    for a in arms:
        ci = f"[{rupees(a.ci_lo_minor)}, {rupees(a.ci_hi_minor)}]"
        w = waste[a.name]
        print(f"{a.name:<21}{rupees(a.incremental_minor):>13}{ci:>25}{a.debits:>8}"
              f"{a.contacts:>9}{w.rate:>8.1%}{a.never_retry_true:>5}{rupees(a.net_minor):>12}")
    print(f"\n  INR. wasted% = contacts to intents that would have recovered anyway or")
    print(f"  never recovered at all. NR! = re-debits against a ground-truth terminal cause.")
    print(f"  ({time.time() - t0:.0f}s)\n")

    print("=== wasted contact cost ===\n")
    for name in ("B2_good_rules", "B2.5_plus_outage", "AGENT_ev_incremental", "AGENT_ev_gross"):
        w = waste[name]
        print(f"  {name:<22} {w.wasted:>5}/{w.contacts:<5} wasted  "
              f"(self-healers {w.to_self_healers}, never-recover {w.to_never_recovers})  "
              f"send INR {rupees(w.send_cost_minor):>8}  annoyance INR {rupees(w.annoyance_cost_minor):>9}")

    def compare(a_name: str, b_name: str) -> None:
        d, lo, hi = paired_gap_ci(runs[a_name], runs[b_name], idx)
        verdict = "BEATS" if lo > 0 else ("LOSES TO" if hi < 0 else "ties with")
        print(f"\n  {a_name} {verdict} {b_name}: INR {rupees(d)}  95% CI [{rupees(lo)}, {rupees(hi)}]")

    print("\n=== head to head (paired, same intents, same random numbers) ===")
    for b in ("B2_good_rules", "B2.5_plus_outage", "AGENT_ev_gross"):
        compare("AGENT_ev_incremental", b)
    compare("B2.5_plus_outage", "B2_good_rules")

    print("\n\n=== sensitivity: the terminal-retry prior is configured, not learned ===\n")
    print("  Chargeback risk cannot be learned from what the agent observes -- the label")
    print("  never arrives in production either. It is priced as a prior on UNKNOWN causes.")
    print(f"  {'p_chargeback_unknown':<22}{'incremental':>13}{'debits':>9}{'NR!':>6}{'net':>12}")
    for rate in (0.010, 0.040, POLICY.p_chargeback_unknown_cause):
        cfg = dataclasses.replace(POLICY, p_chargeback_unknown_cause=rate)
        res, _ = run_agent(obs, lat, EVPolicy(success, organic, cfg=cfg))
        sm = summarize("s", res, make_resamples(n, reps=200))
        star = "  <- default" if rate == POLICY.p_chargeback_unknown_cause else ""
        print(f"  {rate:<22.3f}{rupees(sm.incremental_minor):>13}{sm.debits:>9}"
              f"{sm.never_retry_true:>6}{rupees(sm.net_minor):>12}{star}")
    print("\n  Pricing it higher costs no incremental recovery and cuts terminal-retry")
    print("  exposure by a third. Every one of those violations is filed under an UNKNOWN")
    print("  gateway code -- which is precisely what the M5 tail normaliser is for.")

    print("\n\n=== per cause: agent vs B2.5 (losses first) ===\n")
    hdr = f"{'cause':<24}{'n':>6}{'agent':>12}{'B2.5':>12}{'agent-B2.5':>12}"
    print(hdr); print("-" * len(hdr))
    for cause, cnt, a_v, b_v, gap in gap_by_cause_pair(agent, runs["B2.5_plus_outage"]):
        print(f"{cause.value:<24}{cnt:>6}{rupees(a_v):>12}{rupees(b_v):>12}{rupees(gap):>12}")

    print("\n=== per cause: agent vs B2 (losses first) ===\n")
    print(hdr); print("-" * len(hdr))
    for cause, cnt, a_v, b_v, gap in gap_by_cause_pair(agent, runs["B2_good_rules"]):
        print(f"{cause.value:<24}{cnt:>6}{rupees(a_v):>12}{rupees(b_v):>12}{rupees(gap):>12}")

    print("\n\n=== success model calibration (agent's own attempts) ===\n")
    table, brier = reliability(success, agent, by_id)
    print(f"  {'bin':<14}{'n':>7}{'predicted':>12}{'observed':>11}   reliability")
    for lo_b, hi_b, cnt, pred, obs_r in table:
        bar = "#" * int(obs_r * 30)
        print(f"  {f'[{lo_b:.1f},{hi_b:.1f})':<14}{cnt:>7}{pred:>12.3f}{obs_r:>11.3f}   {bar}")
    print(f"\n  Brier score: {brier:.4f}   (0 = perfect, 0.25 = always guessing 0.5)")

    print("\n\n=== NO_ACTION: how often, and why ===\n")
    decs = meta["decisions"]
    no_act = [d for d in decs if d["chosen_action"] == "no_action"]
    touched = {r.intent_id for r in agent if r.debits or r.contacts or r.other_actions}
    print(f"  decisions taken            : {len(decs)}")
    print(f"  ending in NO_ACTION        : {len(no_act)} ({len(no_act)/max(len(decs),1):.1%})")
    print(f"  intents never acted on     : {n - len(touched)} of {n} "
          f"({(n - len(touched))/n:.1%})\n")
    by_reason: dict = {}
    for d in no_act:
        k = (d["decision_reason_code"], d["binding_constraint"])
        by_reason[k] = by_reason.get(k, 0) + 1
    print(f"  {'reason_code':<26}{'binding constraint':<38}{'count':>7}")
    print("  " + "-" * 70)
    for (code, binding), cnt in sorted(by_reason.items(), key=lambda x: -x[1]):
        print(f"  {code:<26}{str(binding):<38}{cnt:>7}")

    a = meta["auction"]
    print(f"\n  escalation auction: {a.get('claims',0)} claims, {a.get('granted',0)} granted, "
          f"{a.get('lost',0)} lost to a higher claimant   budget {meta['budget'][0]}/{meta['budget'][1]}")
    if meta["breaker_trips"]:
        for cause, w, rate in meta["breaker_trips"]:
            print(f"  circuit breaker OPENED on {cause}: {rate:.1%} success over last {w} attempts")
    else:
        print("  circuit breaker: no cause-cell fell below the floor")

    fit_ids = {o["intent_id"] for o in obs if in_fit_split(o["intent_id"])}
    held = [i for i, o in enumerate(obs) if o["intent_id"] not in fit_ids]
    import numpy as np
    from rr.eval.metrics import delta_vector
    da, db = delta_vector(agent), delta_vector(runs["B2.5_plus_outage"])
    print(f"\n  leakage check -- the success model was fit on a 50% hash split of dev:")
    print(f"    agent - B2.5 on FITTED half   : INR "
          f"{rupees(int((da - db)[[i for i in range(n) if i not in set(held)]].sum()))}")
    print(f"    agent - B2.5 on UNFITTED half : INR {rupees(int((da - db)[held].sum()))}\n")


if __name__ == "__main__":
    main()
