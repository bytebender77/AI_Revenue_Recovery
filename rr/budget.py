"""Portfolio-level capacity budgets.

Per-transaction rules can all pass while the fleet does something collectively
unaffordable. Human review is the clearest case: escalation is +EV on almost any
individual failure, but an ops team can only work a fixed share of them. The
budget is shared across every intent in a run and consumed chronologically.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EscalationBudget:
    """Consumed when a policy commits to an escalation, i.e. when the queue slot
    is reserved -- not when a human eventually works it. A slot spent on a payment
    that self-heals first is a slot genuinely lost, which is the real behaviour."""
    capacity: int
    used: int = 0

    @classmethod
    def for_cohort(cls, n_intents: int, pct: float) -> "EscalationBudget":
        return cls(capacity=int(n_intents * pct))

    def available(self) -> bool:
        return self.used < self.capacity

    def consume(self) -> bool:
        if not self.available():
            return False
        self.used += 1
        return True

    @property
    def remaining(self) -> int:
        return max(self.capacity - self.used, 0)
