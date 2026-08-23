-- A decision can be re-taken at the same `slot`: when the policy schedules an
-- action for a future tick, the intent is re-decided when that tick arrives with
-- the attempt history unchanged. `slot` is the attempt index, NOT a unique key --
-- without a sequence, a legitimate re-decision is indistinguishable from a
-- duplicate row.
ALTER TABLE decision ADD COLUMN IF NOT EXISTS decision_seq INT;

-- `run_id` is denormalised onto decision. It was previously reachable only through
-- a four-table join (decision -> eligibility_snapshot -> diagnosis -> failure_event),
-- which made "the decisions from this run" awkward to express and left the
-- uniqueness of decision_seq unscoped across runs.
ALTER TABLE decision ADD COLUMN IF NOT EXISTS run_id TEXT;

DROP INDEX IF EXISTS decision_intent_seq_idx;
CREATE UNIQUE INDEX IF NOT EXISTS decision_run_intent_seq_idx
    ON decision (run_id, payment_intent_id, decision_seq)
    WHERE decision_seq IS NOT NULL AND run_id IS NOT NULL;
