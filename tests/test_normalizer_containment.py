"""The normaliser feeds diagnosis and nothing else.

Two independent proofs, because either alone is weak:

  STATIC  -- rr/normalize/ must not import the executor, the adapters, or the
             action vocabulary. If it cannot name an action, it cannot pick one.
  DYNAMIC -- force the resolver to return EVERY cause in the taxonomy, including
             the terminal ones, and assert the guardrail counters stay at zero.
             This is the one that matters: it shows the gate, not the model,
             decides legality. A resolver that claims `insufficient_funds` for a
             genuinely revoked mandate cannot authorise a re-debit, because the
             regime and cap rules still apply and the sim still rejects it.
"""
import dataclasses
import json
import pathlib

import pytest

from rr.agent.policy import EVPolicy
from rr.config import CLOCK, CohortConfig
from rr.eval.agent_run import run_agent
from rr.model.beta_binomial import BetaBinomialModel
from rr.normalize.llm_tail import NORMALIZER, Prices, TailNormalizer, _validate
from rr.sim.cohort import generate_cohort
from rr.taxonomy import FailureCause

REPO = pathlib.Path(__file__).resolve().parents[1]
FORBIDDEN = ("rr.pipeline.executor", "rr.adapters", "ActionSpec", "ActionType",
             "apply_action", "ExecutionRequest")


class FixedResolver:
    """Returns one chosen cause for everything, at high confidence.

    Implements the full resolver contract -- model_id, temperature,
    temperature_note, prices -- so it exercises the same audit path a real
    provider does."""
    kind = "test"
    model_id = "test-fixed-resolver"
    temperature = None
    temperature_note = "test; claude-opus-5 returns 400 for temperature"
    prices = Prices(input_per_mtok=0.0, output_per_mtok=0.0)

    def __init__(self, cause: FailureCause, kind: str = None, model_id: str = None):
        self.cause = cause
        if kind:
            self.kind = kind
        if model_id:
            self.model_id = model_id

    def classify(self, system, rendered, schema):
        return (json.dumps({"cause": self.cause.value, "confidence": "high",
                            "evidence": "test"}), {}, None, None)


@pytest.fixture(scope="module")
def cohort():
    cfg = dataclasses.replace(
        CohortConfig(), n_dev=700, n_merchants_dev=5,
        n_adv_fast_organic=25, n_adv_soft_mask_terminal=20, n_adv_outage_midwindow=20)
    intents = generate_cohort("dev", cfg, cfg.n_dev, cfg.n_merchants_dev,
                              CLOCK.dev_hours, "dev")
    obs = [dataclasses.asdict(i.observed) for i in intents]
    for o in obs:
        o["method"], o["regime"] = o["method"].value, o["regime"].value
        o["consented_channels"] = [c.value for c in o["consented_channels"]]
    return obs, [i.latent for i in intents]


@pytest.fixture(scope="module")
def policy():
    p = REPO / "models"
    if not (p / "success_model_v1.json").exists():
        pytest.skip("run `make train` first")
    return lambda: EVPolicy(BetaBinomialModel.load(p / "success_model_v1.json"),
                            BetaBinomialModel.load(p / "organic_model_v2.json"))


def test_normalizer_package_cannot_name_an_action():
    for py in (REPO / "rr" / "normalize").rglob("*.py"):
        src = py.read_text()
        for token in FORBIDDEN:
            assert token not in src, f"{py} references {token}"


@pytest.mark.parametrize("cause", list(FailureCause))
def test_no_forced_cause_can_breach_the_gate(cause, cohort, policy):
    """Every cause, including the terminal set and the most permissive soft cause."""
    tail = TailNormalizer(FixedResolver(cause))
    res, _ = run_agent(*cohort, policy(), normalizer=tail)
    assert sum(r.never_retry_violations_observable for r in res) == 0, (
        f"gate leaked when the resolver claimed {cause.value}")
    assert sum(r.unauthorized_debit_rejected for r in res) == 0, (
        f"an unmandated re-debit reached the gateway when the resolver "
        f"claimed {cause.value}")


def test_confidence_floor_demotes_to_unknown():
    below = json.dumps({"cause": "insufficient_funds", "confidence": "low",
                        "evidence": "x"})
    cause, _, status = _validate(below, None, NORMALIZER)
    assert cause is FailureCause.UNKNOWN and status == "below_floor"


def test_malformed_and_errored_output_become_unknown():
    for raw, err in (("not json at all", None), ('{"cause": "not_a_cause"}', None),
                     ("", "refusal"), ("", "APIConnectionError")):
        cause, _, status = _validate(raw, err, NORMALIZER)
        assert cause is FailureCause.UNKNOWN
        assert status in ("schema_violation", "error")


def test_cache_makes_repeat_resolution_free_and_identical():
    tail = TailNormalizer(FixedResolver(FailureCause.DO_NOT_HONOUR))
    event = {"gateway_code": "BAD_REQUEST_ERROR", "gateway_reason": "payment_failed",
             "gateway_source": "bank", "gateway_step": "payment_authorization",
             "method": "card_mandate",
             "gateway_description": "Declined by issuing bank (do not honour)"}
    first = tail.resolve(event)
    second = tail.resolve(event)
    assert first[0] is second[0]
    assert tail.live_calls == 1 and len(tail.calls) == 2
    assert second[2].cached and second[2].cost_usd == 0.0


EVENT = {"gateway_code": "BAD_REQUEST_ERROR", "gateway_reason": "payment_failed",
         "gateway_source": "bank", "gateway_step": "payment_authorization",
         "method": "card_mandate",
         "gateway_description": "Declined by issuing bank (do not honour)"}


def test_one_providers_cache_entry_never_satisfies_another():
    """The cache key is (resolver kind, model_id, input hash), not the input alone.

    Keying on the input alone let an offline keyword answer serve an `openai`
    request: the run reported zero live calls while the audit row carried an
    OpenAI model_id over an answer no model produced. That is an audit-integrity
    failure, not a caching nicety -- it attributes one model's output to another."""
    shared = {}
    a = TailNormalizer(FixedResolver(FailureCause.DO_NOT_HONOUR,
                                     kind="offline", model_id="keyword"))
    b = TailNormalizer(FixedResolver(FailureCause.INSUFFICIENT_FUNDS,
                                     kind="openai", model_id="gpt-4o-2024-08-06"))
    a._cache = b._cache = shared          # deliberately the SAME cache object

    cause_a, _, call_a = a.resolve(EVENT)
    cause_b, _, call_b = b.resolve(EVENT)

    assert len(shared) == 2, "same input under two resolvers collapsed to one entry"
    assert cause_a is FailureCause.DO_NOT_HONOUR
    assert cause_b is FailureCause.INSUFFICIENT_FUNDS, "cross-provider cache hit"
    assert not call_b.cached, "the second resolver was served a foreign cache entry"
    # Same input, so the input hash matches -- it is the identity prefix that differs.
    assert call_a.rendered_input_hash == call_b.rendered_input_hash
    assert call_a.model_id != call_b.model_id
    assert a.live_calls == 1 and b.live_calls == 1


def test_same_provider_different_model_is_also_a_distinct_entry():
    shared = {}
    old = TailNormalizer(FixedResolver(FailureCause.DO_NOT_HONOUR,
                                       kind="openai", model_id="gpt-4o-2024-08-06"))
    new = TailNormalizer(FixedResolver(FailureCause.ISSUER_DOWNTIME,
                                       kind="openai", model_id="gpt-5-hypothetical"))
    old._cache = new._cache = shared
    old.resolve(EVENT)
    _, _, call = new.resolve(EVENT)
    assert len(shared) == 2 and not call.cached


def test_stale_cache_format_is_discarded_not_partially_trusted(tmp_path):
    """A format-1 file must not read as a cold cache; it must be visibly discarded."""
    p = tmp_path / "cache.json"
    p.write_text(json.dumps({"1438fb4908b754a0": {"cause": "do_not_honour",
                                                  "confidence": "high", "call": {}}}))
    tail = TailNormalizer(FixedResolver(FailureCause.DO_NOT_HONOUR), cache_path=p)
    assert tail.stale_cache_discarded and tail.distinct_inputs == 0


def test_every_call_is_audit_complete(tmp_path):
    tail = TailNormalizer(FixedResolver(FailureCause.ISSUER_DOWNTIME))
    tail.resolve({"gateway_code": "NPCI_XR", "gateway_reason": "xr",
                  "gateway_source": "bank", "gateway_step": "payment_authorization",
                  "method": "upi_autopay",
                  "gateway_description": "remitter bank offline, try after some time"})
    out = tmp_path / "calls.jsonl"
    tail.write_call_log(out)
    row = json.loads(out.read_text().splitlines()[0])
    for field in ("prompt_hash", "rendered_input_hash", "model_id", "raw_output",
                  "parse_status", "input_tokens", "output_tokens", "cost_usd",
                  "latency_ms", "temperature", "temperature_note"):
        assert field in row
    # temperature is logged as null with a note -- the model does not expose it.
    assert row["temperature"] is None and "400" in row["temperature_note"]
