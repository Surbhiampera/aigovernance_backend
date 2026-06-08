-- =============================================================================
-- AI Governance Proxy — Clean Database Schema
-- PostgreSQL (Azure)
--
-- Target database
--   Host : ampera-postgres-server.postgres.database.azure.com
--   Port : 5432
--   Name : ai_goverance
--   User : Postgres
--
-- Run once on a fresh database.
-- All columns that were previously added via runtime _SAFE_ALTERS are already
-- included here; this file is the single source of truth.
-- =============================================================================


-- ---------------------------------------------------------------------------
-- Core tenancy
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS organizations (
    id              VARCHAR(100) PRIMARY KEY,
    org_name        VARCHAR(150) NOT NULL,
    plan_type       VARCHAR(50),
    budget_limit    NUMERIC(14, 6),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS projects (
    id              VARCHAR(100) PRIMARY KEY,
    org_id          VARCHAR(100) NOT NULL REFERENCES organizations(id),
    project_name    VARCHAR(150),
    environment     VARCHAR(50),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS users (
    id              VARCHAR(100) PRIMARY KEY,
    org_id          VARCHAR(100) REFERENCES organizations(id),
    email           VARCHAR(150),
    role            VARCHAR(50),
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- API keys (proxy governance keys)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS api_keys (
    id              VARCHAR(120) PRIMARY KEY,
    org_id          VARCHAR(100) REFERENCES organizations(id),
    project_id      VARCHAR(100) REFERENCES projects(id),
    key_name        VARCHAR(100),
    provider        VARCHAR(100),
    hashed_key      VARCHAR(64),
    raw_key_hint    VARCHAR(30),
    is_proxy_key    BOOLEAN DEFAULT FALSE,
    is_active       BOOLEAN DEFAULT TRUE,
    role            VARCHAR(50),
    expires_at      TIMESTAMP,
    last_used_at    TIMESTAMP,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Model pricing
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS model_pricing (
    id                  BIGSERIAL PRIMARY KEY,
    provider            VARCHAR(100),
    model_name          VARCHAR(120),
    input_cost_per_1k   NUMERIC(12, 6) DEFAULT 0,
    output_cost_per_1k  NUMERIC(12, 6) DEFAULT 0,
    currency            VARCHAR(10) DEFAULT 'USD',
    effective_from      TIMESTAMP DEFAULT NOW(),
    UNIQUE (provider, model_name)
);

-- Model deployments (per-org/project endpoint configuration used by deployment_service)
CREATE TABLE IF NOT EXISTS model_deployments (
    id                  BIGSERIAL PRIMARY KEY,
    deployment_id       VARCHAR(120) UNIQUE NOT NULL,
    org_id              VARCHAR(100) NOT NULL REFERENCES organizations(id),
    project_id          VARCHAR(100) REFERENCES projects(id),
    provider            VARCHAR(100) NOT NULL,
    model_name          VARCHAR(120) NOT NULL,
    deployment_name     VARCHAR(255),
    endpoint_url        VARCHAR(500),
    api_key             TEXT,
    api_version         VARCHAR(50),
    is_default          BOOLEAN DEFAULT FALSE,
    deployment_type     VARCHAR(50) DEFAULT 'api',
    auth_type           VARCHAR(50),
    api_key_ref         VARCHAR(500),
    default_parameters  JSONB,
    rate_limit_rpm      INTEGER,
    rate_limit_tpm      INTEGER,
    is_active           BOOLEAN DEFAULT TRUE,
    created_at          TIMESTAMP DEFAULT NOW(),
    updated_at          TIMESTAMP DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Governance: rules, budgets, rate limits, PII policies
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS governance_rules (
    id              BIGSERIAL PRIMARY KEY,
    rule_name       VARCHAR(150) UNIQUE NOT NULL,
    description     TEXT,
    metric_name     VARCHAR(100) NOT NULL,
    operator        VARCHAR(20) NOT NULL DEFAULT '>',
    threshold_value NUMERIC(14, 6) NOT NULL DEFAULT 0,
    severity        VARCHAR(20) NOT NULL DEFAULT 'medium',
    scope_level     VARCHAR(30) NOT NULL DEFAULT 'organization',
    scope_reference VARCHAR(150),
    is_active       BOOLEAN DEFAULT TRUE,
    org_id          VARCHAR(100),
    project_id      VARCHAR(100),
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS budgets (
    id                      BIGSERIAL PRIMARY KEY,
    org_id                  VARCHAR(100) REFERENCES organizations(id),
    project_id              VARCHAR(100) REFERENCES projects(id),
    budget_type             VARCHAR(50),
    limit_amount            NUMERIC(14, 6),
    alert_threshold_percent INTEGER DEFAULT 80,
    created_at              TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rate_limits (
    id                      BIGSERIAL PRIMARY KEY,
    org_id                  VARCHAR(100),
    project_id              VARCHAR(100),
    key_id                  VARCHAR(120),
    tool_name               VARCHAR(150),
    max_requests_per_min    INTEGER,
    max_tokens_per_day      INTEGER,
    created_at              TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS pii_policies (
    id              BIGSERIAL PRIMARY KEY,
    policy_id       VARCHAR(120) UNIQUE NOT NULL,
    org_id          VARCHAR(100) REFERENCES organizations(id) ON DELETE CASCADE,
    pii_type        VARCHAR(100) NOT NULL,
    risk_level      VARCHAR(20) DEFAULT 'medium',
    action          VARCHAR(20) DEFAULT 'mask',
    mask_pattern    VARCHAR(100) DEFAULT '[{pii_type}]',
    log_detection   BOOLEAN DEFAULT TRUE,
    priority        INTEGER DEFAULT 0,
    description     TEXT,
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Alerts
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    org_id          VARCHAR(100),
    project_id      VARCHAR(100),
    rule_id         BIGINT REFERENCES governance_rules(id),
    alert_type      VARCHAR(100),
    severity        VARCHAR(50),
    message         TEXT,
    threshold_value NUMERIC(10, 2),
    actual_value    NUMERIC(10, 2),
    status          VARCHAR(50) DEFAULT 'active',
    telemetry_id    BIGINT,
    tool_name       VARCHAR(150),
    created_at      TIMESTAMP DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Proxy core: requests, responses, token usage, costs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ai_requests (
    id                      BIGSERIAL PRIMARY KEY,
    request_id              VARCHAR(120) UNIQUE NOT NULL,
    org_id                  VARCHAR(100) NOT NULL REFERENCES organizations(id),
    project_id              VARCHAR(100) REFERENCES projects(id),
    project_ref_id          VARCHAR(100),
    trace_id                VARCHAR(120),
    span_id                 VARCHAR(120),
    parent_span_id          VARCHAR(120),
    session_id              VARCHAR(120),
    conversation_id         VARCHAR(120),
    -- route_id stored as plain string; routing table removed
    route_id                VARCHAR(120),
    request_type            VARCHAR(50) DEFAULT 'chat_completion',
    request_status          VARCHAR(30) DEFAULT 'pending',
    user_id                 VARCHAR(100),
    user_email              VARCHAR(150),
    user_role               VARCHAR(50),
    api_key_id              VARCHAR(120),
    governance_key_id       VARCHAR(120),
    client_ip               VARCHAR(50),
    source_ip               VARCHAR(60),
    user_agent              VARCHAR(500),
    provider                VARCHAR(100),
    model_name              VARCHAR(120),
    model_version           VARCHAR(50),
    requested_model         VARCHAR(120),
    routed_model            VARCHAR(120),
    function_name           VARCHAR(255),
    tool_name               VARCHAR(150),
    source_system           VARCHAR(255),
    deployment_name         VARCHAR(255),
    request_payload         JSONB,
    prompt_text             TEXT,
    sanitized_prompt_text   TEXT,
    system_prompt           TEXT,
    messages                JSONB,
    request_parameters      JSONB,
    request_headers         JSONB,
    request_metadata        JSONB,
    input_token_estimate    INTEGER DEFAULT 0,
    prompt_char_count       INTEGER DEFAULT 0,
    num_messages            INTEGER DEFAULT 0,
    has_system_prompt       BOOLEAN DEFAULT FALSE,
    has_tool_definitions    BOOLEAN DEFAULT FALSE,
    has_images              BOOLEAN DEFAULT FALSE,
    pii_detected            BOOLEAN DEFAULT FALSE,
    pii_types               JSONB,
    pii_masked              BOOLEAN DEFAULT FALSE,
    pii_action_taken        VARCHAR(20),
    content_policy_flags    JSONB,
    received_at             TIMESTAMP DEFAULT NOW(),
    processing_started_at   TIMESTAMP,
    completed_at            TIMESTAMP,
    created_at              TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ai_responses (
    id                      BIGSERIAL PRIMARY KEY,
    response_id             VARCHAR(120) UNIQUE NOT NULL,
    request_id              VARCHAR(120) NOT NULL REFERENCES ai_requests(request_id) ON DELETE CASCADE,
    org_id                  VARCHAR(100) NOT NULL,
    project_id              VARCHAR(100),
    project_ref_id          VARCHAR(100),
    provider                VARCHAR(100),
    model_name              VARCHAR(120),
    model_version           VARCHAR(50),
    response_status         VARCHAR(30) DEFAULT 'pending',
    finish_reason           VARCHAR(50),
    is_streaming            BOOLEAN DEFAULT FALSE,
    is_cached               BOOLEAN DEFAULT FALSE,
    response_payload        JSONB,
    response_text           TEXT,
    tool_calls              JSONB,
    tool_call_results       JSONB,
    response_metadata       JSONB,
    output_char_count       INTEGER DEFAULT 0,
    num_tool_calls          INTEGER DEFAULT 0,
    error_code              VARCHAR(50),
    error_type              VARCHAR(100),
    error_message           TEXT,
    provider_request_id     VARCHAR(255),
    provider_response_id    VARCHAR(255),
    output_pii_detected     BOOLEAN DEFAULT FALSE,
    output_pii_types        JSONB,
    response_started_at     TIMESTAMP,
    response_completed_at   TIMESTAMP,
    first_token_at          TIMESTAMP,
    latency_ms              INTEGER DEFAULT 0,
    ttft_ms                 INTEGER DEFAULT 0,
    created_at              TIMESTAMP DEFAULT NOW()
);

-- Content safety flags stored on the response (Azure Content Safety categories)
ALTER TABLE ai_responses
    ADD COLUMN IF NOT EXISTS hate_category_status      VARCHAR(30),
    ADD COLUMN IF NOT EXISTS sexual_category_status    VARCHAR(30),
    ADD COLUMN IF NOT EXISTS violence_category_status  VARCHAR(30),
    ADD COLUMN IF NOT EXISTS self_harm_category_status VARCHAR(30),
    ADD COLUMN IF NOT EXISTS safety_metadata           JSONB;

CREATE TABLE IF NOT EXISTS token_usage (
    id                      BIGSERIAL PRIMARY KEY,
    token_usage_id          VARCHAR(120) UNIQUE NOT NULL
                                DEFAULT concat('tu-', replace(gen_random_uuid()::text, '-', '')),
    request_id              VARCHAR(120) NOT NULL REFERENCES ai_requests(request_id) ON DELETE CASCADE,
    response_id             VARCHAR(120) REFERENCES ai_responses(response_id) ON DELETE SET NULL,
    org_id                  VARCHAR(100) NOT NULL,
    project_id              VARCHAR(100),
    project_ref_id          VARCHAR(100),
    provider                VARCHAR(100),
    model_name              VARCHAR(120),
    model_version           VARCHAR(50),
    prompt_tokens           INTEGER DEFAULT 0,
    completion_tokens       INTEGER DEFAULT 0,
    total_tokens            INTEGER DEFAULT 0,
    input_tokens            INTEGER DEFAULT 0,
    cached_tokens           INTEGER DEFAULT 0,
    uncached_tokens         INTEGER DEFAULT 0,
    output_tokens           INTEGER DEFAULT 0,
    reasoning_tokens        INTEGER DEFAULT 0,
    tool_definition_tokens  INTEGER DEFAULT 0,
    system_tokens           INTEGER DEFAULT 0,
    context_window_limit    INTEGER,
    context_utilization_pct NUMERIC(6, 3),
    raw_usage               JSONB,
    input_token_source      VARCHAR(30),
    output_token_source     VARCHAR(30),
    is_estimated            BOOLEAN DEFAULT FALSE,
    created_at              TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS request_cost (
    id                  BIGSERIAL PRIMARY KEY,
    cost_id             VARCHAR(120) UNIQUE NOT NULL
                            DEFAULT concat('cu-', replace(gen_random_uuid()::text, '-', '')),
    request_id          VARCHAR(120) NOT NULL REFERENCES ai_requests(request_id) ON DELETE CASCADE,
    response_id         VARCHAR(120) REFERENCES ai_responses(response_id) ON DELETE SET NULL,
    org_id              VARCHAR(100) NOT NULL,
    project_id          VARCHAR(100),
    project_ref_id      VARCHAR(100),
    provider            VARCHAR(100),
    model_name          VARCHAR(120),
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    total_tokens        INTEGER DEFAULT 0,
    input_token_cost    NUMERIC(14, 8) DEFAULT 0,
    cached_token_cost   NUMERIC(14, 8) DEFAULT 0,
    output_token_cost   NUMERIC(14, 8) DEFAULT 0,
    tool_cost           NUMERIC(14, 8) DEFAULT 0,
    infra_cost          NUMERIC(14, 8) DEFAULT 0,
    gateway_cost        NUMERIC(14, 8) DEFAULT 0,
    llm_cost            NUMERIC(14, 8) DEFAULT 0,
    total_cost          NUMERIC(14, 8) DEFAULT 0,
    currency            VARCHAR(10) DEFAULT 'USD',
    pricing_version     VARCHAR(50),
    pricing_snapshot    JSONB,
    cost_model_type     VARCHAR(50),
    discount_pct        NUMERIC(6, 3) DEFAULT 0,
    adjusted_total_cost NUMERIC(14, 8) DEFAULT 0,
    created_at          TIMESTAMP DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Aggregation / rollup tables (populated by APScheduler jobs)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS daily_org_summary (
    id                      BIGSERIAL PRIMARY KEY,
    org_id                  VARCHAR(100) NOT NULL,
    project_id              VARCHAR(100),
    -- tool_name stores the model_name value written by the daily aggregation job
    tool_name               VARCHAR(150) NOT NULL,
    date                    DATE NOT NULL,
    total_events            INTEGER DEFAULT 0,
    total_cost              NUMERIC(14, 6) DEFAULT 0,
    llm_cost                NUMERIC(14, 6) DEFAULT 0,
    infra_cost              NUMERIC(14, 6) DEFAULT 0,
    external_cost           NUMERIC(14, 6) DEFAULT 0,
    total_prompt_tokens     INTEGER DEFAULT 0,
    total_completion_tokens INTEGER DEFAULT 0,
    total_tokens            INTEGER DEFAULT 0,
    input_tokens            INTEGER DEFAULT 0,
    output_tokens           INTEGER DEFAULT 0,
    input_token_cost        NUMERIC(14, 6) DEFAULT 0,
    output_token_cost       NUMERIC(14, 6) DEFAULT 0,
    total_token_cost        NUMERIC(14, 6) DEFAULT 0,
    avg_latency_ms          INTEGER DEFAULT 0,
    success_count           INTEGER DEFAULT 0,
    failure_count           INTEGER DEFAULT 0,
    anomaly_count           INTEGER DEFAULT 0,
    misuse_count            INTEGER DEFAULT 0,
    total_input_mb          NUMERIC(12, 4) DEFAULT 0,
    total_output_mb         NUMERIC(12, 4) DEFAULT 0,
    avg_risk_score          NUMERIC(8, 2) DEFAULT 0,
    created_at              TIMESTAMP DEFAULT NOW(),
    UNIQUE (org_id, project_id, tool_name, date)
);

CREATE TABLE IF NOT EXISTS monthly_org_summary (
    id                      BIGSERIAL PRIMARY KEY,
    org_id                  VARCHAR(100) NOT NULL,
    project_id              VARCHAR(100),
    -- tool_name stores the model_name value rolled up from daily_org_summary
    tool_name               VARCHAR(150) NOT NULL,
    month                   DATE NOT NULL,
    total_events            INTEGER DEFAULT 0,
    total_cost              NUMERIC(14, 6) DEFAULT 0,
    llm_cost                NUMERIC(14, 6) DEFAULT 0,
    infra_cost              NUMERIC(14, 6) DEFAULT 0,
    external_cost           NUMERIC(14, 6) DEFAULT 0,
    total_tokens            INTEGER DEFAULT 0,
    input_tokens            INTEGER DEFAULT 0,
    output_tokens           INTEGER DEFAULT 0,
    input_token_cost        NUMERIC(14, 6) DEFAULT 0,
    output_token_cost       NUMERIC(14, 6) DEFAULT 0,
    total_token_cost        NUMERIC(14, 6) DEFAULT 0,
    total_prompt_tokens     INTEGER DEFAULT 0,
    total_completion_tokens INTEGER DEFAULT 0,
    avg_latency_ms          INTEGER DEFAULT 0,
    success_count           INTEGER DEFAULT 0,
    failure_count           INTEGER DEFAULT 0,
    anomaly_count           INTEGER DEFAULT 0,
    misuse_count            INTEGER DEFAULT 0,
    created_at              TIMESTAMP DEFAULT NOW(),
    UNIQUE (org_id, project_id, tool_name, month)
);

-- ---------------------------------------------------------------------------
-- Audit log
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_logs (
    id                  BIGSERIAL PRIMARY KEY,
    audit_id            VARCHAR(120) UNIQUE NOT NULL,
    org_id              VARCHAR(100) NOT NULL,
    project_id          VARCHAR(100),
    project_ref_id      VARCHAR(100),
    actor_type          VARCHAR(50) NOT NULL,
    actor_id            VARCHAR(100),
    actor_email         VARCHAR(150),
    actor_ip            VARCHAR(50),
    audit_category      VARCHAR(50) NOT NULL,
    audit_action        VARCHAR(100) NOT NULL,
    audit_status        VARCHAR(30) DEFAULT 'success',
    entity_type         VARCHAR(100),
    entity_id           VARCHAR(120),
    request_id          VARCHAR(120),
    trace_id            VARCHAR(120),
    old_value           JSONB,
    new_value           JSONB,
    change_summary      TEXT,
    policy_triggered    BOOLEAN DEFAULT FALSE,
    compliance_relevant BOOLEAN DEFAULT FALSE,
    requires_review     BOOLEAN DEFAULT FALSE,
    audit_metadata      JSONB,
    occurred_at         TIMESTAMP DEFAULT NOW(),
    created_at          TIMESTAMP DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

-- Audit
CREATE INDEX IF NOT EXISTS ix_audit_logs_policy
    ON audit_logs (org_id, audit_action, occurred_at)
    WHERE policy_triggered = TRUE;

CREATE INDEX IF NOT EXISTS ix_audit_logs_org_project
    ON audit_logs (org_id, project_id, occurred_at DESC);

-- Requests / responses
CREATE INDEX IF NOT EXISTS ix_ai_requests_org_project
    ON ai_requests (org_id, project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_ai_requests_governance_key
    ON ai_requests (governance_key_id);

CREATE INDEX IF NOT EXISTS ix_ai_requests_model
    ON ai_requests (model_name, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_ai_responses_request
    ON ai_responses (request_id);

-- Token & cost
CREATE INDEX IF NOT EXISTS ix_token_usage_request
    ON token_usage (request_id);

CREATE INDEX IF NOT EXISTS ix_token_usage_org_project
    ON token_usage (org_id, project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_request_cost_org_project
    ON request_cost (org_id, project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_request_cost_model
    ON request_cost (model_name, created_at DESC);

-- Dashboard rollups
CREATE INDEX IF NOT EXISTS ix_daily_org_summary_org_date
    ON daily_org_summary (org_id, date DESC);

CREATE INDEX IF NOT EXISTS ix_daily_org_summary_project_date
    ON daily_org_summary (project_id, date DESC);

CREATE INDEX IF NOT EXISTS ix_monthly_org_summary_org_month
    ON monthly_org_summary (org_id, month DESC);

CREATE INDEX IF NOT EXISTS ix_monthly_org_summary_project_month
    ON monthly_org_summary (project_id, month DESC);

-- Governance
CREATE INDEX IF NOT EXISTS ix_alerts_org_project
    ON alerts (org_id, project_id, created_at DESC);

-- Model pricing lookup
CREATE INDEX IF NOT EXISTS ix_model_pricing_lookup
    ON model_pricing (provider, model_name);

-- =============================================================================
-- REMOVED TABLES (vs original schema.sql)
-- ---------------------------------------------------------------------------
-- model_registry      — only used by /models admin endpoint; not core to proxy
-- tool_registry       — only used by cost_engine tool base-cost lookup; not critical
-- tool_connectors     — ingestion pipeline not active; user confirmed removal
-- connector_sync_logs — FK dependency on tool_connectors; removed with it
-- providers           — zero application references
-- model_versions      — zero application references
-- ai_routes           — zero application references
-- route_executions    — zero application references
-- project_model_usage — zero application references
-- provider_configs    — zero application references
-- usage_anomalies     — zero application references
-- decorator_registrations — zero application references
-- tool_api_inventory  — zero application references
-- telemetry_events    — no active ingest router
-- cost_breakdown      — child of telemetry_events
-- execution_pipeline  — child of telemetry_events
-- trace_model_usage   — child of telemetry_events
-- trace_tool_usage    — child of telemetry_events
-- data_security_logs  — child of telemetry_events
-- request_response_logs — child of telemetry_events
-- =============================================================================
