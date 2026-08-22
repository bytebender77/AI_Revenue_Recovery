"""Simulated executor and the per-intent runner.

Every arm -- B0 through B3 and, from M2, the agent -- runs through this one
function against the same cohort with the same common random numbers. The
uniform consumed at action slot k is keyed on (intent_id, "outcome", k), so two
arms whose k-th action differs in type or timing still draw the SAME number.
A difference between arms is therefore caused by the policy, not by RNG drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol

from rr import rng
from rr.config import CLOCK
from rr.contracts import ActionSpec, AttemptContext, AttemptOutcome, AttemptRecord
from rr.sim.latent import LatentState
from rr.sim.response_model import p_recovery, resolution_lag_hours
from rr.taxonomy import (
    DEBIT_ACTIONS, ActionType, Channel, FailureCause, Method, NEVER_RETRY, Regime,
    normalize_reason,
)

MAX_SLOTS = 8  # hard stop on runaway policies; no arm should ever reach it


@dataclass
class RunState:
    """Everything a policy may condition on. Observable only."""
    failed_at_h: float
    retry_index: int = 0
    nudges_sent: int = 0
    merchant_alerted: bool = False
    escalated: bool = False
    history: list[AttemptRecord] = field(default_factory=list)


class Policy(Protocol):
    name: str

    def next_action(self, obs: dict, st: RunState) -> Optional[ActionSpec]:
        """Return the next action, or None to stop. Called after each failure."""


@dataclass
class IntentResult:
    intent_id: str
    merchant_id: str
    amount_minor: int
    true_cause: FailureCause
    slice_tag: str
    regime: Regime
    recovered_at_h: Optional[float]
    attribution: Optional[str]           # "agent" | "organic" | None
    counterfactual_self_heals: bool      # would B0 have recovered this?
    debits: int
    contacts: int
    other_actions: int
    never_retry_violations_true: int
    never_retry_violations_observable: int
    unauthorized_debit_rejected: int
    records: list[AttemptRecord]

    @property
    def recovered_value_minor(self) -> int:
        return self.amount_minor if self.recovered_at_h is not None else 0

    @property
    def counterfactual_value_minor(self) -> int:
        """What B0 gets on this intent. The baseline every arm is measured against."""
        return self.amount_minor if self.counterfactual_self_heals else 0


def _ctx(obs: dict, st: RunState, t: float) -> AttemptContext:
    return AttemptContext(
        sim_time_h=t, elapsed_h=t - st.failed_at_h, retry_index=st.retry_index,
        nudges_sent=st.nudges_sent, amount_minor=obs["amount_minor"],
        method=Method(obs["method"]), regime=Regime(obs["regime"]),
        has_alternate_instrument=obs["has_alternate_instrument"],
    )


def apply_action(obs: dict, latent: LatentState, action: ActionSpec, st: RunState,
                 slot: int, crn_ns: str = "outcome") -> AttemptRecord:
    """Execute exactly one action against the world. Shared by the per-intent
    runner and the tick-based agent runner so the two cannot drift apart."""
    if action.type in DEBIT_ACTIONS and Regime(obs["regime"]) is Regime.CUSTOMER_INITIATED:
        return AttemptRecord(slot, action.type, action.channel, action.at_h, 0.0,
                             AttemptOutcome.UNAUTHORIZED_DEBIT_REJECTED)
    p = p_recovery(action, latent, _ctx(obs, st, action.at_h))
    hit = rng.u01(obs["intent_id"], crn_ns, slot) < p
    return AttemptRecord(slot, action.type, action.channel, action.at_h, p,
                         AttemptOutcome.SUCCESS if hit else AttemptOutcome.FAILED)


def advance_state(st: RunState, rec: AttemptRecord) -> None:
    if rec.action_type in DEBIT_ACTIONS:
        st.retry_index += 1
    elif rec.action_type is ActionType.NUDGE:
        st.nudges_sent += 1
    elif rec.action_type is ActionType.MERCHANT_ALERT:
        st.merchant_alerted = True
    elif rec.action_type is ActionType.ESCALATE_HUMAN:
        st.escalated = True


def settle(obs: dict, latent: LatentState, agent_recovery_at: Optional[float]):
    """min(agent success, organic self-heal), both bounded by the horizon."""
    t_end = obs["failed_at_h"] + CLOCK.recovery_horizon_hours
    c = []
    if agent_recovery_at is not None and agent_recovery_at <= t_end:
        c.append((agent_recovery_at, "agent"))
    if latent.self_heal_at_h is not None and latent.self_heal_at_h <= t_end:
        c.append((latent.self_heal_at_h, "organic"))
    return min(c) if c else (None, None)


def run_intent(obs: dict, latent: LatentState, policy: Policy,
               crn_ns: str = "outcome") -> IntentResult:
    t0 = obs["failed_at_h"]
    t_end = t0 + CLOCK.recovery_horizon_hours
    st = RunState(failed_at_h=t0)

    observable_cause = normalize_reason(obs["gateway_reason"])
    regime = Regime(obs["regime"])
    agent_recovery_at: Optional[float] = None
    debits = contacts = other = 0
    v_true = v_obs = v_unauth = 0

    for slot in range(MAX_SLOTS):
        action = policy.next_action(obs, st)
        if action is None or action.type is ActionType.NO_ACTION or action.at_h > t_end:
            break
        # The payment already recovered on its own before this action would fire.
        # Any executor worth the name re-reads payment state before debiting, so
        # the action is simply not taken. This is generous to the naive arms --
        # it is the conservative direction for the gate.
        if latent.self_heal_at_h is not None and latent.self_heal_at_h <= action.at_h:
            break

        is_debit = action.type in DEBIT_ACTIONS
        if is_debit:
            debits += 1
            if latent.true_cause in NEVER_RETRY:
                v_true += 1
            if observable_cause in NEVER_RETRY:
                v_obs += 1
        elif action.type is ActionType.NUDGE:
            contacts += 1
        else:
            other += 1

        rec = apply_action(obs, latent, action, st, slot, crn_ns)
        if rec.outcome is AttemptOutcome.UNAUTHORIZED_DEBIT_REJECTED:
            v_unauth += 1
        st.history.append(rec)
        if rec.outcome is AttemptOutcome.SUCCESS:
            agent_recovery_at = action.at_h + resolution_lag_hours(action, latent)
            break
        advance_state(st, rec)

    recovered_at, attribution = settle(obs, latent, agent_recovery_at)

    return IntentResult(
        intent_id=obs["intent_id"], merchant_id=obs["merchant_id"],
        amount_minor=obs["amount_minor"], true_cause=latent.true_cause,
        slice_tag=latent.slice_tag, regime=regime,
        recovered_at_h=recovered_at, attribution=attribution,
        counterfactual_self_heals=latent.self_heal_at_h is not None,
        debits=debits, contacts=contacts, other_actions=other,
        never_retry_violations_true=v_true, never_retry_violations_observable=v_obs,
        unauthorized_debit_rejected=v_unauth, records=st.history,
    )


def run_arm(obs_rows: list[dict], latents: list[LatentState], policy_factory,
            crn_ns: str = "outcome") -> list[IntentResult]:
    """policy_factory(obs, latent) -> Policy. Only the oracle uses the latent arg.

    Intents are PROCESSED in chronological order, so a shared capacity budget is
    consumed the way it would be in production -- earliest failure first -- but
    RETURNED in input order, so per-intent pairing across arms stays intact.
    """
    order = sorted(range(len(obs_rows)), key=lambda i: obs_rows[i]["failed_at_h"])
    out: list[Optional[IntentResult]] = [None] * len(obs_rows)
    for i in order:
        out[i] = run_intent(obs_rows[i], latents[i],
                            policy_factory(obs_rows[i], latents[i]), crn_ns)
    return out  # type: ignore[return-value]
