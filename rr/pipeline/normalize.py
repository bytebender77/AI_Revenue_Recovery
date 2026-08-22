"""Gateway view -> failure_cause + persistence_class. Deterministic lookup only.

M5 adds an enum-constrained LLM resolver for the tail this map misses -- the
generic `payment_failed` and the bank-specific codes. The ablation against
today's numbers is the point of keeping this deliberately incomplete now.
"""
from __future__ import annotations

from dataclasses import dataclass

from rr.taxonomy import FailureCause, PERSISTENCE, normalize_reason
from rr.versions import NORMALIZER_VERSION, TAXONOMY_VERSION


@dataclass(frozen=True)
class Diagnosis:
    failure_cause: FailureCause
    persistence_class: str
    resolver: str          # map | llm (M5) | default
    confidence: float
    taxonomy_version: str = TAXONOMY_VERSION
    normalizer_version: str = NORMALIZER_VERSION


def diagnose(event: dict) -> Diagnosis:
    cause = normalize_reason(event["gateway_reason"])
    hit = cause is not FailureCause.UNKNOWN
    return Diagnosis(
        failure_cause=cause,
        persistence_class=PERSISTENCE[cause].value,
        resolver="map" if hit else "default",
        # An unmapped code yields UNKNOWN with zero confidence. It never yields a
        # guess -- that is what the M5 tail resolver is for, and it will be
        # constrained to this same closed enum.
        confidence=1.0 if hit else 0.0,
    )
