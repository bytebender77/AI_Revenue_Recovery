"""Post-hoc explainer. Read-only over a committed decision record.

CONTAINMENT, same shape as the normaliser:
  * It is handed a record that has already been written and executed. It returns a
    string. It has no path to the action set, the executor, or the policy -- the
    package does not import them, and tests/test_explainer_containment.py greps for
    that and also deep-compares the record before and after to prove non-mutation.
  * `decision_reason_code` remains the source of truth. This renders it.
  * Delete the LLM and the audit trail is still complete: `templated()` produces a
    full account from the record alone, and is what every rejection falls back to.

THE VALIDATOR IS THE POINT. An explanation that cites a number the record does not
contain is worse than no explanation, because it reads as corroboration. So every
required fact is checked by string containment against the record's own formatting,
and anything that fails is discarded rather than shown. The rejection count is
reported, not hidden -- a disclosed nonzero rate is evidence the check runs.
"""
from __future__ import annotations

import pathlib
import time
from dataclasses import dataclass, field
from typing import Optional

from rr.explain.prompt import (
    PROMPT_VERSION, RESPONSE_SCHEMA, SYSTEM, prompt_hash, render_input,
    rendered_input_hash, required_facts, templated,
)
from rr.normalize.llm_tail import (
    LLMCall, MAX_FAILURES_PER_KEY, Prices, _cost, cache_key, load_cache,
    save_cache, write_calls,
)
import json


@dataclass
class Explanation:
    text: str
    source: str            # llm | templated_fallback | templated_no_resolver
    reject_reason: Optional[str] = None
    call: Optional[LLMCall] = None


def validate(text: str, record: dict) -> Optional[str]:
    """-> None if the explanation is faithful, else the reason it was rejected.

    Containment by string match against the record's own formatting. The prompt
    tells the model to quote these verbatim precisely so this check is possible."""
    if not text or len(text.split()) < 8:
        return "too_short"
    facts = required_facts(record)
    for name, value in facts.items():
        if value and value not in text:
            return f"missing_{name}"
    lowered = text.lower()
    for banned in ("i recommend", "instead we should", "you should retry"):
        if banned in lowered:
            return "recommends_alternative"
    return None


@dataclass
class Explainer:
    """Cache and audit log are the normaliser's, via the shared helpers in llm_tail.

    `resolver=None` is a supported production mode, not a test stub: it is the
    LLM-deleted configuration, and it still explains every decision."""
    resolver: object = None
    cache_path: Optional[pathlib.Path] = None
    purpose: str = "explainer"
    calls: list = field(default_factory=list)
    rejections: list = field(default_factory=list)
    stale_cache_discarded: bool = False
    _cache: dict = field(default_factory=dict)
    _failed: dict = field(default_factory=dict)

    def __post_init__(self):
        self._cache, self.stale_cache_discarded = load_cache(self.cache_path)

    # ------------------------------------------------------------------ core --
    def explain(self, record: dict) -> Explanation:
        if self.resolver is None:
            return Explanation(templated(record), "templated_no_resolver")

        rendered = render_input(record)
        input_hash = rendered_input_hash(rendered)
        key = cache_key(self.resolver.kind, self.resolver.model_id, input_hash)

        if self._failed.get(key, 0) >= MAX_FAILURES_PER_KEY:
            return Explanation(templated(record), "templated_fallback", "suppressed_after_failures")

        if key in self._cache:
            hit = self._cache[key]
            call = LLMCall(**{**hit["call"], "cached": True, "parse_status": "cached",
                              "cost_usd": 0.0, "latency_ms": 0.0})
            self.calls.append(call)
            if hit.get("rejected"):
                return Explanation(templated(record), "templated_fallback",
                                   hit["rejected"], call)
            return Explanation(hit["text"], "llm", None, call)

        t0 = time.perf_counter()
        raw, usage, request_id, err = self.resolver.classify(
            SYSTEM, rendered, RESPONSE_SCHEMA)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        text, status = "", "ok"
        if err:
            status = "error"
        else:
            try:
                text = json.loads(raw)["explanation"]
            except (json.JSONDecodeError, KeyError, TypeError):
                status = "schema_violation"

        reject = validate(text, record) if status == "ok" else status
        call = LLMCall(
            purpose=self.purpose, model_id=self.resolver.model_id,
            prompt_version=PROMPT_VERSION, prompt_hash=prompt_hash(),
            rendered_input_hash=input_hash,
            temperature=getattr(self.resolver, "temperature", None),
            temperature_note=self.resolver.temperature_note, raw_output=raw,
            parse_status=(reject or "ok"),
            resolved_cause=str(record.get("decision_reason_code", "")),
            confidence="n/a",
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_input_tokens", 0),
            cost_usd=_cost(self.resolver.prices, usage.get("input_tokens", 0),
                           usage.get("output_tokens", 0),
                           usage.get("cache_read_input_tokens", 0)),
            latency_ms=latency_ms, request_id=request_id)
        self.calls.append(call)

        if status == "error":
            self._failed[key] = self._failed.get(key, 0) + 1
            return Explanation(templated(record), "templated_fallback", "error", call)

        self._cache[key] = {"text": text, "rejected": reject,
                            "call": {**call.as_row(), "cached": False}}
        if reject:
            self.rejections.append({"payment_intent_id": record.get("payment_intent_id"),
                                    "slot": record.get("slot"), "reason": reject})
            return Explanation(templated(record), "templated_fallback", reject, call)
        return Explanation(text, "llm", None, call)

    # ------------------------------------------------------------------ audit --
    def save(self, log_path: Optional[pathlib.Path] = None) -> None:
        save_cache(self.cache_path, self._cache)
        if log_path:
            write_calls(log_path, self._cache)

    @property
    def rejection_rate(self) -> float:
        n = len([c for c in self.calls if not c.cached])
        return len(self.rejections) / n if n else 0.0

    @property
    def total_cost_usd(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @property
    def live_calls(self) -> int:
        return sum(1 for c in self.calls if not c.cached)
