"""The demo entry point. Same loop, same policy, same decisions as the eval harness.

There is ONE decision layer. `rr/agent/policy.py` scores every candidate and
`rr/agent/decision.py` serialises every record; this module supplies a Postgres
sink and nothing else. The eval harness supplies no sink. That is the whole
difference between "measured" and "demonstrated", and
tests/test_one_decision_layer.py deep-compares the two to prove it.

Half the cohort is a randomised control arm receiving NO_ACTION. Control intents
are scored and audited identically -- a control arm you cannot audit is not one.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import time

import numpy as np

from rr import rng
from rr.agent.features import IssuerFailureIndex
from rr.agent.policy import EVPolicy
from rr.config import COSTS
from rr.db import ledger
from rr.db.conn import connect, ensure_schema
from rr.model.beta_binomial import BetaBinomialModel
from rr.eval.agent_run import run_agent
from rr.pipeline.eligibility import rules_as_json
from rr.sim.adapter import SimAdapter
from rr.sim.cohort import load_observed
from rr.taxonomy import ActionType


class PostgresSink:
    """Persists what the loop decides. It cannot change any of it."""

    def __init__(self, conn, run_id: str, arm_of):
        self.conn, self.run_id, self.arm_of = conn, run_id, arm_of
        self._events: dict[str, int] = {}
        self._last_decision: dict[str, int] = {}

    # ------------------------------------------------------------- ingest --
    def _event_id(self, cur, obs: dict) -> int:
        iid = obs["intent_id"]
        if iid in self._events:
            return self._events[iid]
        from rr.pipeline.ingest import ingest
        event_id, _event, _dup = ingest(cur, self.run_id, obs, self.arm_of(iid))
        self._events[iid] = event_id
        return event_id

    # ------------------------------------------------------------ decision --
    def on_decision(self, obs: dict, record, diag, elig) -> None:
        iid = obs["intent_id"]
        with self.conn.cursor() as cur:
            event_id = self._event_id(cur, obs)
            cur.execute(
                """INSERT INTO diagnosis (failure_event_id, failure_cause, persistence_class,
                       resolver, confidence, taxonomy_version, normalizer_version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (event_id, diag.failure_cause.value, diag.persistence_class,
                 diag.resolver, diag.confidence, diag.taxonomy_version,
                 diag.normalizer_version))
            diag_id = cur.fetchone()[0]

            cur.execute(
                """INSERT INTO eligibility_snapshot (diagnosis_id, payment_intent_id, slot,
                       permitted_actions, blocked_actions, rule_evaluations, context_snapshot)
                   VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (diag_id, iid, record.slot, json.dumps(elig.permitted),
                 json.dumps(elig.blocked), json.dumps(rules_as_json(elig.rules)),
                 json.dumps(elig.context)))
            elig_id = cur.fetchone()[0]

            r = record.as_row()
            cur.execute(
                """INSERT INTO decision (run_id, eligibility_snapshot_id, payment_intent_id, slot,
                       decision_seq, arm, chosen_action, chosen_channel, scheduled_for_h, candidate_set,
                       score_basis, decision_reason_code, binding_constraint,
                       taxonomy_version, model_version, policy_version)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (self.run_id, elig_id, iid, r["slot"], r["decision_seq"], r["arm"], r["chosen_action"],
                 r["chosen_channel"], r["scheduled_for_h"],
                 json.dumps(r["candidate_set"]), r["score_basis"],
                 r["decision_reason_code"], r["binding_constraint"],
                 r["taxonomy_version"], r["model_version"], r["policy_version"]))
            dec_id = cur.fetchone()[0]
            self._last_decision[iid] = dec_id
            ledger.append(cur, self.run_id, "decision", dec_id, iid, "decision_made",
                          "agent", {"slot": r["slot"], "chosen_action": r["chosen_action"],
                                    "reason_code": r["decision_reason_code"],
                                    "binding_constraint": r["binding_constraint"],
                                    "candidates": len(r["candidate_set"]),
                                    "policy_version": r["policy_version"]})

    # ------------------------------------------------------------- attempt --
    def _attempt(self, obs, record, cand, revalidation, status, outcome, p_used):
        """One row per action the loop fired or aborted. `idempotency_key` is UNIQUE
        on (decision, action), so a replayed tick cannot double-debit; the ON
        CONFLICT is the enforcement, not a convenience."""
        iid = obs["intent_id"]
        dec_id = self._last_decision.get(iid)
        if dec_id is None:
            return
        key = f"{dec_id}:{record.chosen_action}"
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO attempt (decision_id, idempotency_key, adapter, action_type,
                       channel, fired_at_h, revalidation_result, execution_status,
                       outcome, p_used, request_snapshot, response_snapshot)
                   VALUES (%s,%s,'sim',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (idempotency_key) DO NOTHING RETURNING id""",
                (dec_id, key, record.chosen_action, record.chosen_channel,
                 cand.at_h, revalidation, status, outcome, p_used,
                 json.dumps({"decision_seq": record.decision_seq,
                             "ev_inr": round(cand.ev_mean / 100, 2)}),
                 json.dumps({"evidence": cand.evidence})))
            row = cur.fetchone()
            if row:
                ledger.append(cur, self.run_id, "attempt", row[0], iid,
                              "attempt_executed" if status == "executed" else "attempt_aborted",
                              "agent", {"idempotency_key": key, "action": record.chosen_action,
                                        "revalidation_result": revalidation,
                                        "outcome": outcome, "at_h": cand.at_h})

    def on_attempt(self, obs, record, cand, rec) -> None:
        self._attempt(obs, record, cand, "ok", "executed",
                      rec.outcome.value, rec.p_used)

    def on_attempt_aborted(self, obs, record, cand, reason) -> None:
        self._attempt(obs, record, cand, reason, "aborted", None, None)

    # ------------------------------------------------------------- outcome --
    def on_outcome(self, result) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO outcome (run_id, payment_intent_id, arm, terminal_state,
                       recovered_amount_minor, recovered_at_h, attribution, debits, contacts)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id, payment_intent_id) DO NOTHING RETURNING id""",
                (self.run_id, result.intent_id, self.arm_of(result.intent_id),
                 "recovered" if result.recovered_at_h is not None else "failed",
                 result.recovered_value_minor, result.recovered_at_h,
                 result.attribution, result.debits, result.contacts))
            row = cur.fetchone()
            if row:
                ledger.append(cur, self.run_id, "outcome", row[0], result.intent_id,
                              "outcome_recorded", "system",
                              {"terminal_state": "recovered" if result.recovered_at_h else "failed",
                               "recovered_amount_minor": result.recovered_value_minor,
                               "attribution": result.attribution,
                               "debits": result.debits, "contacts": result.contacts})


def report(results, arm_of) -> None:
    t = np.array([r.recovered_value_minor for r in results
                  if arm_of(r.intent_id) == "treatment"], dtype=float)
    c = np.array([r.recovered_value_minor for r in results
                  if arm_of(r.intent_id) == "control"], dtype=float)
    n = len(results)
    diff = t.mean() - c.mean()
    g = np.random.default_rng(11)
    boot = np.array([g.choice(t, len(t), replace=True).mean()
                     - g.choice(c, len(c), replace=True).mean() for _ in range(2000)])
    lo, hi = np.percentile(boot, [2.5, 97.5])
    print(f"\n  {n} intents (treatment {len(t)}, control {len(c)})")
    print(f"  mean recovered / intent   treatment INR {t.mean()/100:>9,.0f}   "
          f"control INR {c.mean()/100:>9,.0f}")
    print(f"  INCREMENTAL vs control    INR {diff*n/100:>12,.0f}")
    print(f"  95% CI                    [INR {lo*n/100:,.0f}, INR {hi*n/100:,.0f}]")
    print(f"  significant               {'YES' if lo > 0 else 'NO -- CI spans zero'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=pathlib.Path, default=pathlib.Path("data"))
    ap.add_argument("--models", type=pathlib.Path, default=pathlib.Path("models"))
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--run-id", default=None)
    args = ap.parse_args()
    run_id = args.run_id or f"run_{int(time.time())}"

    obs_all = load_observed(args.data / "dev_observed.jsonl")
    sample = sorted(obs_all, key=lambda o: rng.u01(o["intent_id"], "demo"))[: args.limit]
    sample.sort(key=lambda o: o["failed_at_h"])
    adapter = SimAdapter.from_cohort(args.data, "dev")
    latents = [adapter._lat[o["intent_id"]] for o in sample]

    policy = EVPolicy(
        BetaBinomialModel.load(args.models / "success_model_v1.json"),
        BetaBinomialModel.load(args.models / "organic_model_v2.json"),
        issuer_index=IssuerFailureIndex.build(obs_all))
    arm_of = lambda iid: "treatment" if rng.u01(iid, "arm") < 0.5 else "control"

    ensure_schema()
    t0 = time.time()
    with connect() as conn:
        sink = PostgresSink(conn, run_id, arm_of)
        results, meta = run_agent(sample, latents, policy, sink=sink, arm_of=arm_of)
        for r in results:
            sink.on_outcome(r)
        conn.commit()
        report(results, arm_of)
    print(f"\n  {time.time() - t0:.1f}s   escalation budget "
          f"{meta['budget'][0]}/{meta['budget'][1]}   run_id: {run_id}\n")


if __name__ == "__main__":
    main()
