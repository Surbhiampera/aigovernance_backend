-- Migration 007: Add total_token_cost to all project-scoped summary/event tables.
-- total_tokens already exists in all tables; only total_token_cost is new.
-- Safe to re-run: each ALTER uses IF NOT EXISTS.

-- telemetry_events
ALTER TABLE telemetry_events
    ADD COLUMN IF NOT EXISTS total_token_cost NUMERIC(14,6) NOT NULL DEFAULT 0;

-- trace_model_usage
ALTER TABLE trace_model_usage
    ADD COLUMN IF NOT EXISTS total_token_cost NUMERIC(14,6) NOT NULL DEFAULT 0;

-- daily_org_summary
ALTER TABLE daily_org_summary
    ADD COLUMN IF NOT EXISTS total_token_cost NUMERIC(14,6) NOT NULL DEFAULT 0;

-- monthly_org_summary
ALTER TABLE monthly_org_summary
    ADD COLUMN IF NOT EXISTS total_token_cost NUMERIC(14,6) NOT NULL DEFAULT 0;

-- project_model_usage
ALTER TABLE project_model_usage
    ADD COLUMN IF NOT EXISTS total_token_cost NUMERIC(14,6) NOT NULL DEFAULT 0;
