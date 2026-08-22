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


# Confidence levels are reported by the resolver as words; the Diagnosis record
# carries a number so downstream code has one comparable scale.
_CONFIDENCE_NUMERIC = {"high": 0.9, "medium": 0.65, "low": 0.3}


def diagnose(event: dict, tail=None) -> Diagnosis:
    """Deterministic map first. The LLM tail is consulted only for what it misses.

    `tail` is a rr.normalize.llm_tail.TailNormalizer, or None. Injected rather than
    imported so the pipeline has no import-time dependency on the model, and so the
    ablation can run the identical code path with the resolver switched off.
    """
    cause = normalize_reason(event["gateway_reason"])
    if cause is not FailureCause.UNKNOWN:
        return Diagnosis(cause, PERSISTENCE[cause].value, "map", 1.0)

    if tail is None:
        # An unmapped code yields UNKNOWN with zero confidence. It never yields a
        # guess -- the conservative path downstream is the whole point.
        return Diagnosis(cause, PERSISTENCE[cause].value, "default", 0.0)

    resolved, confidence, _call = tail.resolve(event)
    if resolved is FailureCause.UNKNOWN:
        # Abstention. Indistinguishable downstream from having no resolver at all,
        # which is exactly the intended failure mode.
        return Diagnosis(resolved, PERSISTENCE[resolved].value, "llm_abstain", 0.0)
    return Diagnosis(resolved, PERSISTENCE[resolved].value, "llm",
                     _CONFIDENCE_NUMERIC.get(confidence, 0.0))
