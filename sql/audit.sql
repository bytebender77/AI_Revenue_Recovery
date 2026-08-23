-- Reconstruct one payment intent end to end.
--   AUDIT=dev_pi_000123 make audit
-- Answers, in order: what was known, what was permitted, what was considered
-- with scores, what bound the choice, what executed, what happened, under which
-- versions -- without reading any application code.

\echo '=== 1. WHAT WAS KNOWN (raw gateway view, stored verbatim) ==='
SELECT payment_intent_id, arm, amount_minor, method, regime, mandate_id,
       gateway_code, gateway_reason, gateway_source, gateway_step, gateway_description,
       failed_at_h, ingested_at
FROM failure_event WHERE payment_intent_id = :id \gx

\echo '=== 2. WHAT WE CONCLUDED ==='
SELECT d.slot_diag, d.failure_cause, d.persistence_class, d.resolver, d.confidence,
       d.taxonomy_version, d.normalizer_version
FROM (SELECT dg.*, row_number() OVER (ORDER BY dg.id) - 1 AS slot_diag
      FROM diagnosis dg JOIN failure_event fe ON fe.id = dg.failure_event_id
      WHERE fe.payment_intent_id = :id) d ORDER BY d.slot_diag;

\echo '=== 3. WHAT WAS PERMITTED, AND WHICH RULE BLOCKED WHAT ==='
SELECT slot, permitted_actions, blocked_actions
FROM eligibility_snapshot WHERE payment_intent_id = :id ORDER BY slot \gx

\echo '=== 3b. EVERY RULE EVALUATED (passes included) ==='
SELECT e.slot, r->>'rule_id' AS rule, r->>'result' AS result,
       r->>'threshold' AS threshold, r->>'observed' AS observed, r->>'blocks' AS blocks
FROM eligibility_snapshot e, jsonb_array_elements(e.rule_evaluations) r
WHERE e.payment_intent_id = :id ORDER BY e.slot, rule;

\echo '=== 4. WHAT WAS CONSIDERED, WITH SCORES (rejected options included) ==='
SELECT d.decision_seq AS seq, d.slot, c->>'action' AS action, c->>'channel' AS chan,
       (c->>'score')::numeric AS ev_inr,
       (c->'evidence'->>'p_mean')::numeric AS p_success,
       (c->'evidence'->>'observations')::int AS obs,
       c->'components_inr'->>'value' AS value_inr,
       c->'components_inr'->>'annoyance' AS annoy_inr,
       round((c->>'at_h')::numeric, 1) AS at_h,
       (c->>'permitted')::bool AS ok, c->>'blocked_by' AS blocked_by,
       (c->>'chosen')::bool AS chosen
FROM decision d, jsonb_array_elements(d.candidate_set) c
WHERE d.payment_intent_id = :id ORDER BY d.decision_seq, ev_inr DESC;

\echo '=== 5. WHAT BOUND THE CHOICE, AND UNDER WHICH VERSIONS ==='
SELECT decision_seq, slot, chosen_action, chosen_channel, round(scheduled_for_h::numeric,2) AS at_h,
       decision_reason_code, binding_constraint, score_basis,
       taxonomy_version, model_version, policy_version
FROM decision WHERE payment_intent_id = :id ORDER BY decision_seq \gx

\echo '=== 6. WHAT EXECUTED (idempotency + fire-time revalidation) ==='
SELECT a.id, d.slot, a.idempotency_key, a.adapter, a.action_type, a.channel,
       round(a.fired_at_h::numeric,2) AS fired_at_h,
       a.revalidation_result, a.execution_status, a.outcome, a.p_used
FROM attempt a JOIN decision d ON d.id = a.decision_id
WHERE d.payment_intent_id = :id ORDER BY a.id;

\echo '=== 7. WHAT HAPPENED ==='
SELECT arm, terminal_state, recovered_amount_minor,
       round(recovered_at_h::numeric,2) AS recovered_at_h, attribution, debits, contacts
FROM outcome WHERE payment_intent_id = :id;

\echo '=== 8. LEDGER CHAIN FOR THIS INTENT ==='
SELECT seq, event, actor, entity_type, entity_id,
       left(prev_hash, 12) AS prev, left(entry_hash, 12) AS entry
FROM ledger_entry WHERE payment_intent_id = :id ORDER BY seq;
