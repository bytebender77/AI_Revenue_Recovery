"""Cohort composition and a probe of the hidden structure.

Two jobs. First, show what got generated. Second -- and this is the one that
matters before baselines exist -- show that the latent state carries decision-
relevant information that no observable field reveals. If the probe shows flat
curves, the oracle has nothing to exploit and the M1 gate cannot pass no matter
how the baselines are written.
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib

from rr.config import CLOCK
from rr.contracts import ActionSpec, AttemptContext
from rr.sim.cohort import GATEWAY_VIEW, UNMAPPED_VIEWS, load_latent, load_observed
from rr.sim.latent import LatentState
from rr.sim.response_model import p_recovery
from rr.taxonomy import ActionType, FailureCause, Method, Regime


def _latent(d: dict) -> LatentState:
    return LatentState(**{**d, "true_cause": FailureCause(d["true_cause"])})


def _ctx(o: dict, t: float) -> AttemptContext:
    return AttemptContext(
        sim_time_h=t, elapsed_h=t - o["failed_at_h"], retry_index=0, nudges_sent=0,
        amount_minor=o["amount_minor"], method=Method(o["method"]),
        regime=Regime(o["regime"]), has_alternate_instrument=o["has_alternate_instrument"],
    )


def _bar(label: str, n: int, total: int, width: int = 28) -> str:
    frac = n / total if total else 0
    return f"  {label:<26} {n:>6}  {frac:>6.1%}  {'#' * int(frac * width)}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=pathlib.Path, default=pathlib.Path("data"))
    ap.add_argument("--cohort", default="dev")
    args = ap.parse_args()

    obs = load_observed(args.data / f"{args.cohort}_observed.jsonl")
    lat = [_latent(d) for d in load_latent(args.data / f"{args.cohort}_latent.jsonl")]
    n = len(obs)
    print(f"\n=== cohort '{args.cohort}'  n={n} ===\n")

    print("regime")
    for k, v in collections.Counter(o["regime"] for o in obs).most_common():
        print(_bar(k, v, n))

    print("\ntrue cause")
    for k, v in collections.Counter(l.true_cause.value for l in lat).most_common():
        print(_bar(k, v, n))

    print("\nsignal quality (what the agent actually receives)")
    known = {v[0] for v in GATEWAY_VIEW.values()}
    unmapped_codes = {u[0] for u in UNMAPPED_VIEWS}
    buckets = collections.Counter()
    for o, l in zip(obs, lat):
        if l.slice_tag in ("masquerade_natural", "adv_soft_mask_terminal"):
            buckets["misleading (soft code, terminal truth)"] += 1
        elif o["gateway_code"] in unmapped_codes:
            buckets["unmapped bank code"] += 1
        elif o["gateway_code"] == "BAD_REQUEST_ERROR" and o["gateway_reason"] == "payment_failed" \
                and l.true_cause is not FailureCause.DO_NOT_HONOUR:
            buckets["generic, uninformative"] += 1
        else:
            buckets["faithful"] += 1
    for k, v in buckets.most_common():
        print(_bar(k, v, n))

    print("\nadversarial slices")
    for k, v in collections.Counter(l.slice_tag for l in lat).most_common():
        print(_bar(k, v, n))

    heal = sum(1 for l in lat if l.self_heal_at_h is not None)
    fast = sum(1 for o, l in zip(obs, lat)
               if l.self_heal_at_h is not None and l.self_heal_at_h - o["failed_at_h"] <= 6.0)
    print(f"\nself-healing (recovers with ZERO intervention, inside {CLOCK.recovery_horizon_hours:.0f}h)")
    print(_bar("self-heals", heal, n))
    print(_bar("  ...of which within 6h", fast, n))
    print("  ^ this is the share of 'recovered' revenue a do-everything policy")
    print("    would claim credit for and not deserve.\n")

    # ---------------- hidden-structure probe -------------------------------
    print("=== hidden-structure probe: is there anything for an oracle to exploit? ===\n")
    retry = lambda t: ActionSpec(ActionType.RETRY_SAME, t)

    def sweep(o, l, step=1.0):
        t0 = o["failed_at_h"]
        pts = []
        t = t0 + 1.0
        while t <= t0 + CLOCK.recovery_horizon_hours:
            pts.append((t, p_recovery(retry(t), l, _ctx(o, t))))
            t += step
        return pts

    def report(title, pred, fixed_offset=48.0, limit=400):
        rows = [(o, l) for o, l in zip(obs, lat) if pred(o, l)][:limit]
        if not rows:
            print(f"{title}: no rows\n")
            return
        best, fixed = [], []
        for o, l in rows:
            pts = sweep(o, l)
            best.append(max(p for _, p in pts))
            tf = o["failed_at_h"] + fixed_offset
            fixed.append(p_recovery(retry(tf), l, _ctx(o, tf)))
        b, f = sum(best) / len(best), sum(fixed) / len(fixed)
        print(f"{title}  (n={len(rows)})")
        print(f"  best achievable P(retry succeeds), timed by an oracle : {b:.3f}")
        print(f"  same retry at a fixed +{fixed_offset:.0f}h backoff            : {f:.3f}")
        print(f"  headroom the oracle can capture on timing alone       : {b - f:+.3f}\n")

    report("INSUFFICIENT_FUNDS -- salary-cycle timing",
           lambda o, l: l.true_cause is FailureCause.INSUFFICIENT_FUNDS
           and o["regime"] == Regime.MERCHANT_INITIATED.value)
    report("ISSUER_DOWNTIME -- outage-window timing",
           lambda o, l: l.true_cause is FailureCause.ISSUER_DOWNTIME
           and o["regime"] == Regime.MERCHANT_INITIATED.value, fixed_offset=1.0)

    dnh = [(o, l) for o, l in zip(obs, lat)
           if l.true_cause is FailureCause.DO_NOT_HONOUR
           and o["regime"] == Regime.MERCHANT_INITIATED.value]
    tol0 = [max(p for _, p in sweep(o, l)) for o, l in dnh if l.issuer_retry_tolerance == 0][:300]
    tol2 = [max(p for _, p in sweep(o, l)) for o, l in dnh if l.issuer_retry_tolerance >= 2][:300]
    if tol0 and tol2:
        print(f"DO_NOT_HONOUR -- issuer retry tolerance is invisible from outside")
        print(f"  tolerance==0 (never retryable)  n={len(tol0):<4} best P = {sum(tol0)/len(tol0):.3f}")
        print(f"  tolerance>=2 (retryable)        n={len(tol2):<4} best P = {sum(tol2)/len(tol2):.3f}")
        print(f"  identical gateway code in both cases.\n")

    wasted = [(o, l) for o, l in zip(obs, lat)
              if l.self_heal_at_h is not None and l.self_heal_at_h - o["failed_at_h"] <= 1.0]
    print(f"intents that recover on their own within 1h: {len(wasted)} "
          f"({len(wasted)/n:.1%}) -- every rupee a policy 'recovers' here is gross, not incremental.\n")


if __name__ == "__main__":
    main()
