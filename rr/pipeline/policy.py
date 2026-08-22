"""M2 policy: a faithful port of B2, emitting the full decision record shape.

Deliberately trivial. There is no success model and no expected-value arithmetic
yet -- `score_basis` says `rule_priority` for exactly that reason, and M4 swaps it
to `expected_net_value` without changing the record shape. What matters now is
that the record is already complete: every candidate action with its score, not
only the winner, plus the constraint that removed anything ranked higher.

The ladder constants are imported from the B2 baseline rather than copied, so the
port cannot silently drift from the arm it is measured against. M4 cuts that tie.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rr.baselines.rules import (
    CUSTOMER_FIXABLE, HANDS_OFF, MAX_DEBITS, MERCHANT_FIXABLE, _ladder,
    pick_channel, shift_out_of_quiet_hours,
)
from rr.pipeline.eligibility import Eligibility, PolicyState
from rr.pipeline.normalize import Diagnosis
from rr.taxonomy import ActionType, Regime
from rr.versions import MODEL_VERSION, POLICY_VERSION, TAXONOMY_VERSION

SCORE_BASIS = "rule_priority"


@dataclass
class DecisionRecord:
    chosen_action: str
    chosen_channel: Optional[str]
    scheduled_for_h: Optional[float]
    candidate_set: list[dict]
    score_basis: str
    decision_reason_code: str
    binding_constraint: Optional[str]
    taxonomy_version: str = TAXONOMY_VERSION
    model_version: str = MODEL_VERSION
    policy_version: str = POLICY_VERSION


def _proposals(event: dict, diag: Diagnosis, st: PolicyState) -> list[dict]:
    """Every action the policy is willing to consider, scored and timed.

    Permission is not consulted here -- that is the gate's job, and keeping the
    two separate is what lets the record show what was wanted but not allowed.
    """
    t0, cause = event["failed_at_h"], diag.failure_cause
    regime = Regime(event["regime"])
    ch = pick_channel(event)
    ch_v = ch.value if ch else None
    out: list[dict] = [{"action": ActionType.NO_ACTION.value, "channel": None,
                        "at_h": None, "score": 0.0, "rationale": "always available"}]

    def add(action: ActionType, score: float, at: Optional[float], rationale: str,
            channel: Optional[str] = None):
        out.append({"action": action.value, "channel": channel,
                    "at_h": round(at, 3) if at is not None else None,
                    "score": score, "rationale": rationale})

    if regime is Regime.CUSTOMER_INITIATED:
        add(ActionType.NUDGE, 100.0, shift_out_of_quiet_hours(t0 + 2.0),
            "no standing authority to re-debit; re-engagement is the only lever", ch_v)
        add(ActionType.ESCALATE_HUMAN, 50.0, t0 + 30.0, "high-value tail after nudge")
        add(ActionType.RETRY_SAME, 0.0, t0 + 1.0, "would require a mandate this intent lacks")
    elif cause in HANDS_OFF:
        add(ActionType.ESCALATE_HUMAN, 100.0, t0 + 2.0, "blocked instrument; human review only")
        add(ActionType.NUDGE, 0.0, t0 + 2.0, "messaging a blocked instrument achieves nothing", ch_v)
    elif cause in MERCHANT_FIXABLE:
        add(ActionType.MERCHANT_ALERT, 100.0, t0 + 1.0, "merchant-side configuration problem")
        add(ActionType.ESCALATE_HUMAN, 40.0, t0 + 24.0, "if the merchant does not act")
    elif cause in CUSTOMER_FIXABLE:
        add(ActionType.NUDGE, 100.0, shift_out_of_quiet_hours(t0 + 3.0),
            "customer can fix the instrument; a re-debit cannot", ch_v)
        add(ActionType.ESCALATE_HUMAN, 50.0, t0 + 30.0, "high-value tail after nudge")
        add(ActionType.RETRY_SAME, 0.0, t0 + 3.0, "terminal for re-debit")
    else:
        ladder = _ladder(cause)
        if st.retry_index < min(len(ladder), MAX_DEBITS):
            at = t0 + ladder[st.retry_index]
            add(ActionType.RETRY_SAME, 100.0, at, f"soft decline, ladder step {st.retry_index + 1}")
            add(ActionType.RETRY_ALTERNATE_METHOD, 60.0, at, "alternate consented instrument")
            add(ActionType.NUDGE, 40.0, shift_out_of_quiet_hours(t0 + ladder[-1] + 6.0),
                "held back until the ladder is spent", ch_v)
            add(ActionType.ESCALATE_HUMAN, 30.0, t0 + ladder[-1] + 30.0, "held back until the ladder is spent")
        else:
            tail = t0 + ladder[-1] + 6.0
            add(ActionType.NUDGE, 100.0, shift_out_of_quiet_hours(tail), "ladder spent", ch_v)
            add(ActionType.ESCALATE_HUMAN, 60.0, tail + 24.0, "ladder spent, high-value tail")
    return out


def _reason_code(action: str, diag: Diagnosis, event: dict, st: PolicyState) -> str:
    cause = diag.failure_cause
    if action == ActionType.NO_ACTION.value:
        return "NO_PERMITTED_ACTION_WITH_POSITIVE_PRIORITY"
    if Regime(event["regime"]) is Regime.CUSTOMER_INITIATED:
        return "NO_MANDATE_REENGAGE_ONLY"
    if cause in HANDS_OFF:
        return "TERMINAL_CAUSE_HUMAN_REVIEW"
    if cause in MERCHANT_FIXABLE:
        return "MERCHANT_SIDE_CONFIGURATION"
    if cause in CUSTOMER_FIXABLE:
        return "CUSTOMER_FIXABLE_INSTRUMENT"
    if action == ActionType.ESCALATE_HUMAN.value:
        return "ESCALATE_HIGH_VALUE_TAIL"
    if action == ActionType.NUDGE.value:
        return "LADDER_EXHAUSTED_REENGAGE"
    return "SOFT_DECLINE_LADDER_RETRY"


def decide(event: dict, diag: Diagnosis, st: PolicyState, elig: Eligibility) -> DecisionRecord:
    proposals = sorted(_proposals(event, diag, st), key=lambda p: -p["score"])
    permitted = set(elig.permitted)

    chosen, binding = None, None
    for p in proposals:
        p["permitted"] = p["action"] in permitted
        p["blocked_by"] = None if p["permitted"] else elig.blocking_rule(ActionType(p["action"]))
        # A nudge with no consented channel is unroutable even when the gate allows it.
        if p["action"] == ActionType.NUDGE.value and p["channel"] is None:
            p["permitted"], p["blocked_by"] = False, "R005_no_consented_channel"
        if chosen is None and p["permitted"] and p["score"] > 0:
            chosen = p
        elif chosen is None and not p["permitted"] and binding is None and p["score"] > 0:
            # Ranked above whatever we end up choosing, and something stopped it.
            binding = p["blocked_by"]
    if chosen is None:
        chosen = next(p for p in proposals if p["action"] == ActionType.NO_ACTION.value)

    for p in proposals:
        p["chosen"] = p is chosen

    return DecisionRecord(
        chosen_action=chosen["action"], chosen_channel=chosen["channel"],
        scheduled_for_h=chosen["at_h"], candidate_set=proposals, score_basis=SCORE_BASIS,
        decision_reason_code=_reason_code(chosen["action"], diag, event, st),
        binding_constraint=binding,
    )
