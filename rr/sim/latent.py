"""Ground truth. Simulator-only.

Nothing under rr/agent/ or rr/policy/ may import this module. tests/test_boundary.py
greps for such an import and fails the build. The cohort generator writes latent
state to a *separate file* from observed events, so the boundary is enforced at the
filesystem level too, not just by convention.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Optional

from rr.taxonomy import Channel, FailureCause


@dataclass(frozen=True)
class LatentState:
    # --- what actually went wrong -------------------------------------------
    true_cause: FailureCause
    # True when the account/instrument is dead regardless of what the gateway
    # code suggests. Drives the "soft code, terminal truth" adversarial slice.
    terminal_truth: bool

    # --- customer interior --------------------------------------------------
    intent_to_pay: float             # 0-1. Governs nudge and escalation response.
    salary_day: int                  # 1-28. Anchors the balance trajectory.
    monthly_capacity_minor: int      # disposable income proxy; amount strain
    channel_responsiveness: dict     # Channel value -> 0-1

    # --- issuer interior ----------------------------------------------------
    # How many re-debits this issuer will honour before hard-blocking. The single
    # thing that separates a retryable do_not_honour from a permanent one, and
    # nothing observable reveals it directly.
    issuer_retry_tolerance: int
    outage_start_h: Optional[float]
    outage_end_h: Optional[float]

    # --- counterfactual -----------------------------------------------------
    # Absolute sim time at which this payment recovers with ZERO intervention.
    # None means it never self-heals inside the horizon. Fixed at generation and
    # independent of any policy, which is what makes the incremental number real:
    # a policy that "recovers" a payment before its self_heal_at gets credited
    # gross but earns nothing incremental.
    self_heal_at_h: Optional[float]

    slice_tag: str = "base"


LATENT_FIELD_NAMES = frozenset(f.name for f in fields(LatentState))
