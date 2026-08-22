"""Two resolvers behind one `classify` contract.

    classify(system, rendered, schema) -> (raw_output, usage, request_id, error)

AnthropicResolver is the real one. KeywordResolver is a deterministic offline
stand-in so the ablation runs without an API key -- IT IS NOT AN LLM, and any
number produced with it must be labelled as such. It exists to exercise the
plumbing (enum constraint, confidence floor, cache, audit log) and to give an
upper-bound estimate of what a competent model would do on these exact strings,
which are few and unambiguous.
"""
from __future__ import annotations

import json
import os
from typing import Optional

from rr.normalize.llm_tail import NORMALIZER
from rr.taxonomy import FailureCause

C = FailureCause


class AnthropicResolver:
    """Enum-constrained structured output via `output_config.format`.

    No `temperature` is sent: claude-opus-5 rejects it with a 400. Effort is `low`
    because this is a short closed-set classification -- see the skill guidance
    preferring low effort over disabling thinking on this model.
    """
    def __init__(self, model_id: str = NORMALIZER.model_id, max_tokens: int = 512):
        import anthropic  # imported lazily so the offline path needs no SDK
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Run the ablation with --resolver offline "
                "to exercise the pipeline without live calls, and label the numbers.")
        self._client = anthropic.Anthropic()
        self.model_id = model_id
        self.max_tokens = max_tokens

    @property
    def temperature_note(self) -> str:
        from rr.normalize.llm_tail import TEMPERATURE_NOTE
        return TEMPERATURE_NOTE

    def classify(self, system: str, rendered: str, schema: dict):
        try:
            resp = self._client.messages.create(
                model=self.model_id,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": rendered}],
                output_config={
                    "format": {"type": "json_schema", "schema": schema},
                    "effort": "low",
                },
            )
        except Exception as exc:                       # network, 4xx, 5xx
            return "", {}, None, f"{type(exc).__name__}: {exc}"

        usage = {
            "input_tokens": getattr(resp.usage, "input_tokens", 0),
            "output_tokens": getattr(resp.usage, "output_tokens", 0),
            "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
        }
        request_id = getattr(resp, "_request_id", None)

        # A safety refusal is a valid outcome, not an exception. It becomes UNKNOWN.
        if getattr(resp, "stop_reason", None) == "refusal":
            return "", usage, request_id, "refusal"

        text = next((b.text for b in resp.content if b.type == "text"), "")
        return text, usage, request_id, None


# --------------------------------------------------------------------------- #
# Offline stand-in. NOT AN LLM.                                                 #
# --------------------------------------------------------------------------- #

# Ordered: first match wins. Phrases are the ones this corpus actually contains.
_RULES: tuple[tuple[str, FailureCause, str], ...] = (
    ("do not honour",              C.DO_NOT_HONOUR,         "high"),
    ("avail bal low",              C.INSUFFICIENT_FUNDS,    "high"),
    ("sufficient balance",         C.INSUFFICIENT_FUNDS,    "high"),
    ("bank offline",               C.ISSUER_DOWNTIME,       "high"),
    ("temporarily unavailable",    C.ISSUER_DOWNTIME,       "high"),
    ("switch timeout",             C.GATEWAY_TIMEOUT,       "high"),
    ("did not respond",            C.GATEWAY_TIMEOUT,       "high"),
    # UMRN is the NACH mandate reference; "not found / cancelled" is a dead mandate.
    ("umrn not found",             C.MANDATE_INVALID,       "high"),
    ("no longer active",           C.MANDATE_INVALID,       "high"),
    ("acct closed",                C.INVALID_CARD_DETAILS,  "high"),
    ("card has expired",           C.CARD_EXPIRED,          "high"),
    ("lost or stolen",             C.LOST_STOLEN_FRAUD,     "high"),
    ("risk evaluation",            C.RISK_DECLINE,          "high"),
    ("exceeds registered mandate", C.AMOUNT_EXCEEDS_MANDATE, "high"),
    ("limit exceeded",             C.LIMIT_EXCEEDED,        "high"),
    ("collect request expired",    C.UPI_COLLECT_EXPIRED,   "high"),
    ("authentication was not",     C.AUTH_FAILED,           "high"),
    ("not enabled",                C.METHOD_NOT_ENABLED,    "high"),
)


class KeywordResolver:
    """Deterministic keyword matcher standing in for the model when no key is set.

    Reports `model_id` as an explicit non-model marker so no audit row can be
    mistaken for a real LLM call.
    """
    model_id = "offline-keyword-resolver (NOT AN LLM)"

    @property
    def temperature_note(self) -> str:
        return "No model involved; offline deterministic resolver."

    def classify(self, system: str, rendered: str, schema: dict):
        desc = ""
        for line in rendered.splitlines():
            if line.startswith("description: "):
                desc = line[len("description: "):].lower()
                break

        for phrase, cause, confidence in _RULES:
            if phrase in desc:
                return (json.dumps({"cause": cause.value, "confidence": confidence,
                                    "evidence": phrase}),
                        {}, None, None)

        # No phrase matched -- the generic "Payment failed" lands here, correctly.
        return (json.dumps({"cause": C.UNKNOWN.value, "confidence": "low",
                            "evidence": ""}),
                {}, None, None)


def build_resolver(kind: str):
    if kind == "anthropic":
        return AnthropicResolver()
    if kind == "offline":
        return KeywordResolver()
    raise ValueError(f"unknown resolver kind: {kind}")
