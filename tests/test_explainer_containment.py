"""The explainer renders a committed decision. It cannot change one.

Three proofs:
  STATIC      -- rr/explain/ never imports the executor, adapters, or the action
                 vocabulary. It cannot name an action, so it cannot pick one.
  NON-MUTATION-- the record is deep-compared before and after explaining, including
                 under a resolver that returns hostile text.
  DELETABLE   -- with resolver=None the audit trail is still complete. The LLM buys
                 readability, not content.
"""
import copy
import json
import pathlib

import pytest

from rr.explain.explainer import Explainer, validate
from rr.explain.prompt import required_facts, templated
from rr.normalize.llm_tail import Prices

REPO = pathlib.Path(__file__).resolve().parents[1]
FORBIDDEN = ("rr.pipeline.executor", "rr.adapters", "ActionSpec", "ActionType",
             "apply_action", "ExecutionRequest", "EVPolicy")

RECORD = {
    "payment_intent_id": "dev_pi_004242",
    "slot": 1,
    "arm": "treatment",
    "chosen_action": "retry_same",
    "chosen_channel": None,
    "scheduled_for_h": 74.0,
    "decision_reason_code": "POSITIVE_EXPECTED_NET_VALUE",
    "binding_constraint": "R011_capacity_auction_lost",
    "score_basis": "expected_net_value",
    "policy_version": "ev-policy-v1.0.0",
    "model_version": "beta-binomial-featurecross-v1.0.0",
    "candidate_set": [
        {"action": "retry_same", "channel": None, "score": 412.55, "permitted": True,
         "blocked_by": None, "chosen": True,
         "evidence": {"p_mean": 0.2617, "observations": 143}},
        {"action": "escalate_human", "channel": None, "score": 980.10,
         "permitted": False, "blocked_by": "R011_capacity_auction_lost",
         "chosen": False, "evidence": {"p_mean": 0.3100, "observations": 61}},
        {"action": "no_action", "channel": None, "score": 0.0, "permitted": True,
         "blocked_by": None, "chosen": False, "evidence": {}},
    ],
}


class StubResolver:
    kind, model_id = "test", "stub-explainer"
    temperature, temperature_note = 0.0, "test"
    prices = Prices(input_per_mtok=0.0, output_per_mtok=0.0)

    def __init__(self, text):
        self.text = text

    def classify(self, system, rendered, schema):
        return json.dumps({"explanation": self.text}), {}, None, None


FAITHFUL = ("The agent chose retry_same for this payment. The success model put "
            "p_success at 0.2617 giving an expected net value of 412.55, the best "
            "of the permitted options. A higher-scoring escalation was unavailable "
            "because of R011_capacity_auction_lost.")


def test_explain_package_cannot_name_an_action():
    for py in (REPO / "rr" / "explain").rglob("*.py"):
        src = py.read_text()
        for token in FORBIDDEN:
            assert token not in src, f"{py} references {token}"


@pytest.mark.parametrize("text", [
    FAITHFUL,
    "I recommend retry_same at 0.2617 for 412.55 despite R011_capacity_auction_lost.",
    "Nope.",
    '{"chosen_action": "escalate_human"}',
])
def test_record_is_never_mutated(text):
    """Including under hostile output that tries to restate the decision."""
    record = copy.deepcopy(RECORD)
    before = json.dumps(record, sort_keys=True)
    Explainer(StubResolver(text)).explain(record)
    assert json.dumps(record, sort_keys=True) == before


def test_faithful_explanation_is_accepted():
    e = Explainer(StubResolver(FAITHFUL)).explain(copy.deepcopy(RECORD))
    assert e.source == "llm" and e.reject_reason is None


@pytest.mark.parametrize("text,reason", [
    # Hallucinated probability -- the single most dangerous failure, because a
    # wrong number in prose reads as corroboration of the decision.
    ("Chose retry_same with p_success 0.9000 and expected net 412.55 "
     "despite R011_capacity_auction_lost being binding here.", "missing_p_success"),
    ("Chose retry_same with p_success 0.2617 and expected net 999.99 "
     "despite R011_capacity_auction_lost being binding here.", "missing_expected_net"),
    ("Chose retry_same with p_success 0.2617 and expected net 412.55 "
     "with nothing blocking any higher option at all.", "missing_binding_constraint"),
    ("Chose escalate_human with p 0.2617 net 412.55 R011_capacity_auction_lost.",
     "missing_action"),
    ("Too short.", "too_short"),
])
def test_unfaithful_explanations_are_rejected_and_fall_back(text, reason):
    ex = Explainer(StubResolver(text))
    out = ex.explain(copy.deepcopy(RECORD))
    assert out.reject_reason == reason
    assert out.source == "templated_fallback"
    # The fallback is complete, not a stub.
    for value in required_facts(RECORD).values():
        assert value in out.text
    assert ex.rejection_rate == 1.0


def test_recommending_an_alternative_is_rejected():
    text = FAITHFUL + " I recommend escalate_human next time."
    out = Explainer(StubResolver(text)).explain(copy.deepcopy(RECORD))
    assert out.reject_reason == "recommends_alternative"


def test_audit_trail_is_complete_with_the_llm_deleted():
    """resolver=None is the LLM-deleted configuration, not a stub."""
    out = Explainer(resolver=None).explain(copy.deepcopy(RECORD))
    assert out.source == "templated_no_resolver"
    for value in required_facts(RECORD).values():
        assert value in out.text
    assert RECORD["decision_reason_code"] in out.text
    assert RECORD["policy_version"] in out.text


def test_required_facts_adapt_to_a_record_without_a_success_model():
    """An M2-era rule_priority record has no p_success; requiring one would fail
    every explanation for a reason that is not the model's fault."""
    rec = copy.deepcopy(RECORD)
    rec["score_basis"] = "rule_priority"
    for c in rec["candidate_set"]:
        c.pop("evidence", None)
    facts = required_facts(rec)
    assert "p_success" not in facts and "expected_net" in facts
    assert validate(templated(rec), rec) is None


def test_cache_is_keyed_by_resolver_identity_like_the_normalizer():
    shared = {}
    a = Explainer(StubResolver(FAITHFUL)); a._cache = shared
    b = Explainer(StubResolver(FAITHFUL)); b._cache = shared
    b.resolver.model_id = "different-model"
    a.explain(copy.deepcopy(RECORD))
    out = b.explain(copy.deepcopy(RECORD))
    assert len(shared) == 2 and not out.call.cached
