-- M2 schema. One row per state transition, nothing mutated after the fact.
-- The standard this is built to: a judge picks a payment_intent_id and can
-- reconstruct what was known, what was permitted, what was considered with
-- scores, what bound the choice, what executed, what happened, and under which
-- versions -- without reading any application code.

DROP TABLE IF EXISTS ledger_entry, outcome, attempt, decision,
                     eligibility_snapshot, diagnosis, failure_event CASCADE;

-- 1. what arrived --------------------------------------------------------
CREATE TABLE failure_event (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT NOT NULL,
    payment_intent_id   TEXT NOT NULL,
    dedupe_key          TEXT NOT NULL,
    raw_payload         JSONB NOT NULL,            -- verbatim, never reparsed
    arm                 TEXT NOT NULL CHECK (arm IN ('treatment','control')),
    merchant_id         TEXT NOT NULL,
    customer_id         TEXT NOT NULL,
    issuer_id           TEXT NOT NULL,
    amount_minor        BIGINT NOT NULL,
    currency            TEXT NOT NULL,
    method              TEXT NOT NULL,
    regime              TEXT NOT NULL,
    mandate_id          TEXT,
    instrument_ref      TEXT NOT NULL,
    gateway_code        TEXT NOT NULL,
    gateway_reason      TEXT NOT NULL,
    gateway_source      TEXT NOT NULL,
    gateway_step        TEXT NOT NULL,
    gateway_description TEXT NOT NULL,
    failed_at_h         DOUBLE PRECISION NOT NULL,
    attempt_index       INT NOT NULL,
    ingested_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, dedupe_key)                    -- dedupe to the intent key
);

-- 2. what we concluded ---------------------------------------------------
CREATE TABLE diagnosis (
    id                  BIGSERIAL PRIMARY KEY,
    failure_event_id    BIGINT NOT NULL REFERENCES failure_event(id),
    failure_cause       TEXT NOT NULL,
    persistence_class   TEXT NOT NULL,
    resolver            TEXT NOT NULL,             -- map | llm (M5) | default
    confidence          DOUBLE PRECISION NOT NULL,
    taxonomy_version    TEXT NOT NULL,
    normalizer_version  TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3. what was legally available -----------------------------------------
CREATE TABLE eligibility_snapshot (
    id                  BIGSERIAL PRIMARY KEY,
    diagnosis_id        BIGINT NOT NULL REFERENCES diagnosis(id),
    payment_intent_id   TEXT NOT NULL,
    slot                INT NOT NULL,
    permitted_actions   JSONB NOT NULL,
    blocked_actions     JSONB NOT NULL,
    rule_evaluations    JSONB NOT NULL,            -- passes too, not only blocks
    context_snapshot    JSONB NOT NULL,
    evaluated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. what was considered and chosen -------------------------------------
CREATE TABLE decision (
    id                      BIGSERIAL PRIMARY KEY,
    eligibility_snapshot_id BIGINT NOT NULL REFERENCES eligibility_snapshot(id),
    payment_intent_id       TEXT NOT NULL,
    slot                    INT NOT NULL,
    arm                     TEXT NOT NULL,
    chosen_action           TEXT NOT NULL,
    chosen_channel          TEXT,
    scheduled_for_h         DOUBLE PRECISION,
    candidate_set           JSONB NOT NULL,        -- EVERY action considered, incl. rejected
    score_basis             TEXT NOT NULL,         -- rule_priority (M2) -> expected_net_value (M4)
    decision_reason_code    TEXT NOT NULL,
    binding_constraint      TEXT,                  -- rule that removed a higher-ranked action
    taxonomy_version        TEXT NOT NULL,
    model_version           TEXT NOT NULL,
    policy_version          TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 5. what actually ran ---------------------------------------------------
CREATE TABLE attempt (
    id                  BIGSERIAL PRIMARY KEY,
    decision_id         BIGINT NOT NULL REFERENCES decision(id),
    idempotency_key     TEXT NOT NULL UNIQUE,      -- (decision_id, action). One execution, ever.
    adapter             TEXT NOT NULL,
    action_type         TEXT NOT NULL,
    channel             TEXT,
    fired_at_h          DOUBLE PRECISION NOT NULL,
    revalidation_result TEXT NOT NULL,             -- logged on EVERY attempt, pass or abort
    execution_status    TEXT NOT NULL CHECK (execution_status IN ('executed','aborted')),
    outcome             TEXT,
    p_used              DOUBLE PRECISION,
    request_snapshot    JSONB NOT NULL,
    response_snapshot   JSONB NOT NULL,
    executed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 6. what happened -------------------------------------------------------
CREATE TABLE outcome (
    id                      BIGSERIAL PRIMARY KEY,
    run_id                  TEXT NOT NULL,
    payment_intent_id       TEXT NOT NULL,
    arm                     TEXT NOT NULL,
    terminal_state          TEXT NOT NULL,
    recovered_amount_minor  BIGINT NOT NULL,
    recovered_at_h          DOUBLE PRECISION,
    attribution             TEXT,                  -- agent | organic | null
    debits                  INT NOT NULL,
    contacts                INT NOT NULL,
    UNIQUE (run_id, payment_intent_id)
);

-- 7. tamper-evident chain over every transition -------------------------
CREATE TABLE ledger_entry (
    seq                 BIGSERIAL PRIMARY KEY,
    run_id              TEXT NOT NULL,
    payment_intent_id   TEXT,
    entity_type         TEXT NOT NULL,
    entity_id           BIGINT,
    event               TEXT NOT NULL,
    actor               TEXT NOT NULL,             -- agent | system | human
    payload             JSONB NOT NULL,
    payload_hash        TEXT NOT NULL,
    prev_hash           TEXT NOT NULL,
    entry_hash          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION ledger_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ledger_entry is append-only (attempted %)', TG_OP;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER ledger_no_mutation
    BEFORE UPDATE OR DELETE ON ledger_entry
    FOR EACH ROW EXECUTE FUNCTION ledger_append_only();

CREATE INDEX ON failure_event (payment_intent_id);
CREATE INDEX ON decision (payment_intent_id);
CREATE INDEX ON eligibility_snapshot (payment_intent_id);
CREATE INDEX ON ledger_entry (payment_intent_id);
