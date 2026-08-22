-- M5: audit trail for every LLM call.
--
-- Additive migration rather than an edit to schema.sql, so an existing database
-- picks it up without dropping the ledger chain:
--   psql "$DATABASE_URL" -f sql/002_llm_call.sql
--
-- The eval harness stays on files and writes the same records to
-- models/llm_calls.jsonl; this table is the pipeline path.

CREATE TABLE IF NOT EXISTS llm_call (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT,
    payment_intent_id   TEXT,
    purpose             TEXT NOT NULL,          -- tail_normalizer | explainer (M7)
    model_id            TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    prompt_hash         TEXT NOT NULL,          -- system prompt + response schema
    rendered_input_hash TEXT NOT NULL,          -- cache key; identical input = identical answer
    -- claude-opus-5 REJECTS `temperature` with a 400, so the parameter is never
    -- sent. Logged as NULL with the reason rather than a fabricated 0.0.
    temperature         DOUBLE PRECISION,
    temperature_note    TEXT NOT NULL,
    raw_output          TEXT NOT NULL,
    parse_status        TEXT NOT NULL
        CHECK (parse_status IN ('ok','cached','below_floor','schema_violation','error')),
    resolved_cause      TEXT NOT NULL,          -- always in the closed taxonomy
    confidence          TEXT NOT NULL,
    input_tokens        INT NOT NULL DEFAULT 0,
    output_tokens       INT NOT NULL DEFAULT 0,
    cache_read_tokens   INT NOT NULL DEFAULT 0,
    cost_usd            DOUBLE PRECISION NOT NULL DEFAULT 0,
    latency_ms          DOUBLE PRECISION NOT NULL DEFAULT 0,
    request_id          TEXT,                   -- Anthropic request-id header
    cached              BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS llm_call_intent_idx ON llm_call (payment_intent_id);
CREATE INDEX IF NOT EXISTS llm_call_input_idx  ON llm_call (rendered_input_hash);

-- The diagnosis row already carries `resolver` ('map' | 'llm' | 'llm_abstain' |
-- 'default'); this column ties a diagnosis to the exact call that produced it.
ALTER TABLE diagnosis ADD COLUMN IF NOT EXISTS llm_call_id BIGINT REFERENCES llm_call(id);
