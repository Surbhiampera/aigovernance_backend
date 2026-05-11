-- Migration 003: Decorator-based governance framework
-- Adds first-class support for automatic telemetry capture via Python decorators.
-- External tools integrate once with @gov.trace() / @gov.llm_call() / @gov.pipeline()
-- and all telemetry is stored in these tables automatically.
--
-- Run this against an existing database.
-- For fresh installs, use 004_consolidated_schema.sql instead.

-- ============================================================
-- 1. telemetry_events — decorator context columns
-- ============================================================
-- Identifies which decorated function produced each event and
-- what kind of decorator wrapped it.
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS tool_name        VARCHAR(150);
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS function_name    VARCHAR(255);
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS module_path      VARCHAR(500);
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS decorator_type   VARCHAR(50);
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS execution_env    VARCHAR(50) DEFAULT 'production';
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS sdk_version      VARCHAR(20);
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS tool_version     VARCHAR(50);
-- PII-masked previews (first N chars of serialized input/output)
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS input_preview    TEXT;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS output_preview   TEXT;

-- ============================================================
-- 2. trace_model_usage — per-model-call context
-- ============================================================
ALTER TABLE trace_model_usage ADD COLUMN IF NOT EXISTS function_name  VARCHAR(255);
ALTER TABLE trace_model_usage ADD COLUMN IF NOT EXISTS call_sequence   INTEGER DEFAULT 0;

-- ============================================================
-- 3. decorator_registrations
-- ============================================================
CREATE TABLE IF NOT EXISTS decorator_registrations (
    id               BIGSERIAL PRIMARY KEY,
    org_id           VARCHAR(100) NOT NULL,
    project_id       VARCHAR(100),
    tool_name        VARCHAR(150) NOT NULL,
    function_name    VARCHAR(255) NOT NULL,
    module_path      VARCHAR(500),
    decorator_type   VARCHAR(50)  NOT NULL DEFAULT 'trace',
    sdk_version      VARCHAR(20),
    python_version   VARCHAR(20),
    execution_env    VARCHAR(50)  DEFAULT 'production',
    first_seen       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    last_seen        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    call_count       BIGINT       DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_dec_reg_unique
    ON decorator_registrations (org_id, tool_name, function_name, COALESCE(module_path, ''));
CREATE INDEX IF NOT EXISTS idx_dec_reg_org_tool
    ON decorator_registrations (org_id, tool_name);

-- ============================================================
-- 4. project_model_usage
-- ============================================================
CREATE TABLE IF NOT EXISTS project_model_usage (
    id                      BIGSERIAL PRIMARY KEY,
    org_id                  VARCHAR(100) NOT NULL,
    project_id              VARCHAR(100),
    model_name              VARCHAR(120) NOT NULL,
    provider                VARCHAR(100),
    date                    DATE         NOT NULL,
    call_count              INTEGER      DEFAULT 0,
    total_prompt_tokens     INTEGER      DEFAULT 0,
    total_completion_tokens INTEGER      DEFAULT 0,
    total_tokens            INTEGER      DEFAULT 0,
    total_cost              DECIMAL(14,6) DEFAULT 0,
    avg_latency_ms          INTEGER      DEFAULT 0,
    success_count           INTEGER      DEFAULT 0,
    error_count             INTEGER      DEFAULT 0,
    created_at              TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_proj_model_usage_unique
    ON project_model_usage (org_id, COALESCE(project_id, '__none__'), model_name, date);
CREATE INDEX IF NOT EXISTS idx_proj_model_usage_lookup
    ON project_model_usage (org_id, project_id, date DESC);

-- ============================================================
-- 5. tool_api_inventory
-- ============================================================
CREATE TABLE IF NOT EXISTS tool_api_inventory (
    id             BIGSERIAL PRIMARY KEY,
    org_id         VARCHAR(100) NOT NULL,
    project_id     VARCHAR(100),
    tool_name      VARCHAR(150) NOT NULL,
    function_name  VARCHAR(255) NOT NULL,
    module_path    VARCHAR(500),
    decorator_type VARCHAR(50),
    description    TEXT,
    first_seen     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    last_seen      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    total_calls    BIGINT       DEFAULT 0,
    success_calls  BIGINT       DEFAULT 0,
    error_calls    BIGINT       DEFAULT 0,
    avg_latency_ms INTEGER      DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_api_inv_unique
    ON tool_api_inventory (org_id, tool_name, function_name);
CREATE INDEX IF NOT EXISTS idx_tool_api_inv_lookup
    ON tool_api_inventory (org_id, project_id, tool_name);

-- ============================================================
-- 6. request_response_logs
-- ============================================================
CREATE TABLE IF NOT EXISTS request_response_logs (
    id                 BIGSERIAL PRIMARY KEY,
    event_id           VARCHAR(120) REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    function_name      VARCHAR(255),
    input_preview      TEXT,
    output_preview     TEXT,
    input_size_bytes   INTEGER      DEFAULT 0,
    output_size_bytes  INTEGER      DEFAULT 0,
    input_keys         TEXT,
    output_keys        TEXT,
    pii_detected       BOOLEAN      DEFAULT FALSE,
    pii_fields         TEXT,
    created_at         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_req_resp_event_id
    ON request_response_logs (event_id);
