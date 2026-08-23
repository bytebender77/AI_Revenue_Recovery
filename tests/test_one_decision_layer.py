"""One decision layer. The demo surface and the eval harness are the same claim.

There used to be two: `rr/pipeline/policy.py` (M2 rules port) behind `make run`, and
`rr/agent/policy.py` (M4 EV policy) behind every number. The video would have
demonstrated one system while the results described another.

This file is the evidence the blocker is closed. It runs one cohort through BOTH
entry points -- the eval harness in memory, the demo path through Postgres -- and
deep-compares the decision records field by field, including every candidate's
score, the binding constraint, and the reason code. Serialisation is included on
purpose: a record that survives the round-trip differently is not the same record.
"""
import json
import pathlib

import pytest

from rr import rng
from rr.agent.features import IssuerFailureIndex
from rr.agent.policy import EVPolicy
from rr.eval.agent_run import run_agent
from rr.model.beta_binomial import BetaBinomialModel
from rr.sim.adapter import SimAdapter
from rr.sim.cohort import load_observed

REPO = pathlib.Path(__file__).resolve().parents[1]
DATA, MODELS = REPO / "data", REPO / "models"
N = 200

# Any live import of the retired module would mean two layers again.
LIVE_PACKAGES = ("pipeline", "agent", "eval", "normalize", "explain", "model", "sim")


def test_retired_rules_port_is_unreachable_from_every_entry_point():
    for pkg in LIVE_PACKAGES:
        root = REPO / "rr" / pkg
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            src = py.read_text()
            assert "rr.attic" not in src, f"{py} imports the retired rules port"
            assert "pipeline.policy" not in src, f"{py} imports the retired rules port"


def test_only_one_module_scores_candidates():
    """`score_basis` is the tell. If anything still emits rule_priority, the old
    layer is alive somewhere."""
    emitters = ('SCORE_BASIS = "rule_priority"', 'score_basis="rule_priority"',
                "score_basis='rule_priority'", '"score_basis": "rule_priority"')
    offenders = []
    for pkg in LIVE_PACKAGES:
        root = REPO / "rr" / pkg
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            src = py.read_text()
            if any(e in src for e in emitters):
                offenders.append(str(py.relative_to(REPO)))
    assert not offenders, f"rule_priority still emitted by {offenders}"


@pytest.fixture(scope="module")
def fixtures():
    if not (MODELS / "success_model_v1.json").exists():
        pytest.skip("run `make train` first")
    obs_all = load_observed(DATA / "dev_observed.jsonl")
    sample = sorted(obs_all, key=lambda o: rng.u01(o["intent_id"], "demo"))[:N]
    sample.sort(key=lambda o: o["failed_at_h"])
    adapter = SimAdapter.from_cohort(DATA, "dev")
    latents = [adapter._lat[o["intent_id"]] for o in sample]
    policy = lambda: EVPolicy(
        BetaBinomialModel.load(MODELS / "success_model_v1.json"),
        BetaBinomialModel.load(MODELS / "organic_model_v2.json"),
        issuer_index=IssuerFailureIndex.build(obs_all))
    arm_of = lambda iid: "treatment" if rng.u01(iid, "arm") < 0.5 else "control"
    return sample, latents, policy, arm_of


def _normalise(row: dict) -> dict:
    """Round-trip both sides through JSON so float and key-order differences from
    Postgres jsonb cannot masquerade as agreement or as disagreement."""
    return json.loads(json.dumps(row, sort_keys=True, default=str))


def test_both_entry_points_produce_identical_decisions(fixtures):
    sample, latents, policy, arm_of = fixtures
    psycopg = pytest.importorskip("psycopg")
    from rr.db.conn import connect
    from rr.pipeline.runner import PostgresSink

    # Entry point A: the eval harness. No sink, decisions kept in memory.
    _eval_results, eval_meta = run_agent(sample, latents, policy(), arm_of=arm_of)
    # Keyed on decision_seq, not slot: a deferred action is re-decided at the same
    # slot when its tick arrives, so (intent, slot) is not unique.
    from_eval = {(d["payment_intent_id"], d["decision_seq"]): d
                 for d in eval_meta["decisions"]}

    # Entry point B: the demo path. Same loop, Postgres sink.
    run_id = "equivalence_probe"
    try:
        conn = connect()
    except Exception as exc:
        pytest.skip(f"no database ({exc}); run `make db-init`")
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM ledger_entry WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM decision WHERE run_id=%s", (run_id,))
            cur.execute("DELETE FROM failure_event WHERE run_id=%s", (run_id,))
        run_agent(sample, latents, policy(),
                  sink=PostgresSink(conn, run_id, arm_of), arm_of=arm_of)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                """SELECT d.payment_intent_id, d.decision_seq, d.slot, d.arm, d.chosen_action,
                          d.chosen_channel, d.scheduled_for_h, d.candidate_set,
                          d.score_basis, d.decision_reason_code, d.binding_constraint,
                          d.taxonomy_version, d.model_version, d.policy_version
                   FROM decision d WHERE d.run_id = %s ORDER BY d.id""", (run_id,))
            cols = [c[0] for c in cur.description]
            from_db = {(r[0], r[1]): dict(zip(cols, r)) for r in cur.fetchall()}

    assert from_db, "the demo path wrote no decisions"
    assert set(from_eval) == set(from_db), (
        f"different decisions were made: "
        f"eval-only={sorted(set(from_eval) - set(from_db))[:5]} "
        f"db-only={sorted(set(from_db) - set(from_eval))[:5]}")

    for key in sorted(from_eval):
        a, b = _normalise(from_eval[key]), _normalise(from_db[key])
        for field in ("slot", "arm", "chosen_action", "chosen_channel", "score_basis",
                      "decision_reason_code", "binding_constraint",
                      "taxonomy_version", "model_version", "policy_version"):
            assert a[field] == b[field], f"{key} {field}: eval={a[field]} db={b[field]}"
        assert a["candidate_set"] == b["candidate_set"], (
            f"{key}: candidate_set differs -- scores, permissions or blocked_by "
            f"do not match between entry points")


def test_shipping_config_versions_are_stamped(fixtures):
    """Every decision row must name the code that produced it, and it must be the
    shipping config -- not the retired policy, not the losing feature cross."""
    from rr.versions import EV_POLICY_VERSION, MODEL_VERSION, TAXONOMY_VERSION
    sample, latents, policy, arm_of = fixtures
    _results, meta = run_agent(sample, latents, policy(), arm_of=arm_of)
    rows = meta["decisions"]
    assert rows
    for d in rows:
        assert d["policy_version"] == EV_POLICY_VERSION == "ev-policy-v1.0.0"
        assert d["model_version"] == MODEL_VERSION == "beta-binomial-featurecross-v1.0.0"
        assert d["taxonomy_version"] == TAXONOMY_VERSION
        assert d["score_basis"] == "expected_net_value"


def test_treatment_decisions_carry_real_ev_evidence(fixtures):
    """Not rule_priority: an actual p_success from a named cell with an
    observation count, and an expected-net breakdown."""
    sample, latents, policy, arm_of = fixtures
    _results, meta = run_agent(sample, latents, policy(), arm_of=arm_of)
    acted = [d for d in meta["decisions"]
             if d["arm"] == "treatment" and d["chosen_action"] != "no_action"]
    assert acted, "no treatment decision acted"
    chosen = next(c for c in acted[0]["candidate_set"] if c["chosen"])
    assert "p_mean" in chosen["evidence"] and "observations" in chosen["evidence"]
    assert set(chosen["components_inr"]) >= {"value", "cost", "objective"}
    assert chosen["ev_ci90_inr"][0] <= chosen["score"] <= chosen["ev_ci90_inr"][1]
