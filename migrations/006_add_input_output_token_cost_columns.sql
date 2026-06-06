-- Migration 006: Add input_tokens, output_tokens, input_token_cost, output_token_cost
-- to all project-scoped summary/event tables.
-- Safe to re-run: each ALTER uses IF NOT EXISTS.

-- daily_org_summary
ALTER TABLE daily_org_summary
    ADD COLUMN IF NOT EXISTS input_tokens       INTEGER        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_tokens      INTEGER        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS input_token_cost   NUMERIC(14,6)  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_token_cost  NUMERIC(14,6)  NOT NULL DEFAULT 0;

-- monthly_org_summary
ALTER TABLE monthly_org_summary
    ADD COLUMN IF NOT EXISTS input_tokens       INTEGER        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_tokens      INTEGER        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS input_token_cost   NUMERIC(14,6)  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_token_cost  NUMERIC(14,6)  NOT NULL DEFAULT 0;

-- project_model_usage
ALTER TABLE project_model_usage
    ADD COLUMN IF NOT EXISTS input_tokens       INTEGER        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_tokens      INTEGER        NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS input_token_cost   NUMERIC(14,6)  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_token_cost  NUMERIC(14,6)  NOT NULL DEFAULT 0;

-- trace_model_usage (input_tokens / output_tokens already exist)
ALTER TABLE trace_model_usage
    ADD COLUMN IF NOT EXISTS input_token_cost   NUMERIC(14,6)  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_token_cost  NUMERIC(14,6)  NOT NULL DEFAULT 0;

-- telemetry_events (prompt_tokens / completion_tokens already exist)
ALTER TABLE telemetry_events
    ADD COLUMN IF NOT EXISTS input_token_cost   NUMERIC(14,6)  NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS output_token_cost  NUMERIC(14,6)  NOT NULL DEFAULT 0;
