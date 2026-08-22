"""B3 -- the ceiling. Reads ground truth. Not achievable, and not meant to be.

Two deliberate constraints so that B3 - B2 measures an INFORMATION advantage and
not a permission or budget advantage:
  * B3 obeys the same guardrails as B2 (NEVER_RETRY, no unmandated re-debit) and
    the same budget (3 debits, 1 nudge, 1 merchant alert, 1 escalation).
  * B3 is GREEDY over a time grid, not a full sequential optimum. It is therefore
    a LOWER bound on the true ceiling. The real headroom is at least this large.
"""
from __future__ import annotations

from typing import Optional

from rr.config import CLOCK, COSTS
from rr.contracts import ActionSpec, AttemptContext
from rr.sim.latent import LatentState
from rr.sim.response_model import p_recovery
from rr.sim.world import RunState
from rr.taxonomy import ActionType, Channel, Method, NEVER_RETRY, Regime

BASE_GRID = (0.5, 1, 2, 3, 4, 6, 8, 10, 12, 16, 20, 24, 36, 48, 60, 72, 84, 96, 108, 120, 132, 144, 156, 168)
MAX_DEBITS, MAX_NUDGES = 3, 1


def _cost_minor(action: ActionSpec) -> int:
    if action.type in (ActionType.RETRY_SAME, ActionType.RETRY_ALTERNATE_METHOD):
        return COSTS.debit_attempt_cost_minor
    if action.type is ActionType.NUDGE:
        return COSTS.contact_cost_minor[action.channel.value]
    if action.type is ActionType.MERCHANT_ALERT:
        return COSTS.merchant_alert_cost_minor
    if action.type is ActionType.ESCALATE_HUMAN:
        return COSTS.human_escalation_cost_minor
    return 0


class B3Oracle:
    name = "B3_oracle"

    def __init__(self, latent: LatentState):
        self.latent = latent

    def next_action(self, obs: dict, st: RunState) -> Optional[ActionSpec]:
        lat = self.latent
        # It recovers on its own. Acting adds gross revenue and ZERO incremental
        # revenue, while still costing money. The single largest thing the oracle
        # knows that no observable field reveals.
        if lat.self_heal_at_h is not None:
            return None

        regime = Regime(obs["regime"])
        t0, margin = st.failed_at_h, COSTS.default_margin_bps / 10_000
        t_end = t0 + CLOCK.recovery_horizon_hours
        earliest = (st.history[-1].at_h + 0.25) if st.history else t0 + 0.25

        debits_allowed = (
            st.retry_index < MAX_DEBITS
            and regime is Regime.MERCHANT_INITIATED
            and lat.true_cause not in NEVER_RETRY
        )
        consented = [Channel(c) for c in obs["consented_channels"]]

        candidates: list[ActionSpec] = []
        times = [t0 + g for g in BASE_GRID]
        if lat.outage_end_h is not None:
            times.append(lat.outage_end_h + 0.6)  # the oracle knows when the bank came back
        for t in times:
            if not (earliest <= t <= t_end):
                continue
            if debits_allowed:
                candidates.append(ActionSpec(ActionType.RETRY_SAME, t))
                if obs["has_alternate_instrument"]:
                    candidates.append(ActionSpec(ActionType.RETRY_ALTERNATE_METHOD, t))
            if st.nudges_sent < MAX_NUDGES:
                candidates.extend(ActionSpec(ActionType.NUDGE, t, channel=ch) for ch in consented)
            if not st.merchant_alerted:
                candidates.append(ActionSpec(ActionType.MERCHANT_ALERT, t))
            if not st.escalated:
                candidates.append(ActionSpec(ActionType.ESCALATE_HUMAN, t))

        best, best_ev = None, 0.0
        for a in candidates:
            ctx = AttemptContext(
                sim_time_h=a.at_h, elapsed_h=a.at_h - t0, retry_index=st.retry_index,
                nudges_sent=st.nudges_sent, amount_minor=obs["amount_minor"],
                method=Method(obs["method"]), regime=regime,
                has_alternate_instrument=obs["has_alternate_instrument"],
            )
            ev = p_recovery(a, lat, ctx) * obs["amount_minor"] * margin - _cost_minor(a)
            if ev > best_ev:
                best, best_ev = a, ev
        return best
