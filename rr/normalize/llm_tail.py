"""LLM tail normaliser. Resolves what the deterministic map cannot.

WHY THIS CALL IS LOAD-BEARING, IN ONE NUMBER: `payment_failed` is the gateway's
generic catch-all reason, so the deterministic map answers UNKNOWN for it. But the
DO_NOT_HONOUR view carries that same reason alongside a description that names the
cause outright -- 658 dev intents (11% of the corpus) reach the policy as UNKNOWN
while the disambiguating signal sits in free text one field away. That is the tail,
and closing it is worth real money because an UNKNOWN cause is priced at the
conservative chargeback rate and gets the cautious single-retry ladder.

WHAT IT CANNOT DO: reach the executor or the action set. It returns a FailureCause
and nothing else. The eligibility gate decides legality from that cause, and the
gate is the same code whether the cause came from the map, the model, or a coin
flip. tests/test_normalizer_containment.py proves it by forcing the resolver to
return every cause in the taxonomy and asserting the guardrail counters stay at 0.

MODEL NOTES (from the current API docs, not recall):
  * `temperature` is REJECTED on claude-opus-5 -- it returns 400. Determinism comes
    from the response schema plus the input-hash cache, not from a sampling knob.
    The audit record logs temperature=None with a note rather than a fabricated 0.0.
  * Output is constrained by `output_config.format` (json_schema), so the model
    cannot emit a cause outside the enum. `_validate` below is a second line of
    defence for a malformed, truncated, or errored response.
  * Requires a recent SDK: `pip install -U anthropic`. 0.30.0 predates all of this.
"""
from __future__ import annotations

import json
import pathlib
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from rr.normalize.prompt import (
    CONFIDENCE_LEVELS, PROMPT_VERSION, RESPONSE_SCHEMA, SYSTEM,
    prompt_hash, render_input, rendered_input_hash,
)
from rr.taxonomy import FailureCause


@dataclass(frozen=True)
class NormalizerConfig:
    # Weakest confidence still acted on. Anything weaker becomes UNKNOWN and takes
    # the existing conservative path -- the normaliser can fail to help, never harm.
    confidence_floor: str = "medium"
    # TODO(citation): list prices for claude-opus-5, USD per million tokens.
    # Verify against platform.claude.com/docs/en/pricing before quoting a cost.
    price_input_per_mtok: float = 5.00
    price_output_per_mtok: float = 25.00
    price_cache_read_per_mtok: float = 0.50
    model_id: str = "claude-opus-5"


NORMALIZER = NormalizerConfig()

TEMPERATURE_NOTE = (
    "claude-opus-5 rejects `temperature` with a 400; the parameter is not sent. "
    "Reproducibility comes from the enum-constrained response schema and the "
    "rendered_input_hash cache."
)


@dataclass
class LLMCall:
    """One row of the audit trail. Everything needed to re-run the call by hand."""
    purpose: str
    model_id: str
    prompt_version: str
    prompt_hash: str
    rendered_input_hash: str
    temperature: Optional[float]
    temperature_note: str
    raw_output: str
    parse_status: str          # ok | cached | below_floor | schema_violation | error
    resolved_cause: str
    confidence: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    request_id: Optional[str] = None
    cached: bool = False

    def as_row(self) -> dict:
        return asdict(self)


def _cost(cfg: NormalizerConfig, inp: int, out: int, cache_read: int) -> float:
    return (inp * cfg.price_input_per_mtok
            + out * cfg.price_output_per_mtok
            + cache_read * cfg.price_cache_read_per_mtok) / 1_000_000


def _validate(raw: str, err: Optional[str], cfg: NormalizerConfig
              ) -> tuple[FailureCause, str, str]:
    """Second line of defence behind the schema constraint.

    Anything that is not a clean, in-enum, at-or-above-floor answer becomes UNKNOWN,
    which routes to the same conservative path the map already used. The normaliser
    can fail to resolve; it cannot invent a cause outside the taxonomy."""
    if err:
        return FailureCause.UNKNOWN, "low", "error"
    try:
        parsed = json.loads(raw)
        cause = FailureCause(parsed["cause"])
        confidence = parsed["confidence"]
        if confidence not in CONFIDENCE_LEVELS:
            return FailureCause.UNKNOWN, "low", "schema_violation"
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return FailureCause.UNKNOWN, "low", "schema_violation"

    # CONFIDENCE_LEVELS is strongest-first, so a larger index is weaker.
    if CONFIDENCE_LEVELS.index(confidence) > CONFIDENCE_LEVELS.index(cfg.confidence_floor):
        return FailureCause.UNKNOWN, confidence, "below_floor"
    return cause, confidence, "ok"


@dataclass
class TailNormalizer:
    """Call once per DISTINCT gateway response; the cache absorbs the rest.

    The synthetic corpus contains only a handful of distinct descriptions, so the
    cache reduces the whole 6000-intent dev run to single-digit live calls. That is
    an artefact of the corpus, not a property of the real problem -- a production
    corpus has far more description diversity and a correspondingly higher bill.
    """
    resolver: object                       # AnthropicResolver | KeywordResolver
    cache_path: Optional[pathlib.Path] = None
    cfg: NormalizerConfig = NORMALIZER
    calls: list = field(default_factory=list)
    _cache: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.cache_path and self.cache_path.exists():
            self._cache = json.loads(self.cache_path.read_text())

    def resolve(self, event: dict) -> tuple[FailureCause, str, LLMCall]:
        rendered = render_input(event)
        key = rendered_input_hash(rendered)

        if key in self._cache:
            hit = self._cache[key]
            call = LLMCall(**{**hit["call"], "cached": True, "parse_status": "cached",
                              "cost_usd": 0.0, "latency_ms": 0.0})
            self.calls.append(call)
            return FailureCause(hit["cause"]), hit["confidence"], call

        t0 = time.perf_counter()
        raw, usage, request_id, err = self.resolver.classify(
            SYSTEM, rendered, RESPONSE_SCHEMA)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        cause, confidence, status = _validate(raw, err, self.cfg)
        call = LLMCall(
            purpose="tail_normalizer",
            model_id=self.resolver.model_id,
            prompt_version=PROMPT_VERSION,
            prompt_hash=prompt_hash(),
            rendered_input_hash=key,
            temperature=None,
            temperature_note=TEMPERATURE_NOTE,
            raw_output=raw,
            parse_status=status,
            resolved_cause=cause.value,
            confidence=confidence,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cost_usd=_cost(self.cfg, usage.get("input_tokens", 0),
                           usage.get("output_tokens", 0),
                           usage.get("cache_read_input_tokens", 0)),
            latency_ms=latency_ms,
            request_id=request_id,
        )
        self.calls.append(call)
        self._cache[key] = {"cause": cause.value, "confidence": confidence,
                            "call": {**call.as_row(), "cached": False}}
        return cause, confidence, call

    # ------------------------------------------------------------------ audit --
    def save_cache(self) -> None:
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=1, sort_keys=True))

    def write_call_log(self, path: pathlib.Path) -> None:
        """JSONL sink for the eval harness, which stays on files and in-process.
        The pipeline path writes the same records to the `llm_call` table instead."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            for c in self.calls:
                fh.write(json.dumps(c.as_row(), sort_keys=True) + "\n")

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @property
    def live_calls(self) -> int:
        return sum(1 for c in self.calls if not c.cached)

    @property
    def distinct_inputs(self) -> int:
        return len(self._cache)
