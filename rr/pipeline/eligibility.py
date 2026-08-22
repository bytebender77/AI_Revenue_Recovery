"""The eligibility gate. Runs BEFORE the policy and emits the set of permitted actions.

Constraints shape the action space; they are not a veto applied afterwards. The
policy never sees an illegal action, so a missed check cannot become a live
breach -- it can only produce an empty option set. Every rule records its
threshold and the observed value, and PASSES are stored as well as blocks:
"we checked and it was fine" is evidence, silence is not.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from rr.baselines.rules import ESCALATE_MIN_AMOUNT_MINOR, MAX_DEBITS, MAX_NUDGES
from rr.budget import EscalationBudget
from rr.pipeline.normalize import Diagnosis
from rr.taxonomy import ActionType, Channel, NEVER_RETRY, Regime

DEBITS = (ActionType.RETRY_SAME, ActionType.RETRY_ALTERNATE_METHOD)


@dataclass(frozen=True)
class RuleEval:
    rule_id: str
    description: str
    threshold: str
    observed: str
    result: str                 # pass | block
    blocks: tuple[str, ...] = ()


@dataclass
class PolicyState:
    slot: int = 0
    retry_index: int = 0
    nudges_sent: int = 0
    merchant_alerted: bool = False
    escalated: bool = False


@dataclass
class Eligibility:
    permitted: list[str]
    blocked: list[dict]
    rules: list[RuleEval]
    context: dict

    def blocking_rule(self, action: ActionType) -> Optional[str]:
        return next((b["rule_id"] for b in self.blocked if b["action"] == action.value), None)


def evaluate(event: dict, diag: Diagnosis, st: PolicyState,
             budget: Optional[EscalationBudget]) -> Eligibility:
    regime = Regime(event["regime"])
    channels = [Channel(c) for c in event["consented_channels"]]
    amount = event["amount_minor"]
    rules: list[RuleEval] = []
    blocked: dict[str, str] = {}

    def rule(rid: str, desc: str, threshold, observed, violated: bool, blocks=()):
        rules.append(RuleEval(rid, desc, str(threshold), str(observed),
                              "block" if violated else "pass", tuple(a.value for a in blocks)))
        if violated:
            for a in blocks:
                blocked.setdefault(a.value, rid)

    rule("R001_no_mandate_no_debit", "re-debit requires a standing mandate",
         "regime == merchant_initiated", regime.value,
         regime is Regime.CUSTOMER_INITIATED, DEBITS)
    rule("R002_terminal_cause_no_debit", "cause is terminal for re-debit",
         f"cause not in {sorted(c.value for c in NEVER_RETRY)}", diag.failure_cause.value,
         diag.failure_cause in NEVER_RETRY, DEBITS)
    rule("R003_max_debits", "per-intent re-debit cap",
         MAX_DEBITS, st.retry_index, st.retry_index >= MAX_DEBITS, DEBITS)
    rule("R004_max_nudges", "per-intent contact cap",
         MAX_NUDGES, st.nudges_sent, st.nudges_sent >= MAX_NUDGES, (ActionType.NUDGE,))
    rule("R005_no_consented_channel", "contact requires recorded consent",
         ">=1 consented channel", len(channels), len(channels) == 0, (ActionType.NUDGE,))
    rule("R006_no_alternate_instrument", "alternate rail needs a second consented instrument",
         True, event["has_alternate_instrument"],
         not event["has_alternate_instrument"], (ActionType.RETRY_ALTERNATE_METHOD,))
    rule("R007_escalation_capacity", "shared ops review capacity",
         (budget.capacity if budget else "unbounded"), (budget.used if budget else 0),
         budget is not None and not budget.available(), (ActionType.ESCALATE_HUMAN,))
    rule("R008_escalation_value_floor", "escalate only the high-value tail",
         ESCALATE_MIN_AMOUNT_MINOR, amount,
         amount < ESCALATE_MIN_AMOUNT_MINOR, (ActionType.ESCALATE_HUMAN,))
    rule("R009_merchant_alert_once", "one merchant alert per intent",
         1, int(st.merchant_alerted), st.merchant_alerted, (ActionType.MERCHANT_ALERT,))
    rule("R010_escalation_once", "one escalation per intent",
         1, int(st.escalated), st.escalated, (ActionType.ESCALATE_HUMAN,))

    permitted = [a.value for a in ActionType if a.value not in blocked]
    return Eligibility(
        permitted=permitted,
        blocked=[{"action": a, "rule_id": r} for a, r in sorted(blocked.items())],
        rules=rules,
        context={
            "cause": diag.failure_cause.value, "persistence": diag.persistence_class,
            "regime": regime.value, "amount_minor": amount, "slot": st.slot,
            "retry_index": st.retry_index, "nudges_sent": st.nudges_sent,
            "consented_channels": [c.value for c in channels],
            "escalation_budget_remaining": budget.remaining if budget else None,
        },
    )


def rules_as_json(rules: list[RuleEval]) -> list[dict]:
    return [asdict(r) for r in rules]
