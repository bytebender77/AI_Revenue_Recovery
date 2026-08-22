"""Resolvers behind one `classify` contract.

    classify(system, rendered, schema) -> (raw_output, usage, request_id, error)

Every resolver also exposes `model_id`, `temperature`, `temperature_note`, and
`prices`. Those four fields exist so the audit row records what the provider
ACTUALLY exposes rather than flattening providers to one convention:

    AnthropicResolver  temperature = None  (claude-opus-5 returns 400 for it)
    OpenAIResolver     temperature = 0.0   (supported, and set to 0 for determinism)
    KeywordResolver    temperature = None  (no model involved)

All three go through the identical `_validate` path in llm_tail.py -- closed enum,
confidence floor, UNKNOWN fallback -- and produce the identical `LLMCall` row.
There is exactly one validation path and one logging path in this package.
"""
from __future__ import annotations

import json
import os

from rr.normalize.llm_tail import NORMALIZER, TEMPERATURE_NOTE, Prices
from rr.taxonomy import FailureCause

C = FailureCause


class AnthropicResolver:
    """Enum-constrained structured output via `output_config.format`.

    No `temperature` is sent: claude-opus-5 rejects it with a 400. Effort is `low`
    because this is a short closed-set classification.
    """
    temperature = None
    temperature_note = TEMPERATURE_NOTE
    # TODO(citation): UNVERIFIED. platform.claude.com/docs/en/pricing
    prices = Prices(input_per_mtok=5.00, output_per_mtok=25.00, cache_read_per_mtok=0.50)

    def __init__(self, model_id: str = NORMALIZER.model_id, max_tokens: int = 512):
        import anthropic
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Use --resolver openai, or --resolver "
                "offline to exercise the pipeline without live calls.")
        self._client = anthropic.Anthropic()
        self.model_id = model_id
        self.max_tokens = max_tokens

    def classify(self, system: str, rendered: str, schema: dict):
        try:
            resp = self._client.messages.create(
                model=self.model_id,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": rendered}],
                output_config={"format": {"type": "json_schema", "schema": schema},
                               "effort": "low"},
            )
        except Exception as exc:
            return "", {}, None, f"{type(exc).__name__}: {exc}"

        usage = {"input_tokens": getattr(resp.usage, "input_tokens", 0),
                 "output_tokens": getattr(resp.usage, "output_tokens", 0),
                 "cache_read_input_tokens":
                     getattr(resp.usage, "cache_read_input_tokens", 0)}
        request_id = getattr(resp, "_request_id", None)
        if getattr(resp, "stop_reason", None) == "refusal":
            return "", usage, request_id, "refusal"
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return text, usage, request_id, None


class OpenAIResolver:
    """Enum-constrained Structured Outputs via `response_format.json_schema`.

    The schema in prompt.py is already strict-compatible -- every property is in
    `required` and `additionalProperties` is false -- so it is passed through
    unchanged rather than loosened. `strict: true` makes the enum a hard constraint
    at decode time, which is the OpenAI-side equivalent of the Anthropic path.

    Unlike claude-opus-5, OpenAI accepts `temperature`, so it is sent as 0 and the
    real value is what lands in the audit row.

    Verified against the installed SDK (openai 3.3.1) rather than recalled:
    `chat.completions.create` accepts `response_format`, `temperature`, and
    `max_completion_tokens`; usage is `prompt_tokens` / `completion_tokens` with
    cached reads under `prompt_tokens_details.cached_tokens`.
    """
    temperature = 0.0
    temperature_note = (
        "OpenAI exposes `temperature`; sent as 0.0 for determinism. Combined with "
        "the strict json_schema and the rendered_input_hash cache, repeat runs "
        "reproduce exactly.")
    # TODO(citation): UNVERIFIED. openai.com/api/pricing
    prices = Prices(input_per_mtok=2.50, output_per_mtok=10.00, cache_read_per_mtok=1.25)

    # The model must support Structured Outputs. Override when the account has a
    # newer one -- this default is not verified against the live model list.
    DEFAULT_MODEL = os.environ.get("RR_OPENAI_MODEL", "gpt-4o-2024-08-06")

    def __init__(self, model_id: str = None, max_tokens: int = 512):
        import openai
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Export it, or use --resolver offline "
                "to exercise the pipeline without live calls.")
        self._client = openai.OpenAI()
        self.model_id = model_id or self.DEFAULT_MODEL
        self.max_tokens = max_tokens

    def classify(self, system: str, rendered: str, schema: dict):
        try:
            resp = self._client.chat.completions.create(
                model=self.model_id,
                temperature=self.temperature,
                max_completion_tokens=self.max_tokens,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": rendered}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "failure_cause_classification",
                                    "strict": True, "schema": schema},
                },
            )
        except Exception as exc:
            hint = ""
            if "model" in str(exc).lower():
                hint = (" -- set RR_OPENAI_MODEL to a Structured-Outputs-capable "
                        "model available on your account")
            return "", {}, None, f"{type(exc).__name__}: {exc}{hint}"

        u = resp.usage
        cached = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        usage = {"input_tokens": (u.prompt_tokens or 0) - cached,
                 "output_tokens": u.completion_tokens or 0,
                 "cache_read_input_tokens": cached}
        request_id = getattr(resp, "id", None)

        choice = resp.choices[0]
        # A strict-schema run that hits the token ceiling returns truncated JSON;
        # surface it as an error so _validate demotes it to UNKNOWN rather than
        # letting a half-parsed object through.
        if choice.finish_reason == "length":
            return "", usage, request_id, "truncated_max_tokens"
        if getattr(choice.message, "refusal", None):
            return "", usage, request_id, f"refusal: {choice.message.refusal}"
        return choice.message.content or "", usage, request_id, None


# --------------------------------------------------------------------------- #
# Offline stand-in. NOT AN LLM.                                                 #
# --------------------------------------------------------------------------- #

_RULES: tuple[tuple[str, FailureCause, str], ...] = (
    ("do not honour",              C.DO_NOT_HONOUR,          "high"),
    ("avail bal low",              C.INSUFFICIENT_FUNDS,     "high"),
    ("sufficient balance",         C.INSUFFICIENT_FUNDS,     "high"),
    ("bank offline",               C.ISSUER_DOWNTIME,        "high"),
    ("temporarily unavailable",    C.ISSUER_DOWNTIME,        "high"),
    ("switch timeout",             C.GATEWAY_TIMEOUT,        "high"),
    ("did not respond",            C.GATEWAY_TIMEOUT,        "high"),
    # UMRN is the NACH mandate reference; "not found / cancelled" is a dead mandate.
    ("umrn not found",             C.MANDATE_INVALID,        "high"),
    ("no longer active",           C.MANDATE_INVALID,        "high"),
    ("acct closed",                C.INVALID_CARD_DETAILS,   "high"),
    ("card has expired",           C.CARD_EXPIRED,           "high"),
    ("lost or stolen",             C.LOST_STOLEN_FRAUD,      "high"),
    ("risk evaluation",            C.RISK_DECLINE,           "high"),
    ("exceeds registered mandate", C.AMOUNT_EXCEEDS_MANDATE, "high"),
    ("limit exceeded",             C.LIMIT_EXCEEDED,         "high"),
    ("collect request expired",    C.UPI_COLLECT_EXPIRED,    "high"),
    ("authentication was not",     C.AUTH_FAILED,            "high"),
    ("not enabled",                C.METHOD_NOT_ENABLED,     "high"),
)


class KeywordResolver:
    """Deterministic keyword matcher standing in for a model when no key is set.

    `model_id` carries an explicit non-model marker so no audit row can be mistaken
    for a real LLM call."""
    model_id = "offline-keyword-resolver (NOT AN LLM)"
    temperature = None
    temperature_note = "No model involved; offline deterministic resolver."
    prices = Prices(input_per_mtok=0.0, output_per_mtok=0.0, cache_read_per_mtok=0.0)

    def classify(self, system: str, rendered: str, schema: dict):
        desc = ""
        for line in rendered.splitlines():
            if line.startswith("description: "):
                desc = line[len("description: "):].lower()
                break
        for phrase, cause, confidence in _RULES:
            if phrase in desc:
                return (json.dumps({"cause": cause.value, "confidence": confidence,
                                    "evidence": phrase}), {}, None, None)
        # No phrase matched -- the generic "Payment failed" lands here, correctly.
        return (json.dumps({"cause": C.UNKNOWN.value, "confidence": "low",
                            "evidence": ""}), {}, None, None)


def build_resolver(kind: str):
    if kind == "anthropic":
        return AnthropicResolver()
    if kind == "openai":
        return OpenAIResolver()
    if kind == "offline":
        return KeywordResolver()
    raise ValueError(f"unknown resolver kind: {kind}")
