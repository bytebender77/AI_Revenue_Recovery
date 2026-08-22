"""M1 gate check. Dev cohort only -- the test cohort stays sealed until the final run.

Runs B0-B3 through the same executor on the same intents with common random
numbers, then asks one question: is there enough headroom between a competent
rules baseline and a perfectly-informed oracle for a learned policy to be worth
building? If not, the honest move is to say so, not to widen the gap.
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

from rr.baselines.oracle import B3Oracle
from rr.baselines.rules import B0DoNothing, B1BlindLadder, B2GoodRules
from rr.eval.metrics import (
    ArmSummary, gap_by_cause, make_resamples, paired_gap_ci, rupees, summarize,
)
from rr.sim.cohort import load_latent, load_observed
from rr.sim.latent import LatentState
from rr.sim.world import run_arm
from rr.taxonomy import FailureCause

# Gate thresholds. These are OUR bar for proceeding, not facts about the world.
MIN_CAPTURE_HEADROOM = 0.15   # B2 must leave >=15% of oracle incremental on the table
MAX_OUTAGE_CONCENTRATION = 0.60


def _latent(d: dict) -> LatentState:
    return LatentState(**{**d, "true_cause": FailureCause(d["true_cause"])})


def _arm_table(arms: list[ArmSummary]) -> None:
    h = (f"{'arm':<17}{'gross':>12}{'incremental':>13}{'95% CI':>25}"
         f"{'debits':>8}{'contacts':>9}{'NR!':>6}{'unauth':>8}{'net':>12}")
    print(h)
    print("-" * len(h))
    for a in arms:
        ci = f"[{rupees(a.ci_lo_minor)}, {rupees(a.ci_hi_minor)}]"
        print(f"{a.name:<17}{rupees(a.gross_minor):>12}{rupees(a.incremental_minor):>13}{ci:>25}"
              f"{a.debits:>8}{a.contacts:>9}{a.never_retry_true:>6}{a.unauthorized_debits:>8}"
              f"{rupees(a.net_minor):>12}")
    print("\n  all values in INR. NR! = re-debits issued against a cause that is")
    print("  terminal in ground truth. unauth = re-debits with no mandate behind them.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=pathlib.Path, default=pathlib.Path("data"))
    ap.add_argument("--cohort", default="dev")
    ap.add_argument("--reps", type=int, default=2000)
    args = ap.parse_args()

    if args.cohort != "dev":
        print("REFUSED: the gate runs on dev. The test cohort is read once, at the "
              "end, by the final eval run.", file=sys.stderr)
        sys.exit(2)

    obs = load_observed(args.data / f"{args.cohort}_observed.jsonl")
    lat = [_latent(d) for d in load_latent(args.data / f"{args.cohort}_latent.jsonl")]
    print(f"\n=== M1 GATE -- cohort '{args.cohort}', n={len(obs)} ===\n")

    t0 = time.time()
    runs = {
        "B0_do_nothing": run_arm(obs, lat, lambda o, l: B0DoNothing()),
        "B1_blind_ladder": run_arm(obs, lat, lambda o, l: B1BlindLadder()),
        "B2_good_rules": run_arm(obs, lat, lambda o, l: B2GoodRules()),
        "B3_oracle": run_arm(obs, lat, lambda o, l: B3Oracle(l)),
    }
    idx = make_resamples(len(obs), reps=args.reps)
    arms = [summarize(k, v, idx) for k, v in runs.items()]
    _arm_table(arms)
    print(f"\n  ({time.time() - t0:.1f}s, {args.reps} bootstrap resamples, paired on intents)\n")

    b2, b3 = runs["B2_good_rules"], runs["B3_oracle"]
    gap_total, gap_lo, gap_hi = paired_gap_ci(b3, b2, idx)
    s2 = next(a for a in arms if a.name == "B2_good_rules")
    s3 = next(a for a in arms if a.name == "B3_oracle")
    capture = s2.incremental_minor / s3.incremental_minor if s3.incremental_minor else 0.0

    print("=== B3 - B2 gap, by true failure cause ===\n")
    rows = gap_by_cause(b2, b3)
    hdr = f"{'cause':<24}{'n':>6}{'B2 incr':>12}{'B3 incr':>12}{'gap':>12}{'share':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        share = r.gap_minor / gap_total if gap_total else 0.0
        print(f"{r.cause.value:<24}{r.n:>6}{rupees(r.b2_incremental_minor):>12}"
              f"{rupees(r.b3_incremental_minor):>12}{rupees(r.gap_minor):>12}{share:>7.1%}")

    outage = next((r for r in rows if r.cause is FailureCause.ISSUER_DOWNTIME), None)
    outage_share = (outage.gap_minor / gap_total) if (outage and gap_total) else 0.0
    top3 = sum(r.gap_minor for r in rows[:3]) / gap_total if gap_total else 0.0

    gap_significant = gap_lo > 0
    headroom_ok = (1 - capture) >= MIN_CAPTURE_HEADROOM
    spread_ok = outage_share < MAX_OUTAGE_CONCENTRATION
    passed = gap_significant and headroom_ok and spread_ok

    print(f"\n=== VERDICT ===\n")
    print(f"  B3 - B2 incremental gap  : INR {rupees(gap_total)}  "
          f"95% CI [{rupees(gap_lo)}, {rupees(gap_hi)}]")
    print(f"  B2 captures              : {capture:.1%} of oracle incremental "
          f"({1 - capture:.1%} headroom, need >= {MIN_CAPTURE_HEADROOM:.0%})")
    print(f"  issuer_downtime share    : {outage_share:.1%} of the gap "
          f"(need < {MAX_OUTAGE_CONCENTRATION:.0%})")
    print(f"  top-3 cause concentration: {top3:.1%}")
    print(f"\n  gap statistically detectable : {'YES' if gap_significant else 'NO'}")
    print(f"  headroom worth building for  : {'YES' if headroom_ok else 'NO'}")
    print(f"  headroom spread across causes: {'YES' if spread_ok else 'NO'}")
    print(f"\n  GATE: {'PASS' if passed else 'FAIL'}\n")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
