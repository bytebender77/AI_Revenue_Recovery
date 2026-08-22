"""Append-only, hash-chained ledger.

entry_hash = sha256(prev_hash || payload_hash || event || entity_type)

One global chain across every run, so a single pass verifies everything. A row
mutated or removed after the fact breaks the chain at that point and every entry
after it. The table also carries a BEFORE UPDATE OR DELETE trigger, so tampering
requires disabling a database object rather than issuing a quiet UPDATE.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

GENESIS = "0" * 64


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(_canonical(payload).encode()).hexdigest()


def entry_hash(prev: str, p_hash: str, event: str, entity_type: str) -> str:
    return hashlib.sha256(f"{prev}{p_hash}{event}{entity_type}".encode()).hexdigest()


def append(cur, run_id: str, entity_type: str, entity_id: Optional[int],
           payment_intent_id: Optional[str], event: str, actor: str, payload: dict) -> str:
    cur.execute("SELECT entry_hash FROM ledger_entry ORDER BY seq DESC LIMIT 1")
    row = cur.fetchone()
    prev = row[0] if row else GENESIS
    p_hash = payload_hash(payload)
    e_hash = entry_hash(prev, p_hash, event, entity_type)
    cur.execute(
        """INSERT INTO ledger_entry (run_id, payment_intent_id, entity_type, entity_id,
               event, actor, payload, payload_hash, prev_hash, entry_hash)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (run_id, payment_intent_id, entity_type, entity_id, event, actor,
         _canonical(payload), p_hash, prev, e_hash),
    )
    return e_hash


def verify(conn) -> tuple[bool, int, Optional[str]]:
    """Recompute the whole chain. Returns (ok, entries_checked, first_bad_reason)."""
    prev = GENESIS
    checked = 0
    with conn.cursor() as cur:
        cur.execute("""SELECT seq, entity_type, event, payload, payload_hash, prev_hash, entry_hash
                       FROM ledger_entry ORDER BY seq ASC""")
        for seq, entity_type, event, payload, p_hash, stored_prev, stored_entry in cur:
            checked += 1
            if stored_prev != prev:
                return False, checked, f"seq {seq}: prev_hash does not match the previous entry"
            if payload_hash(payload) != p_hash:
                return False, checked, f"seq {seq}: payload was modified after writing"
            if entry_hash(prev, p_hash, event, entity_type) != stored_entry:
                return False, checked, f"seq {seq}: entry_hash does not match its contents"
            prev = stored_entry
    return True, checked, None
