"""Idempotent execution with fire-time revalidation.

Two properties this buys, both of which are guardrails rather than optimisations:

  * Idempotency. `idempotency_key` is UNIQUE on (decision_id, action). A replayed
    tick cannot double-debit; the second insert loses the race and returns the
    first attempt untouched.
  * Fire-time revalidation, INSIDE the transaction. Payment state is re-read at
    the moment the action fires, not at the moment it was decided. A payment that
    recovered on its own between decision and execution aborts the attempt --
    this is the race that produces double charges and nudges to people who have
    already paid. `revalidation_result` is logged on EVERY attempt, pass or abort.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Optional

from rr.adapters.base import REVALIDATION_OK, ExecutionRequest, ExecutorAdapter
from rr.db import ledger


def execute_decision(conn, run_id: str, decision_id: int, event: dict, slot: int,
                     action_type: str, channel: Optional[str], at_h: float,
                     retry_index: int, nudges_sent: int,
                     adapter: ExecutorAdapter) -> dict:
    iid = event["payment_intent_id"]
    key = f"{decision_id}:{action_type}"
    req = ExecutionRequest(
        payment_intent_id=iid, idempotency_key=key, slot=slot, action_type=action_type,
        channel=channel, at_h=at_h, amount_minor=event["amount_minor"],
        method=event["method"], regime=event["regime"],
        instrument_ref=event["instrument_ref"], mandate_id=event.get("mandate_id"),
        retry_index=retry_index, nudges_sent=nudges_sent,
    )

    with conn.cursor() as cur:
        # Claim the slot first. If someone else already has it, we are a replay.
        cur.execute(
            """INSERT INTO attempt (decision_id, idempotency_key, adapter, action_type,
                   channel, fired_at_h, revalidation_result, execution_status,
                   request_snapshot, response_snapshot)
               VALUES (%s,%s,%s,%s,%s,%s,'pending','aborted',%s,'{}')
               ON CONFLICT (idempotency_key) DO NOTHING
               RETURNING id""",
            (decision_id, key, adapter.name, action_type, channel, at_h,
             json.dumps(asdict(req))),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute("""SELECT revalidation_result, execution_status, outcome
                           FROM attempt WHERE idempotency_key = %s""", (key,))
            reval, status, outcome = cur.fetchone()
            return {"attempt_id": None, "revalidation_result": reval,
                    "execution_status": status, "outcome": outcome, "replayed": True}
        attempt_id = row[0]

        reval = adapter.revalidate(iid, at_h)
        if reval != REVALIDATION_OK:
            cur.execute("""UPDATE attempt SET revalidation_result=%s, execution_status='aborted',
                           response_snapshot=%s WHERE id=%s""",
                        (reval, json.dumps({"aborted_before_execution": True}), attempt_id))
            ledger.append(cur, run_id, "attempt", attempt_id, iid, "attempt_aborted", "agent",
                          {"idempotency_key": key, "revalidation_result": reval,
                           "action": action_type, "at_h": at_h})
            return {"attempt_id": attempt_id, "revalidation_result": reval,
                    "execution_status": "aborted", "outcome": None, "replayed": False}

        result = adapter.execute(req)
        cur.execute("""UPDATE attempt SET revalidation_result=%s, execution_status='executed',
                       outcome=%s, p_used=%s, response_snapshot=%s WHERE id=%s""",
                    (reval, result.outcome, result.p_used, json.dumps(result.response), attempt_id))
        ledger.append(cur, run_id, "attempt", attempt_id, iid, "attempt_executed", "agent",
                      {"idempotency_key": key, "action": action_type, "channel": channel,
                       "at_h": at_h, "revalidation_result": reval, "outcome": result.outcome,
                       "p_used": result.p_used, "adapter": adapter.name})
        return {"attempt_id": attempt_id, "revalidation_result": reval,
                "execution_status": "executed", "outcome": result.outcome, "replayed": False}
