"""B0, B1, B2. None of these may import ground truth -- tests/test_boundary.py checks.

B2 is the honest opponent. It is what a competent payments engineer writes in a
day, and if the agent cannot beat it the project has no thesis. It is written to
be genuinely good, not to lose.
"""
from __future__ import annotations

from typing import Optional

from rr.config import REGULATORY
from rr.contracts import ActionSpec
from rr.sim.world import RunState
from rr.taxonomy import (
    ActionType, Channel, FailureCause, NEVER_RETRY, Persistence, PERSISTENCE, Regime,
    normalize_reason,
)

C = FailureCause

# Retry ladders in hours after the original failure, by cause class.
LADDER_TRANSIENT = (0.5, 4.0, 24.0)
LADDER_FUNDS = (26.0, 74.0, 146.0)      # cross day boundaries; limits reset, salary lands
LADDER_SESSION = (2.0, 24.0, 72.0)
LADDER_UNKNOWN = (24.0,)                 # one cautious attempt on an uninformative code

MAX_DEBITS = 3
MAX_NUDGES = 1

# Human review is capacity-bound, so a competent rules engine rations it to the
# high-value tail rather than escalating everything that fails.
# TODO(citation): real threshold is a merchant ops-capacity decision, not a
# regulatory one. Placeholder.
ESCALATE_MIN_AMOUNT_MINOR = 200_000
CHANNEL_PREFERENCE = (Channel.WHATSAPP, Channel.SMS, Channel.IN_APP, Channel.EMAIL)

# Causes where the customer can fix the problem themselves but a re-debit cannot.
CUSTOMER_FIXABLE = frozenset({C.CARD_EXPIRED, C.INVALID_CARD_DETAILS, C.MANDATE_INVALID})
MERCHANT_FIXABLE = frozenset({C.METHOD_NOT_ENABLED, C.AMOUNT_EXCEEDS_MANDATE})
# Nothing a retry or a message can do. Acting here is worse than useless.
HANDS_OFF = frozenset({C.LOST_STOLEN_FRAUD, C.RISK_DECLINE})


def shift_out_of_quiet_hours(t: float) -> float:
    """Move a contact out of the quiet window.

    TODO(citation): quiet-hours bounds come from REGULATORY.quiet_hours_ist, which
    is UNVERIFIED. See docs/compliance-open-questions.md item 4.
    """
    start, end = REGULATORY.quiet_hours_ist
    hour = t % 24
    if hour >= start:
        return (t - hour) + 24 + end + 1.0
    if hour < end:
        return (t - hour) + end + 1.0
    return t


def pick_channel(obs: dict) -> Optional[Channel]:
    consented = {Channel(c) for c in obs["consented_channels"]}
    for ch in CHANNEL_PREFERENCE:
        if ch in consented:
            return ch
    return None


def _ladder(cause: FailureCause) -> tuple[float, ...]:
    if cause is C.UNKNOWN:
        return LADDER_UNKNOWN
    if PERSISTENCE[cause] is Persistence.TRANSIENT:
        return LADDER_TRANSIENT
    if cause in (C.INSUFFICIENT_FUNDS, C.LIMIT_EXCEEDED):
        return LADDER_FUNDS
    return LADDER_SESSION


class B0DoNothing:
    name = "B0_do_nothing"

    def next_action(self, obs: dict, st: RunState) -> Optional[ActionSpec]:
        return None


class B1BlindLadder:
    """Retry everything at +1h, +24h, +72h. No cause check, no regime check.

    The naive industry default, and the arm that shows what indiscriminate
    dunning actually costs.
    """
    name = "B1_blind_ladder"
    LADDER = (1.0, 24.0, 72.0)

    def next_action(self, obs: dict, st: RunState) -> Optional[ActionSpec]:
        if st.retry_index >= len(self.LADDER):
            return None
        return ActionSpec(ActionType.RETRY_SAME, st.failed_at_h + self.LADDER[st.retry_index])


class B2GoodRules:
    """Soft declines only, exponential-ish backoff, NEVER_RETRY respected, one nudge,
    no re-debit without a mandate."""
    name = "B2_good_rules"

    def next_action(self, obs: dict, st: RunState) -> Optional[ActionSpec]:
        cause = normalize_reason(obs["gateway_reason"])
        regime = Regime(obs["regime"])
        t0 = st.failed_at_h

        # Blocked instruments: never re-debit, never message. Route the high-value
        # tail to a human for review -- this is the escalation rung the bar asks for.
        if cause in HANDS_OFF:
            return self._escalate(obs, st, t0 + 2.0)

        if cause in MERCHANT_FIXABLE:
            if not st.merchant_alerted:
                return ActionSpec(ActionType.MERCHANT_ALERT, t0 + 1.0)
            return self._escalate(obs, st, t0 + 24.0)

        # No standing authority to re-debit a one-time checkout. One nudge, then stop.
        if regime is Regime.CUSTOMER_INITIATED:
            return self._nudge(obs, st, t0 + 2.0) or self._escalate(obs, st, t0 + 30.0)

        if cause in NEVER_RETRY:
            # Customer-fixable: a message is the only thing that can work.
            nudge = self._nudge(obs, st, t0 + 3.0) if cause in CUSTOMER_FIXABLE else None
            return nudge or self._escalate(obs, st, t0 + 30.0)

        ladder = _ladder(cause)
        if st.retry_index < min(len(ladder), MAX_DEBITS):
            return ActionSpec(ActionType.RETRY_SAME, t0 + ladder[st.retry_index])

        # Debits exhausted. One nudge, then the high-value tail goes to a human.
        tail = t0 + ladder[-1] + 6.0
        return self._nudge(obs, st, tail) or self._escalate(obs, st, tail + 24.0)

    def _escalate(self, obs: dict, st: RunState, at: float) -> Optional[ActionSpec]:
        if st.escalated or obs["amount_minor"] < ESCALATE_MIN_AMOUNT_MINOR:
            return None
        return ActionSpec(ActionType.ESCALATE_HUMAN, at)

    def _nudge(self, obs: dict, st: RunState, at: float) -> Optional[ActionSpec]:
        if st.nudges_sent >= MAX_NUDGES:
            return None
        ch = pick_channel(obs)
        if ch is None:
            return None
        return ActionSpec(ActionType.NUDGE, shift_out_of_quiet_hours(at), channel=ch)
