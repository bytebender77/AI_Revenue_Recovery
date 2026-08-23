"""The executor seam. NOT CURRENTLY WIRED -- read this before assuming it runs.

When the pipeline gained a Postgres sink and started sharing the eval harness's
tick loop, the loop took over execution (it calls `apply_action` against the
simulated world) and the sink took over recording. That left this interface with no
live caller: `revalidate` / `execute` / `poll_outcome` are implemented by
`rr/sim/adapter.py` and stubbed by `razorpay_test.py`, and nothing invokes them.

It is kept, and kept honest with this notice, because it is the contract M7's one
live Razorpay test-mode call plugs into. Re-wiring it is M7 work, not decoration.
The guarantees it used to provide -- idempotency on (decision, action), fire-time
revalidation, an `attempt` row per fired-or-aborted action -- are preserved in
`PostgresSink` and asserted by tests/test_pipeline_db.py."""
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
