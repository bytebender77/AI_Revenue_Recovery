"""The observable side of the boundary.

Everything in this module is what the agent is allowed to see. LatentState lives
in rr/sim/latent.py and never crosses into here. The two are kept field-disjoint
and tests/test_boundary.py enforces that mechanically -- if you add a field to
one that also exists in the other, the test fails.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Optional

from rr.taxonomy import ActionType, Channel, Method, Regime


@dataclass(frozen=True)
class ObservedFailureEvent:
    """Exactly what arrives from the gateway plus merchant-side facts we hold.

    No ground truth. `gateway_*` fields may be generic, may be a code absent from
    our taxonomy, and may be actively misleading -- that is the point.
    """
    intent_id: str
    merchant_id: str
    customer_id: str
    issuer_id: str
    amount_minor: int
    currency: str
    method: Method
    regime: Regime
    mandate_id: Optional[str]
    instrument_ref: str            # token / VPA hash. Never a raw PAN.
    has_alternate_instrument: bool
    consented_channels: tuple[Channel, ...]
    failed_at_h: float             # sim hours since t=0
    attempt_index: int             # 0 for the original debit
    gateway_code: str
    gateway_reason: str
    gateway_source: str
    gateway_step: str
    gateway_description: str
    customer_prior_failures_30d: int
    merchant_prior_failures_30d: int


@dataclass(frozen=True)
class AttemptContext:
    """State at the moment an action fires. All observable."""
    sim_time_h: float
    elapsed_h: float
    retry_index: int          # debits already attempted after the original failure
    nudges_sent: int
    amount_minor: int
    method: Method
    regime: Regime
    has_alternate_instrument: bool


@dataclass(frozen=True)
class ActionSpec:
    type: ActionType
    at_h: float                       # absolute sim time the action fires
    channel: Optional[Channel] = None


def observed_field_names() -> frozenset[str]:
    return frozenset(f.name for f in fields(ObservedFailureEvent)) | frozenset(
        f.name for f in fields(AttemptContext)
    )


def assert_no_latent_leak(obj) -> None:
    """Runtime tripwire for anything handed to agent code.

    Cheap belt-and-braces on top of the static disjointness test: catches dicts
    assembled by hand, which the dataclass boundary would not.
    """
    from rr.sim.latent import LATENT_FIELD_NAMES

    if hasattr(obj, "__dataclass_fields__"):
        keys = set(asdict(obj).keys())
    elif isinstance(obj, dict):
        keys = set(obj.keys())
    else:
        return
    leaked = keys & LATENT_FIELD_NAMES
    if leaked:
        raise AssertionError(f"latent field(s) leaked into agent context: {sorted(leaked)}")
