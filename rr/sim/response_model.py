"""RESPONSE MODEL v1.0.0 -- FROZEN.

P(recovery | action, latent_state, elapsed_time), per failure cause.

This file is written and committed BEFORE any policy code exists. That ordering is
the whole point: a simulator authored after the policy is a simulator the policy
wins on by construction. tests/test_response_model_frozen.py pins the sha256 of
this file, so any later edit shows up as a deliberate, reviewable change rather
than a quiet tuning pass. If you must change it: bump RESPONSE_MODEL_VERSION,
regenerate the hash with `make freeze`, and note why in docs/CHANGELOG-sim.md.

The numbers below are my judgement about how Indian subscription failures behave.
They are NOT measured, NOT cited, and NOT claimed to be accurate. What matters for
the gate is the *structure*: which information is decision-relevant and hidden.
Every constant is chosen to create hidden structure, not to flatter a policy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from rr.contracts import ActionSpec, AttemptContext
from rr.rng import clip
from rr.sim.latent import LatentState
from rr.taxonomy import ActionType, Channel, FailureCause, Persistence, PERSISTENCE, Regime

RESPONSE_MODEL_VERSION = "v1.0.0"


@dataclass(frozen=True)
class CauseParams:
    retry_base: float      # P(re-debit succeeds) under otherwise ideal conditions
    scope: str             # what a *different* instrument would bypass
    nudge_affinity: float  # how much a customer nudge can do about this cause
    self_heal_rate: float  # P(recovers with zero intervention inside the horizon)
    self_heal_median_h: float
    attempt_decay: float   # multiplicative penalty per prior re-debit


# scope semantics:
#   account    -- the money isn't there; another card on the same person rarely helps
#   instrument -- this card/token is broken; another instrument fixes it
#   issuer     -- this bank is the problem; another rail routes around it
#   gateway    -- transient plumbing
#   session    -- the customer didn't complete an interaction
#   none       -- nothing technical fixes it; needs a human or a new authorisation
CAUSE_PARAMS: dict[FailureCause, CauseParams] = {
    FailureCause.INSUFFICIENT_FUNDS:     CauseParams(0.55, "account",    0.30, 0.22, 60, 0.85),
    FailureCause.ISSUER_DOWNTIME:        CauseParams(0.80, "issuer",     0.10, 0.35,  6, 0.95),
    FailureCause.GATEWAY_TIMEOUT:        CauseParams(0.72, "gateway",    0.08, 0.45,  2, 0.90),
    FailureCause.UPI_COLLECT_EXPIRED:    CauseParams(0.30, "session",    0.45, 0.30,  8, 0.80),
    FailureCause.AUTH_FAILED:            CauseParams(0.28, "session",    0.40, 0.33,  5, 0.80),
    FailureCause.DO_NOT_HONOUR:          CauseParams(0.30, "issuer",     0.18, 0.15, 48, 0.75),
    FailureCause.LIMIT_EXCEEDED:         CauseParams(0.45, "account",    0.20, 0.28, 30, 0.90),
    FailureCause.CARD_EXPIRED:           CauseParams(0.01, "instrument", 0.42, 0.06, 96, 1.00),
    FailureCause.INVALID_CARD_DETAILS:   CauseParams(0.02, "instrument", 0.38, 0.08, 72, 1.00),
    FailureCause.MANDATE_INVALID:        CauseParams(0.00, "none",       0.30, 0.03, 96, 1.00),
    FailureCause.AMOUNT_EXCEEDS_MANDATE: CauseParams(0.00, "none",       0.25, 0.03, 96, 1.00),
    FailureCause.METHOD_NOT_ENABLED:     CauseParams(0.00, "config",     0.05, 0.02, 96, 1.00),
    FailureCause.LOST_STOLEN_FRAUD:      CauseParams(0.00, "none",       0.02, 0.01, 96, 1.00),
    FailureCause.RISK_DECLINE:           CauseParams(0.00, "none",       0.03, 0.02, 96, 1.00),
}

# How much of the cause an alternate instrument routes around.
SCOPE_BYPASS = {
    "instrument": 0.85,
    "issuer": 0.75,
    "gateway": 0.50,
    "session": 0.60,
    "account": 0.25,
    "config": 0.20,
    "none": 0.00,
}

ALT_BASE = 0.62
NUDGE_BASE = 0.80
NUDGE_FATIGUE = 0.55          # per nudge already sent
QUIET_HOUR_EFFECT = 0.35      # conversion penalty, not a compliance rule
BALANCE_FLOOR = 0.12
BALANCE_TAU_DAYS = 6.0
OUTAGE_INSIDE = 0.03
OUTAGE_RAMP_HOURS = 0.5
TOLERANCE_EXHAUSTED = 0.05
TRANSIENT_FRESHNESS_BONUS = 1.25   # retrying a timeout quickly is genuinely better
TRANSIENT_FRESH_WINDOW_H = 1.0


# ---------------------------------------------------------------- modifiers --

def balance_availability(latent: LatentState, sim_time_h: float, amount_minor: int) -> float:
    """Salary-cycle structure. The oracle's single biggest timing edge.

    Availability peaks on the customer's salary day and decays across the month.
    Nothing observable states salary_day; a learned policy can only infer it from
    day-of-month priors and this customer's history, which is exactly the kind of
    partially-recoverable signal the gate needs to exist.
    """
    day_of_month = int(sim_time_h // 24) % 30 + 1
    days_since_salary = (day_of_month - latent.salary_day) % 30
    avail = BALANCE_FLOOR + (1.0 - BALANCE_FLOOR) * math.exp(-days_since_salary / BALANCE_TAU_DAYS)
    strain = amount_minor / max(latent.monthly_capacity_minor, 1)
    return clip(avail * clip(1.15 - strain * 3.0, 0.05, 1.0), 0.02, 0.98)


def outage_multiplier(latent: LatentState, sim_time_h: float) -> float:
    if latent.outage_start_h is None or latent.outage_end_h is None:
        return 1.0
    if latent.outage_start_h <= sim_time_h < latent.outage_end_h:
        return OUTAGE_INSIDE
    if latent.outage_end_h <= sim_time_h < latent.outage_end_h + OUTAGE_RAMP_HOURS:
        return 0.60
    return 1.0


def _amount_friction(amount_minor: int) -> float:
    return 1.0 - 0.15 * min(amount_minor / 500_000, 1.0)


def _day_boundaries_crossed(failed_at_h: float, sim_time_h: float) -> int:
    return int(sim_time_h // 24) - int((sim_time_h - max(sim_time_h - failed_at_h, 0.0)) // 24)


# ------------------------------------------------------------------ actions --

def _p_debit(latent: LatentState, at: AttemptContext, alternate: bool) -> float:
    p = CAUSE_PARAMS[latent.true_cause]
    # No standing authority to re-debit a one-time checkout. The gateway rejects
    # it outright; a policy that tries is not merely wasteful, it is a violation.
    if at.regime is Regime.CUSTOMER_INITIATED:
        return 0.0
    if latent.terminal_truth:
        return 0.0
    if alternate and not at.has_alternate_instrument:
        return 0.0

    base = ALT_BASE * SCOPE_BYPASS[p.scope] if alternate else p.retry_base
    if base <= 0.0:
        return 0.0

    m = _amount_friction(at.amount_minor)
    m *= p.attempt_decay ** at.retry_index

    # An issuer that has stopped honouring re-debits keeps saying no. Invisible
    # from outside; only inferable from how many attempts already bounced.
    if at.retry_index >= latent.issuer_retry_tolerance:
        m *= TOLERANCE_EXHAUSTED

    outage = outage_multiplier(latent, at.sim_time_h)
    m *= outage if not alternate else (1.0 + outage) / 2.0  # another rail partly routes around

    if p.scope == "account":
        m *= balance_availability(latent, at.sim_time_h, at.amount_minor) / 0.6

    if latent.true_cause is FailureCause.LIMIT_EXCEEDED:
        # Daily limits reset at midnight. Retrying inside the same day is near-futile.
        crossed = int(at.sim_time_h // 24) > int((at.sim_time_h - at.elapsed_h) // 24)
        m *= 1.0 if crossed else 0.15

    if PERSISTENCE[latent.true_cause] is Persistence.TRANSIENT and at.elapsed_h <= TRANSIENT_FRESH_WINDOW_H:
        m *= TRANSIENT_FRESHNESS_BONUS

    return clip(base * m, 0.0, 0.97)


def _p_nudge(latent: LatentState, at: AttemptContext, channel: Channel) -> float:
    p = CAUSE_PARAMS[latent.true_cause]
    responsiveness = latent.channel_responsiveness.get(
        channel.value if hasattr(channel, "value") else str(channel), 0.5
    )
    m = latent.intent_to_pay * responsiveness
    m *= NUDGE_FATIGUE ** at.nudges_sent

    hour_ist = int(at.sim_time_h % 24)
    if hour_ist >= 21 or hour_ist < 9:
        m *= QUIET_HOUR_EFFECT

    if p.scope == "account":
        # Telling someone to pay when they have no money does not create money.
        m *= balance_availability(latent, at.sim_time_h, at.amount_minor) / 0.6

    if latent.terminal_truth and latent.true_cause in (
        FailureCause.LOST_STOLEN_FRAUD, FailureCause.RISK_DECLINE
    ):
        return 0.0

    return clip(NUDGE_BASE * p.nudge_affinity * m, 0.0, 0.95)


def _p_merchant_alert(latent: LatentState, at: AttemptContext) -> float:
    if latent.true_cause in (FailureCause.METHOD_NOT_ENABLED, FailureCause.AMOUNT_EXCEEDS_MANDATE):
        return 0.45
    return 0.02


def _p_escalate(latent: LatentState, at: AttemptContext) -> float:
    if latent.true_cause in (FailureCause.LOST_STOLEN_FRAUD, FailureCause.RISK_DECLINE):
        return 0.02
    scope = CAUSE_PARAMS[latent.true_cause].scope
    base = 0.10 if scope == "none" else 0.32
    return clip(base * latent.intent_to_pay, 0.0, 0.9)


def p_recovery(action: ActionSpec, latent: LatentState, at: AttemptContext) -> float:
    """The frozen contract. Pure function; no RNG, no I/O, no state."""
    t = action.type
    if t is ActionType.NO_ACTION:
        return 0.0
    if t is ActionType.RETRY_SAME:
        return _p_debit(latent, at, alternate=False)
    if t is ActionType.RETRY_ALTERNATE_METHOD:
        return _p_debit(latent, at, alternate=True)
    if t is ActionType.NUDGE:
        assert action.channel is not None, "NUDGE requires a channel"
        return _p_nudge(latent, at, action.channel)
    if t is ActionType.MERCHANT_ALERT:
        return _p_merchant_alert(latent, at)
    if t is ActionType.ESCALATE_HUMAN:
        return _p_escalate(latent, at)
    raise ValueError(f"unhandled action type {t}")


def resolution_lag_hours(action: ActionSpec, latent: LatentState) -> float:
    """A successful action does not always convert instantly."""
    if action.type is ActionType.MERCHANT_ALERT:
        return 12.0   # merchant fixes config, customer re-presents
    if action.type is ActionType.ESCALATE_HUMAN:
        return 6.0
    if action.type is ActionType.NUDGE:
        return 1.5    # customer sees it, pays a bit later
    return 0.0
