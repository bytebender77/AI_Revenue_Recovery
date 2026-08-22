"""The batch tick: ingest -> diagnose -> gate -> decide -> execute -> outcome.

Half the cohort is held out as a randomised control arm that receives NO_ACTION.
Control intents still travel the full pipeline and still get a decision record --
a control arm you cannot audit is not a control arm.

The headline number is the treatment/control difference, which is what you could
actually measure in production. The simulator's counterfactual is printed beneath
it as a check on the estimator, never as the claim.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time
from dataclasses import asdict

import numpy as np

from rr import rng
from rr.adapters.base import REVALIDATION_OK
from rr.budget import EscalationBudget
from rr.config import CLOCK, COSTS
from rr.db import ledger
from rr.db.conn import connect
from rr.pipeline.eligibility import PolicyState, evaluate, rules_as_json
from rr.pipeline.executor import execute_decision
from rr.pipeline.ingest import ingest
from rr.pipeline.normalize import diagnose
from rr.pipeline.policy import SCORE_BASIS, DecisionRecord, decide
from rr.sim.adapter import SimAdapter
from rr.sim.cohort import load_observed
from rr.taxonomy import ActionType
from rr.versions import MODEL_VERSION, POLICY_VERSION, TAXONOMY_VERSION

MAX_SLOTS = 8


def _control_decision() -> DecisionRecord:
    return DecisionRecord(
        chosen_action=ActionType.NO_ACTION.value, chosen_channel=None, scheduled_for_h=None,
        candidate_set=[{"action": ActionType.NO_ACTION.value, "channel": None, "at_h": None,
                        "score": 0.0, "permitted": True, "blocked_by": None, "chosen": True,
                        "rationale": "randomised holdout: no action is taken by design"}],
        score_basis=SCORE_BASIS, decision_reason_code="CONTROL_ARM_HOLDOUT",
        binding_constraint="ARM_ASSIGNMENT",
    )


def process(cur, run_id: str, event_id: int, event: dict, arm: str, adapter, budget) -> dict:
    iid = event["payment_intent_id"]
    t_end = event["failed_at_h"] + CLOCK.recovery_horizon_hours
    st = PolicyState()
    debits = contacts = 0

    for slot in range(MAX_SLOTS):
        st.slot = slot
        diag = diagnose(event)
        cur.execute(
            """INSERT INTO diagnosis (failure_event_id, failure_cause, persistence_class,
                   resolver, confidence, taxonomy_version, normalizer_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (event_id, diag.failure_cause.value, diag.persistence_class, diag.resolver,
             diag.confidence, diag.taxonomy_version, diag.normalizer_version))
        diag_id = cur.fetchone()[0]

        elig = evaluate(event, diag, st, budget)
        cur.execute(
            """INSERT INTO eligibility_snapshot (diagnosis_id, payment_intent_id, slot,
                   permitted_actions, blocked_actions, rule_evaluations, context_snapshot)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (diag_id, iid, slot, json.dumps(elig.permitted), json.dumps(elig.blocked),
             json.dumps(rules_as_json(elig.rules)), json.dumps(elig.context)))
        elig_id = cur.fetchone()[0]

        dec = _control_decision() if arm == "control" else decide(event, diag, st, elig)
        cur.execute(
            """INSERT INTO decision (eligibility_snapshot_id, payment_intent_id, slot, arm,
                   chosen_action, chosen_channel, scheduled_for_h, candidate_set, score_basis,
                   decision_reason_code, binding_constraint, taxonomy_version, model_version,
                   policy_version)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (elig_id, iid, slot, arm, dec.chosen_action, dec.chosen_channel,
             dec.scheduled_for_h, json.dumps(dec.candidate_set), dec.score_basis,
             dec.decision_reason_code, dec.binding_constraint, TAXONOMY_VERSION,
             MODEL_VERSION, POLICY_VERSION))
        dec_id = cur.fetchone()[0]
        ledger.append(cur, run_id, "decision", dec_id, iid, "decision_made", "agent",
                      {"slot": slot, "chosen_action": dec.chosen_action,
                       "reason_code": dec.decision_reason_code,
                       "binding_constraint": dec.binding_constraint,
                       "candidates": len(dec.candidate_set),
                       "policy_version": POLICY_VERSION})

        if dec.chosen_action == ActionType.NO_ACTION.value:
            break
        if dec.scheduled_for_h is None or dec.scheduled_for_h > t_end:
            break
        if dec.chosen_action == ActionType.ESCALATE_HUMAN.value and budget is not None:
            budget.consume()

        res = execute_decision(cur.connection, run_id, dec_id, event, slot,
                               dec.chosen_action, dec.chosen_channel, dec.scheduled_for_h,
                               st.retry_index, st.nudges_sent, adapter)
        if res["execution_status"] != "executed":
            break
        if dec.chosen_action in (ActionType.RETRY_SAME.value,
                                 ActionType.RETRY_ALTERNATE_METHOD.value):
            debits += 1
            st.retry_index += 1
        elif dec.chosen_action == ActionType.NUDGE.value:
            contacts += 1
            st.nudges_sent += 1
        elif dec.chosen_action == ActionType.MERCHANT_ALERT.value:
            st.merchant_alerted = True
        elif dec.chosen_action == ActionType.ESCALATE_HUMAN.value:
            st.escalated = True
        if res["outcome"] == "success":
            break

    recovered_at, attribution = adapter.poll_outcome(iid, t_end)
    amount = event["amount_minor"] if recovered_at is not None else 0
    cur.execute(
        """INSERT INTO outcome (run_id, payment_intent_id, arm, terminal_state,
               recovered_amount_minor, recovered_at_h, attribution, debits, contacts)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (run_id, payment_intent_id) DO NOTHING RETURNING id""",
        (run_id, iid, arm, "recovered" if recovered_at is not None else "failed",
         amount, recovered_at, attribution, debits, contacts))
    row = cur.fetchone()
    if row:
        ledger.append(cur, run_id, "outcome", row[0], iid, "outcome_recorded", "system",
                      {"terminal_state": "recovered" if recovered_at else "failed",
                       "recovered_amount_minor": amount, "attribution": attribution,
                       "debits": debits, "contacts": contacts})
    return {"recovered": amount, "arm": arm}


def _report(conn, run_id: str, adapter) -> None:
    with conn.cursor() as cur:
        cur.execute("""SELECT arm, payment_intent_id, recovered_amount_minor, attribution
                       FROM outcome WHERE run_id=%s""", (run_id,))
        rows = cur.fetchall()
    t = np.array([r[2] for r in rows if r[0] == "treatment"], dtype=float)
    c = np.array([r[2] for r in rows if r[0] == "control"], dtype=float)
    n = len(rows)

    diff = t.mean() - c.mean()
    g = np.random.default_rng(11)
    boot = np.array([
        g.choice(t, len(t), replace=True).mean() - g.choice(c, len(c), replace=True).mean()
        for _ in range(2000)])
    lo, hi = np.percentile(boot, [2.5, 97.5])

    print(f"\n=== run {run_id} -- {n} intents "
          f"(treatment {len(t)}, control {len(c)}) ===\n")
    print(f"  mean recovered / intent  treatment INR {t.mean()/100:>9,.0f}   "
          f"control INR {c.mean()/100:>9,.0f}")
    print(f"  INCREMENTAL vs control   INR {diff*n/100:>12,.0f} over the cohort")
    print(f"  95% CI                   [INR {lo*n/100:,.0f}, INR {hi*n/100:,.0f}]")
    print(f"  significant              {'YES' if lo > 0 else 'NO -- CI spans zero'}")

    heal = {r[1] for r in rows if adapter.counterfactual_self_heals(r[1])}
    agent = sum(1 for r in rows if r[3] == "agent")
    organic = sum(1 for r in rows if r[3] == "organic")
    print(f"\n  check (simulator counterfactual, not the claim):")
    print(f"    would self-heal with no action : {len(heal)} of {n}")
    print(f"    recovered, credited to agent   : {agent}")
    print(f"    recovered organically          : {organic}")
    if lo <= 0:
        print(f"\n  NOTE: at n={n} the control arm is small. A CI spanning zero here is a")
        print(f"        power problem, not evidence the policy does nothing. The M6 run")
        print(f"        uses the full held-out cohort.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=pathlib.Path, default=pathlib.Path("data"))
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()
    run_id = args.run_id or f"run_{int(time.time())}"

    obs = load_observed(args.data / "dev_observed.jsonl")
    sample = sorted(obs, key=lambda o: rng.u01(o["intent_id"], "demo"))[:args.limit]
    sample.sort(key=lambda o: o["failed_at_h"])
    adapter = SimAdapter.from_cohort(args.data, "dev")
    budget = EscalationBudget.for_cohort(len(sample), COSTS.escalation_capacity_pct)

    t0 = time.time()
    with connect() as conn:
        with conn.cursor() as cur:
            for raw in sample:
                arm = "treatment" if rng.u01(raw["intent_id"], "arm") < 0.5 else "control"
                event_id, event, deduped = ingest(cur, run_id, raw, arm)
                if deduped:
                    continue
                process(cur, run_id, event_id, event, arm, adapter, budget)
        conn.commit()
        _report(conn, run_id, adapter)
    print(f"\n  {time.time() - t0:.1f}s   escalation budget {budget.used}/{budget.capacity}")
    print(f"  run_id: {run_id}\n")


if __name__ == "__main__":
    main()
