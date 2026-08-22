"""Accept a failure event, dedupe to the payment-intent key, persist raw + parsed.

The raw payload is stored verbatim and never reparsed. When the taxonomy or the
normaliser changes, past decisions can be re-derived from what actually arrived
rather than from what an earlier version of the code happened to keep.
"""
from __future__ import annotations

import json

from rr.db import ledger

FIELDS = ("merchant_id", "customer_id", "issuer_id", "amount_minor", "currency",
          "method", "regime", "mandate_id", "instrument_ref", "gateway_code",
          "gateway_reason", "gateway_source", "gateway_step", "gateway_description",
          "failed_at_h", "attempt_index")


def dedupe_key(raw: dict) -> str:
    """One payment intent, one attempt index, one event. Replays collapse."""
    return f"{raw['intent_id']}#{raw['attempt_index']}"


def ingest(cur, run_id: str, raw: dict, arm: str) -> tuple[int, dict, bool]:
    key = dedupe_key(raw)
    cols = ", ".join(FIELDS)
    ph = ", ".join(["%s"] * len(FIELDS))
    cur.execute(
        f"""INSERT INTO failure_event (run_id, payment_intent_id, dedupe_key,
                raw_payload, arm, {cols})
            VALUES (%s,%s,%s,%s,%s,{ph})
            ON CONFLICT (run_id, dedupe_key) DO NOTHING
            RETURNING id""",
        (run_id, raw["intent_id"], key, json.dumps(raw), arm,
         *[raw.get(f) for f in FIELDS]),
    )
    row = cur.fetchone()
    if row is None:
        cur.execute("SELECT id FROM failure_event WHERE run_id=%s AND dedupe_key=%s",
                    (run_id, key))
        return cur.fetchone()[0], _event(raw, arm), True

    event_id = row[0]
    ledger.append(cur, run_id, "failure_event", event_id, raw["intent_id"],
                  "event_ingested", "system",
                  {"dedupe_key": key, "arm": arm, "gateway_code": raw["gateway_code"],
                   "gateway_reason": raw["gateway_reason"], "amount_minor": raw["amount_minor"]})
    return event_id, _event(raw, arm), False


def _event(raw: dict, arm: str) -> dict:
    e = {f: raw.get(f) for f in FIELDS}
    e.update(payment_intent_id=raw["intent_id"], arm=arm,
             consented_channels=raw["consented_channels"],
             has_alternate_instrument=raw["has_alternate_instrument"])
    return e
