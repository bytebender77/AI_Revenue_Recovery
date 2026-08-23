"""The single decision record shape, built in exactly one place.

There used to be two decision layers -- an M2 rules port behind `make run` and the
M4 EV policy behind the eval harness -- so the demo and the numbers described
different systems. This module is the join: every decision, whichever entry point
asked for it, is scored by `rr/agent/policy.py` and serialised here.

`decision_reason_code` is the source of truth. The prose explainer renders it; it
never produces it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from rr.taxonomy import ActionType
from rr.versions import EV_POLICY_VERSION, MODEL_VERSION, TAXONOMY_VERSION

SCORE_BASIS = "expected_net_value"
AUCTION_RULE = "R011_capacity_auction_lost"


@dataclass
class DecisionRecord:
    payment_intent_id: str
    slot: int          # attempt index; NOT unique -- see decision_seq
    decision_seq: int  # monotonic per intent; distinguishes a re-decision from a duplicate
    arm: str
    chosen_action: str
    chosen_channel: Optional[str]
    scheduled_for_h: Optional[float]
    candidate_set: list
    score_basis: str
    decision_reason_code: str
    binding_constraint: Optional[str]
    taxonomy_version: str = TAXONOMY_VERSION
    model_version: str = MODEL_VERSION
    policy_version: str = EV_POLICY_VERSION

    def as_row(self) -> dict:
        return {
            "payment_intent_id": self.payment_intent_id, "slot": self.slot,
            "decision_seq": self.decision_seq,
            "arm": self.arm, "chosen_action": self.chosen_action,
            "chosen_channel": self.chosen_channel,
            "scheduled_for_h": self.scheduled_for_h,
            "candidate_set": self.candidate_set, "score_basis": self.score_basis,
            "decision_reason_code": self.decision_reason_code,
            "binding_constraint": self.binding_constraint,
            "taxonomy_version": self.taxonomy_version,
            "model_version": self.model_version, "policy_version": self.policy_version,
        }


def no_action_reason(candidates, lost_auction: bool) -> tuple[str, Optional[str]]:
    """Why nothing was done. Three economically distinct answers, never merged:
    someone else's payment outbid this one, a rule removed the option, or nothing
    available cleared its own cost."""
    if lost_auction:
        return "CAPACITY_AUCTION_LOST", AUCTION_RULE
    blocked_but_wanted = [c for c in candidates if not c.permitted and c.ev_hi > 0]
    if blocked_but_wanted:
        best = max(blocked_but_wanted, key=lambda c: c.ev_mean)
        return "BLOCKED_BY_RULE", best.blocked_by
    return "NEGATIVE_EV_TERMINAL", "EV_UPPER_BOUND_NOT_POSITIVE"


def build(payment_intent_id: str, slot: int, decision_seq: int, arm: str,
          candidates, chosen, lost_auction: bool = False) -> DecisionRecord:
    """Serialise a scored candidate set and its winner. Deterministic ordering so
    two entry points asking the same question produce byte-identical records."""
    ordered = sorted(candidates, key=lambda c: (-c.ev_mean, c.action, str(c.channel)))
    if chosen.action == ActionType.NO_ACTION.value:
        code, binding = no_action_reason(candidates, lost_auction)
    else:
        code = "POSITIVE_EXPECTED_NET_VALUE"
        binding = AUCTION_RULE if lost_auction else _binding(ordered, chosen)
    return DecisionRecord(
        payment_intent_id=payment_intent_id, slot=slot, decision_seq=decision_seq,
        arm=arm, chosen_action=chosen.action, chosen_channel=chosen.channel,
        scheduled_for_h=chosen.at_h,
        candidate_set=[c.as_record() for c in ordered],
        score_basis=SCORE_BASIS, decision_reason_code=code, binding_constraint=binding)


def _binding(ordered, chosen) -> Optional[str]:
    """The rule that removed a higher-scoring option than the one taken. Null when
    nothing outranked the choice -- that is a real answer, not a missing value."""
    for c in ordered:
        if c is chosen or c.ev_mean <= chosen.ev_mean:
            break
        if not c.permitted and c.blocked_by:
            return c.blocked_by
    return None


def control_record(payment_intent_id: str, slot: int, decision_seq: int) -> DecisionRecord:
    """The randomised holdout still gets a full, auditable decision record. A
    control arm you cannot audit is not a control arm."""
    return DecisionRecord(
        payment_intent_id=payment_intent_id, slot=slot, decision_seq=decision_seq,
        arm="control",
        chosen_action=ActionType.NO_ACTION.value, chosen_channel=None,
        scheduled_for_h=None,
        candidate_set=[{"action": ActionType.NO_ACTION.value, "channel": None,
                        "at_h": None, "score": 0.0, "permitted": True,
                        "blocked_by": None, "chosen": True,
                        "evidence": {"note": "randomised holdout: no action by design"},
                        "components_inr": {}}],
        score_basis=SCORE_BASIS, decision_reason_code="CONTROL_ARM_HOLDOUT",
        binding_constraint="ARM_ASSIGNMENT")
