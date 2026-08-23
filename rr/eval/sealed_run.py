"""The sealed run. One shot, on the held-out test cohort.

The test cohort was generated in M1 with disjoint merchants and customers, a later
time window (sim days 21-30), and an outage pattern that never appears in dev. The
success model is fit on dev only. Nothing here has seen it.

Configuration is exactly docs/shipping-config.md: feature_cross_v1, normaliser OFF,
explainer ON (irrelevant to the numbers -- it cannot affect a decision), escalation
capacity 5%, breaker on (cause, attempt_index).

DISCIPLINE. `--cohort test` refuses to run twice: the first run writes a seal
receipt and any later attempt is rejected. A second run on sealed data is not a
sealed run, and the point of sealing is that you only get to be surprised once.
Develop and debug against `--cohort dev`, which is unrestricted.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import sys
import time

import numpy as np

from rr.agent.features import IssuerFailureIndex
from rr.agent.policy import EVPolicy
from rr.baselines.oracle import B3GreedyOracle
from rr.baselines.outage import B25GoodRulesPlusOutage, OutageDetector
from rr.baselines.rules import B0DoNothing, B1BlindLadder, B2GoodRules
from rr.budget import EscalationBudget
from rr.config import COSTS
from rr.eval.agent_run import run_agent
from rr.eval.metrics import (
    contact_waste, delta_vector, gap_by_cause_pair, make_resamples, paired_gap_ci,
    reliability, rupees, summarize,
)
from rr.model.beta_binomial import BetaBinomialModel
from rr.sim.cohort import UNMAPPED_VIEWS, load_latent, load_observed
from rr.sim.latent import LatentState
from rr.taxonomy import FailureCause, NEVER_RETRY, normalize_reason

UNMAPPED_CODES = {v[0] for v in UNMAPPED_VIEWS}
SEAL = pathlib.Path("data/SEALED_RUN_RECEIPT.json")


def signal_tier(o: dict, l: LatentState) -> str:
    """How much the gateway actually told us. Violations are only interpretable
    against this: a gate can enforce what the signal reveals and nothing more."""
    if l.slice_tag in ("masquerade_natural", "adv_soft_mask_terminal"):
        return "misleading"
    if normalize_reason(o["gateway_reason"]) is not FailureCause.UNKNOWN:
        return "faithful"
    if o["gateway_code"] in UNMAPPED_CODES:
        return "unmapped"
    if o["gateway_source"] == "NA":
        return "generic"
    return "reason_collides"          # do_not_honour under the generic reason


def run_arms(obs, lat, models: pathlib.Path):
    from rr.sim.world import run_arm
    n = len(obs)
    pct = COSTS.escalation_capacity_pct
    B = lambda: EscalationBudget.for_cohort(n, pct)
    det = OutageDetector()
    b25_budget = B()
    issuer_index = IssuerFailureIndex.build(obs)
    success = BetaBinomialModel.load(models / "success_model_v1.json")
    organic = BetaBinomialModel.load(models / "organic_model_v2.json")

    runs = {
        "B0_do_nothing": run_arm(obs, lat, lambda o, l: B0DoNothing()),
        "B1_blind_ladder": run_arm(obs, lat, lambda o, l: B1BlindLadder()),
        "B2_good_rules": run_arm(obs, lat, (lambda b: lambda o, l: B2GoodRules(b))(B())),
        "B2.5_plus_outage": run_arm(obs, lat,
                                    lambda o, l: B25GoodRulesPlusOutage(det, b25_budget)),
    }
    assert det._suspected_until, "outage detector never fired -- B2.5 degenerated to B2"
    agent, meta = run_agent(obs, lat, EVPolicy(success, organic, issuer_index=issuer_index))
    runs["AGENT"] = agent
    runs["B3_greedy"] = run_arm(obs, lat, (lambda b: lambda o, l: B3GreedyOracle(l, b))(B()))
    return runs, meta, success, issuer_index


def headline(runs, idx) -> dict:
    """The numbers the writeup is allowed to quote."""
    arms = {k: summarize(k, v, idx) for k, v in runs.items()}
    waste = {k: contact_waste(v) for k, v in runs.items()}
    oracle = arms["B3_greedy"].incremental_minor
    out = {}
    for k, a in arms.items():
        w = waste[k]
        d, lo, hi = paired_gap_ci(runs[k], runs["B0_do_nothing"], idx)
        out[k] = {
            "gross": a.gross_minor, "incremental": a.incremental_minor,
            "ci": [lo, hi], "net": a.net_minor, "debits": a.debits,
            "contacts": a.contacts, "wasted_contacts": w.wasted,
            "wasted_rate": w.rate, "wasted_cost": w.annoyance_cost_minor,
            "never_retry_true": a.never_retry_true,
            "never_retry_observable": a.never_retry_observable,
            "unauthorized": a.unauthorized_debits,
            "oracle_share": (a.incremental_minor / oracle) if oracle else 0.0,
        }
    return out


def print_report(tag, obs, lat, runs, meta, success, issuer_index, idx, out: dict):
    by_id = {o["intent_id"]: o for o in obs}
    n = len(obs)
    H = out["headline"]

    print(f"\n{'=' * 78}\n=== {tag.upper()} COHORT -- n={n} ===\n{'=' * 78}\n")
    hdr = (f"{'arm':<18}{'gross':>11}{'incremental':>13}{'95% CI':>24}"
           f"{'net':>11}{'debits':>8}{'contacts':>9}{'wasted%':>9}{'oracle%':>9}")
    print(hdr); print("-" * len(hdr))
    for k in ("B0_do_nothing", "B1_blind_ladder", "B2_good_rules", "B2.5_plus_outage",
              "AGENT", "B3_greedy"):
        h = H[k]
        ci = f"[{rupees(h['ci'][0])}, {rupees(h['ci'][1])}]"
        print(f"{k:<18}{rupees(h['gross']):>11}{rupees(h['incremental']):>13}{ci:>24}"
              f"{rupees(h['net']):>11}{h['debits']:>8}{h['contacts']:>9}"
              f"{h['wasted_rate']:>8.1%}{h['oracle_share']:>8.1%}")
    print("\n  INR. wasted% = contacts to intents that would have recovered anyway or")
    print("  never recovered. oracle% = share of B3_greedy's incremental captured.")
    print("  B3_greedy is a GREEDY oracle, so it is a lower bound on the true ceiling.")

    print("\n=== wasted-contact cost ===\n")
    for k in ("B2_good_rules", "B2.5_plus_outage", "AGENT"):
        h = H[k]
        print(f"  {k:<20}{h['wasted_contacts']:>6}/{h['contacts']:<6} wasted   "
              f"modelled annoyance INR {rupees(h['wasted_cost']):>10}")

    print("\n=== head to head (paired on intents, common random numbers) ===")
    for opp in ("B2_good_rules", "B2.5_plus_outage"):
        d, lo, hi = paired_gap_ci(runs["AGENT"], runs[opp], idx)
        verdict = "BEATS" if lo > 0 else ("LOSES TO" if hi < 0 else "ties with")
        print(f"\n  AGENT {verdict} {opp}: INR {rupees(d)}  95% CI [{rupees(lo)}, {rupees(hi)}]")
    d, lo, hi = paired_gap_ci(runs["B2.5_plus_outage"], runs["B2_good_rules"], idx)
    print(f"  B2.5 over B2: INR {rupees(d)}  95% CI [{rupees(lo)}, {rupees(hi)}]")

    print("\n\n=== per cause: AGENT vs B2.5 (losses first) ===\n")
    h2 = f"  {'cause':<24}{'n':>6}{'agent':>12}{'B2.5':>12}{'delta':>12}"
    print(h2); print("  " + "-" * (len(h2) - 2))
    for cause, cnt, a_v, b_v, gap in gap_by_cause_pair(runs["AGENT"], runs["B2.5_plus_outage"]):
        print(f"  {cause.value:<24}{cnt:>6}{rupees(a_v):>12}{rupees(b_v):>12}{rupees(gap):>12}")

    print("\n=== calibration on held-out attempts ===\n")
    table, brier = reliability(success, runs["AGENT"], by_id, issuer_index=issuer_index)
    print(f"  {'bin':<14}{'n':>7}{'predicted':>12}{'observed':>11}   reliability")
    for lo_b, hi_b, cnt, pred, obs_r in table:
        print(f"  {f'[{lo_b:.1f},{hi_b:.1f})':<14}{cnt:>7}{pred:>12.3f}{obs_r:>11.3f}"
              f"   {'#' * int(obs_r * 30)}")
    print(f"\n  Brier: {brier:.4f}   (0 perfect, 0.25 = always guessing 0.5)")
    out["brier"] = brier
    out["reliability"] = table

    print("\n=== NEVER_RETRY violations by signal-quality tier ===\n")
    tiers = {}
    for i, (o, l) in enumerate(zip(obs, lat)):
        t = signal_tier(o, l)
        r = runs["AGENT"][i]
        b = tiers.setdefault(t, [0, 0, 0])
        b[0] += 1
        b[1] += r.never_retry_violations_true
        b[2] += r.never_retry_violations_observable
    h3 = f"  {'tier':<18}{'intents':>9}{'true':>8}{'observable':>12}"
    print(h3); print("  " + "-" * (len(h3) - 2))
    for t, (cnt, tv, ov) in sorted(tiers.items(), key=lambda kv: -kv[1][1]):
        print(f"  {t:<18}{cnt:>9}{tv:>8}{ov:>12}")
    print(f"\n  observable MUST be 0 -- it is what the gate could see and act on.")
    print(f"  true>0 on degraded tiers is irreducible: the code hid the cause.")
    out["tiers"] = {t: {"intents": c, "true": tv, "observable": ov}
                    for t, (c, tv, ov) in tiers.items()}

    print(f"\n  unauthorized debits (all arms): "
          + ", ".join(f"{k}={H[k]['unauthorized']}" for k in H))

    print("\n=== NO_ACTION and binding constraints (AGENT) ===\n")
    decs = meta["decisions"]
    no_act = [d for d in decs if d["chosen_action"] == "no_action"]
    touched = {r.intent_id for r in runs["AGENT"] if r.debits or r.contacts or r.other_actions}
    print(f"  decisions taken        : {len(decs)}")
    print(f"  ending in NO_ACTION    : {len(no_act)} ({len(no_act)/max(len(decs),1):.1%})")
    print(f"  intents never acted on : {n - len(touched)} of {n} "
          f"({(n - len(touched))/n:.1%})\n")
    by_reason = collections.Counter(
        (d["decision_reason_code"], d["binding_constraint"]) for d in no_act)
    print(f"  {'reason_code':<34}{'binding constraint':<36}{'count':>7}")
    print("  " + "-" * 76)
    for (code, binding), cnt in by_reason.most_common():
        print(f"  {code:<34}{str(binding):<36}{cnt:>7}")
    out["no_action"] = {"decisions": len(decs), "no_action": len(no_act),
                        "untouched": n - len(touched),
                        "by_reason": {f"{c}|{b}": v for (c, b), v in by_reason.items()}}

    bound = collections.Counter(d["binding_constraint"] for d in decs if d["binding_constraint"])
    print(f"\n  binding constraint frequency across ALL decisions:")
    for rule, cnt in bound.most_common(10):
        print(f"    {rule:<40}{cnt:>7}")
    out["binding"] = dict(bound)
    a = meta["auction"]
    print(f"\n  escalation auction: {a.get('claims',0)} claims, {a.get('granted',0)} granted, "
          f"{a.get('lost',0)} lost   budget {meta['budget'][0]}/{meta['budget'][1]}")
    for cause, w, rate in meta["breaker_trips"]:
        print(f"  circuit breaker OPENED on {cause}: {rate:.1%} over last {w}")
    out["auction"] = a
    out["breaker_trips"] = [[c, w, r] for c, w, r in meta["breaker_trips"]]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", default="dev", choices=("dev", "test"))
    ap.add_argument("--data", type=pathlib.Path, default=pathlib.Path("data"))
    ap.add_argument("--models", type=pathlib.Path, default=pathlib.Path("models"))
    ap.add_argument("--out", type=pathlib.Path, default=None)
    ap.add_argument("--reps", type=int, default=2000)
    args = ap.parse_args()

    if args.cohort == "test" and SEAL.exists():
        print(f"REFUSED: the sealed cohort has already been run.\n"
              f"  {SEAL} exists -- see it for the timestamp and hashes.\n"
              f"  A second run on sealed data is not a sealed run. Use --cohort dev.",
              file=sys.stderr)
        sys.exit(2)

    obs = load_observed(args.data / f"{args.cohort}_observed.jsonl")
    lat = [LatentState(**{**d, "true_cause": FailureCause(d["true_cause"])})
           for d in load_latent(args.data / f"{args.cohort}_latent.jsonl")]
    idx = make_resamples(len(obs), reps=args.reps)

    t0 = time.time()
    runs, meta, success, issuer_index = run_arms(obs, lat, args.models)
    out = {"cohort": args.cohort, "n": len(obs), "headline": headline(runs, idx)}
    out = print_report(args.cohort, obs, lat, runs, meta, success, issuer_index, idx, out)
    print(f"\n  ({time.time() - t0:.0f}s)\n")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out, indent=1, default=str))
        print(f"  wrote {args.out}")

    if args.cohort == "test":
        digest = hashlib.sha256(
            (args.data / "test_observed.jsonl").read_bytes()).hexdigest()
        SEAL.write_text(json.dumps({
            "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "test_observed_sha256": digest, "n": len(obs),
            "note": "The sealed cohort has been read. Any further run is not sealed.",
        }, indent=1))
        print(f"  seal receipt written to {SEAL}")


if __name__ == "__main__":
    main()
