"""One interface, two adapters. The eval runs on `sim`; the demo makes one live
test-mode call. Fixing the interface now means the live adapter is a drop-in and
not a rewrite."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

REVALIDATION_OK = "ok"
REVALIDATION_ALREADY_RECOVERED = "already_recovered"
REVALIDATION_TERMINAL = "terminal_state"


@dataclass(frozen=True)
class ExecutionRequest:
    payment_intent_id: str
    idempotency_key: str
    slot: int
    action_type: str
    channel: Optional[str]
    at_h: float
    amount_minor: int
    method: str
    regime: str
    instrument_ref: str
    mandate_id: Optional[str]
    retry_index: int
    nudges_sent: int


@dataclass(frozen=True)
class ExecutionResult:
    outcome: str                 # success | failed | unauthorized_debit_rejected
    p_used: Optional[float]
    response: dict


class ExecutorAdapter(Protocol):
    name: str

    def revalidate(self, payment_intent_id: str, at_h: float) -> str:
        """Re-read payment state at FIRE time, not decision time."""

    def execute(self, req: ExecutionRequest) -> ExecutionResult: ...

    def poll_outcome(self, payment_intent_id: str, until_h: float):
        """-> (recovered_at_h | None, attribution | None). Stands in for the webhook."""
