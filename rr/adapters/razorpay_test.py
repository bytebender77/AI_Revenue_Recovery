"""Razorpay test-mode adapter. INTERFACE FIXED, NOT YET IMPLEMENTED.

Deliberately a stub. The eval runs thousands of simulated attempts and pushing
those through a live sandbox buys nothing but rate limits and flakiness. The demo
makes one real test-mode call through this adapter; everything else runs on `sim`.

UNCERTAIN, to resolve before wiring this up:
  * whether test mode can deterministically force a SPECIFIC failure reason, or
    only a generic failure. Determines how faithful the live call can be.
  * the exact error `code`/`reason`/`source`/`step` enumerations returned.
  See docs/compliance-open-questions.md items 6-7.
"""
from __future__ import annotations

from rr.adapters.base import ExecutionRequest, ExecutionResult

_NOT_YET = ("razorpay_test adapter is not implemented yet (M7). "
            "The interface is fixed; the eval runs on the `sim` adapter.")


class RazorpayTestAdapter:
    name = "razorpay_test"

    def revalidate(self, payment_intent_id: str, at_h: float) -> str:
        raise NotImplementedError(_NOT_YET)

    def execute(self, req: ExecutionRequest) -> ExecutionResult:
        raise NotImplementedError(_NOT_YET)

    def poll_outcome(self, payment_intent_id: str, until_h: float):
        raise NotImplementedError(_NOT_YET)
