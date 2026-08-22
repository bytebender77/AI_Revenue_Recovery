"""Fit the success model and the organic-recovery model on dev exploration data.

Two things keep this honest:

  * Exploration runs under a SEPARATE common-random-number namespace ("explore").
    The model therefore learns cell-level rates and cannot memorise the specific
    coin flip an intent will receive at evaluation time.
  * Fitting uses a deterministic 50% hash split of dev. The agent is evaluated on
    full dev as the milestone asks, and the report also prints its result on the
    unfitted half so the size of any in-sample advantage is visible rather than
    argued about.

The exploration policy respects the hard guardrails -- it never proposes an
unmandated re-debit or a re-debit against a terminal cause -- so the training set
contains no illegal actions. It deliberately ignores the escalation value floor
and capacity budget, because the model needs coverage of escalation across the
whole amount range, not only the tail B2 happens to escalate.
"""
from __future__ import annotations

import argparse
import pathlib
from typing import Optional

from rr import rng
from rr.baselines.rules import B0DoNothing, MAX_DEBITS, MAX_NUDGES, pick_channel
from rr.config import MODEL, POLICY
from rr.contracts import ActionSpec, AttemptOutcome
from rr.model.beta_binomial import BetaBinomialModel, time_bucket
from rr.sim.cohort import load_latent, load_observed
from rr.sim.latent import LatentState
from rr.sim.world import RunState, run_arm
from rr.taxonomy import (
    ActionType, Channel, DEBIT_ACTIONS, FailureCause, NEVER_RETRY, Regime, normalize_reason,
)

EXPLORE_NS = "explore"
MAX_EXPLORE_SLOTS = 3


class ExplorationPolicy:
    name = "explore"

    def next_action(self, obs: dict, st: RunState) -> Optional[ActionSpec]:
        slot = len(st.history)
        if slot >= MAX_EXPLORE_SLOTS:
            return None
        cause = normalize_reason(obs["gateway_reason"])
        regime = Regime(obs["regime"])
        legal: list[tuple[ActionType, Optional[Channel]]] = []

        if regime is Regime.MERCHANT_INITIATED and cause not in NEVER_RETRY \
                and st.retry_index < MAX_DEBITS:
            legal.append((ActionType.RETRY_SAME, None))
            if obs["has_alternate_instrument"]:
                legal.append((ActionType.RETRY_ALTERNATE_METHOD, None))
        if st.nudges_sent < MAX_NUDGES:
            legal += [(ActionType.NUDGE, Channel(c)) for c in obs["consented_channels"]]
        if not st.merchant_alerted:
            legal.append((ActionType.MERCHANT_ALERT, None))
        if not st.escalated:
            legal.append((ActionType.ESCALATE_HUMAN, None))
        if not legal:
            return None

        iid = obs["intent_id"]
        act, ch = legal[int(rng.u01(iid, "explore_action", slot) * len(legal))]
        grid = POLICY.candidate_grid_h
        at = obs["failed_at_h"] + grid[int(rng.u01(iid, "explore_time", slot) * len(grid))]
        return ActionSpec(act, at, ch)


def _action_label(action_type: str, channel: Optional[str]) -> str:
    return f"{action_type}:{channel}" if channel else action_type


def observations_from_run(results, obs_by_id: dict) -> list[dict]:
    """One row per executed attempt: the features visible at fire time, and the outcome."""
    rows = []
    for r in results:
        o = obs_by_id[r.intent_id]
        cause = normalize_reason(o["gateway_reason"]).value
        retry_index = 0
        for rec in r.records:
            if rec.outcome is AttemptOutcome.UNAUTHORIZED_DEBIT_REJECTED:
                continue
            rows.append({
                "action": _action_label(rec.action_type.value,
                                        rec.channel.value if rec.channel else None),
                "cause": cause, "method": o["method"], "attempt_index": retry_index,
                "time_bucket": time_bucket(rec.at_h - o["failed_at_h"]),
                "regime": o["regime"],
                "success": rec.outcome is AttemptOutcome.SUCCESS,
            })
            if rec.action_type in DEBIT_ACTIONS:
                retry_index += 1
    return rows


def organic_observations(results, obs_by_id: dict) -> list[dict]:
    """P(recovers with no intervention). Learned from the do-nothing arm, which is
    exactly what a production control arm gives you."""
    return [{"action": "organic", "cause": normalize_reason(
                obs_by_id[r.intent_id]["gateway_reason"]).value,
             "method": obs_by_id[r.intent_id]["method"], "attempt_index": 0,
             "time_bucket": "all", "regime": obs_by_id[r.intent_id]["regime"],
             "success": r.recovered_at_h is not None}
            for r in results]


def in_fit_split(intent_id: str) -> bool:
    return rng.u01(intent_id, "fitsplit") < MODEL.fit_split_fraction


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=pathlib.Path, default=pathlib.Path("data"))
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("models"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    obs = load_observed(args.data / "dev_observed.jsonl")
    lat = [LatentState(**{**d, "true_cause": FailureCause(d["true_cause"])})
           for d in load_latent(args.data / "dev_latent.jsonl")]
    pairs = [(o, l) for o, l in zip(obs, lat) if in_fit_split(o["intent_id"])]
    fit_obs = [o for o, _ in pairs]
    fit_lat = [l for _, l in pairs]
    by_id = {o["intent_id"]: o for o in fit_obs}
    print(f"  fit split: {len(fit_obs)} of {len(obs)} dev intents "
          f"({MODEL.fit_split_fraction:.0%}), CRN namespace '{EXPLORE_NS}'")

    explore = run_arm(fit_obs, fit_lat, lambda o, l: ExplorationPolicy(), crn_ns=EXPLORE_NS)
    rows = observations_from_run(explore, by_id)
    success = BetaBinomialModel().fit(rows)
    success.save(args.out / "success_model.json")

    control = run_arm(fit_obs, fit_lat, lambda o, l: B0DoNothing(), crn_ns=EXPLORE_NS)
    organic_rows = organic_observations(control, by_id)
    organic = BetaBinomialModel().fit(organic_rows)
    organic.save(args.out / "organic_model.json")

    print(f"  success model : {len(rows):>6} attempt observations  cells {success.summary()}")
    print(f"  organic model : {len(organic_rows):>6} intent observations  "
          f"base rate {sum(r['success'] for r in organic_rows)/len(organic_rows):.3f}")
    print(f"  written to {args.out}/")


if __name__ == "__main__":
    main()
