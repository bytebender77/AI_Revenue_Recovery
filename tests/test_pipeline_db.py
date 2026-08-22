"""Postgres-backed invariants: idempotency, fire-time revalidation, chain integrity.

Skipped when no database is reachable, so `make test` still works without docker.
"""
import json

import pytest

psycopg = pytest.importorskip("psycopg")

from rr.db import ledger
from rr.db.conn import connect


@pytest.fixture(scope="module")
def db():
    try:
        conn = connect()
    except Exception as exc:                       # pragma: no cover
        pytest.skip(f"no database reachable ({exc}); run `make db-init`")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM ledger_entry")
        if cur.fetchone()[0] == 0:
            pytest.skip("no ledger entries; run `make run` first")
    yield conn
    conn.rollback()
    conn.close()


def test_chain_verifies_end_to_end(db):
    ok, checked, reason = ledger.verify(db)
    assert ok, reason
    assert checked > 0


def test_ledger_rejects_update_and_delete(db):
    for stmt in ("UPDATE ledger_entry SET actor='forged' WHERE seq=1",
                 "DELETE FROM ledger_entry WHERE seq=1"):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with db.cursor() as cur:
                cur.execute(stmt)
        db.rollback()


def test_verify_detects_a_payload_edited_behind_the_trigger(db):
    """Disabling the trigger is the only way to mutate a row. The chain still catches it."""
    with db.cursor() as cur:
        cur.execute("ALTER TABLE ledger_entry DISABLE TRIGGER ledger_no_mutation")
        cur.execute("""UPDATE ledger_entry SET payload = payload || '{"forged": true}'::jsonb
                       WHERE seq = (SELECT min(seq) FROM ledger_entry)""")
    ok, checked, reason = ledger.verify(db)
    db.rollback()
    assert not ok
    assert "modified" in reason


def test_every_attempt_is_idempotent(db):
    with db.cursor() as cur:
        cur.execute("SELECT count(*), count(DISTINCT idempotency_key) FROM attempt")
        total, distinct = cur.fetchone()
    assert total == distinct, "an action executed more than once for the same decision"


def test_revalidation_is_logged_on_every_attempt(db):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM attempt WHERE revalidation_result IN ('', 'pending')")
        assert cur.fetchone()[0] == 0
        cur.execute("""SELECT count(*) FROM attempt
                       WHERE revalidation_result <> 'ok' AND execution_status <> 'aborted'""")
        assert cur.fetchone()[0] == 0, "an attempt executed despite failing revalidation"


def test_no_unauthorized_debit_reached_the_gateway(db):
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM attempt WHERE outcome='unauthorized_debit_rejected'")
        assert cur.fetchone()[0] == 0, "the gate let a re-debit through without a mandate"


def test_every_decision_carries_its_version_triple_and_full_candidate_set(db):
    with db.cursor() as cur:
        cur.execute("""SELECT count(*) FROM decision
                       WHERE taxonomy_version IS NULL OR model_version IS NULL
                          OR policy_version IS NULL OR jsonb_array_length(candidate_set) = 0""")
        assert cur.fetchone()[0] == 0
        # The rejected options must be present, not only the winner.
        cur.execute("""SELECT count(*) FROM decision
                       WHERE arm='treatment' AND jsonb_array_length(candidate_set) < 2""")
        assert cur.fetchone()[0] == 0


def test_control_arm_never_acts(db):
    with db.cursor() as cur:
        cur.execute("""SELECT count(*) FROM decision d JOIN attempt a ON a.decision_id = d.id
                       WHERE d.arm = 'control'""")
        assert cur.fetchone()[0] == 0
