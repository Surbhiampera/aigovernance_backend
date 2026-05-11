-- =============================================================================
-- 004_consolidated_schema.sql
-- Full canonical DDL for the AI Governance Platform.
-- Represents the complete target database state after all migrations (001–003).
--
-- USE THIS FILE for fresh installations (replaces running 001 + 002 + 003).
-- For existing databases that already ran 001–002, run 003 only.
-- =============================================================================

-- =============================================================================
-- TIER 1: Identity & Tenancy
-- Organizations → Projects → Users → API Keys
-- =============================================================================

CREATE TABLE IF NOT EXISTS organizations (
    id           VARCHAR(100)  PRIMARY KEY,
    org_name     VARCHAR(150)  NOT NULL,
    plan_type    VARCHAR(50),
    budget_limit DECIMAL(14,6),
    created_at   TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS projects (
    id           VARCHAR(100)  PRIMARY KEY,
    org_id       VARCHAR(100)  NOT NULL REFERENCES organizations(id),
    project_name VARCHAR(150),
    environment  VARCHAR(50),
    created_at   TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS users (
    id         VARCHAR(100) PRIMARY KEY,
    org_id     VARCHAR(100) REFERENCES organizations(id),
    email      VARCHAR(150),
    role       VARCHAR(50),
    created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS api_keys (
    id         VARCHAR(120) PRIMARY KEY,
    org_id     VARCHAR(100) REFERENCES organizations(id),
    project_id VARCHAR(100) REFERENCES projects(id),
    key_name   VARCHAR(100),
    provider   VARCHAR(100),
    created_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_projects (
    user_id    VARCHAR(100) REFERENCES users(id),
    project_id VARCHAR(100) REFERENCES projects(id),
    role       VARCHAR(50),
    PRIMARY KEY (user_id, project_id)
);

-- =============================================================================
-- TIER 2: Tool & Model Catalog
-- =============================================================================

CREATE TABLE IF NOT EXISTS providers (
    id            VARCHAR(100) PRIMARY KEY,
    provider_name VARCHAR(150)
);

CREATE TABLE IF NOT EXISTS tool_registry (
    id         BIGSERIAL     PRIMARY KEY,
    tool_name  VARCHAR(150)  UNIQUE NOT NULL,
    tool_type  VARCHAR(50),
    vendor     VARCHAR(100),
    cost_model VARCHAR(50),
    base_cost  DECIMAL(12,6) DEFAULT 0,
    created_at TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS model_registry (
    id                 BIGSERIAL    PRIMARY KEY,
    model_name         VARCHAR(120) UNIQUE NOT NULL,
    provider           VARCHAR(100),
    model_type         VARCHAR(50),
    cost_per_1k_tokens DECIMAL(12,6) DEFAULT 0,
    created_at         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS model_pricing (
    id                BIGSERIAL    PRIMARY KEY,
    provider          VARCHAR(100),
    model_name        VARCHAR(120),
    input_cost_per_1k DECIMAL(12,6) DEFAULT 0,
    output_cost_per_1k DECIMAL(12,6) DEFAULT 0,
    currency          VARCHAR(10)  DEFAULT 'USD',
    effective_from    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (provider, model_name)
);

-- =============================================================================
-- TIER 3: Connectors & Ingestion
-- =============================================================================

CREATE TABLE IF NOT EXISTS tool_connectors (
    id                   BIGSERIAL    PRIMARY KEY,
    connector_name       VARCHAR(150) UNIQUE NOT NULL,
    tool_name            VARCHAR(150) NOT NULL,
    provider             VARCHAR(100),
    endpoint_url         VARCHAR(255),
    auth_type            VARCHAR(50),
    ingestion_mode       VARCHAR(50)  NOT NULL DEFAULT 'api',
    status               VARCHAR(30)  NOT NULL DEFAULT 'active',
    org_id               VARCHAR(100),
    project_id           VARCHAR(100),
    api_key              VARCHAR(500),
    last_ingested_at     TIMESTAMP,
    sync_enabled         BOOLEAN      DEFAULT TRUE,
    pull_interval_minutes INTEGER     DEFAULT 15,
    last_sync_status     VARCHAR(30),
    last_sync_error      TEXT,
    total_events_pulled  INTEGER      DEFAULT 0,
    created_at           TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS connector_sync_logs (
    id             BIGSERIAL    PRIMARY KEY,
    connector_id   BIGINT       NOT NULL REFERENCES tool_connectors(id),
    connector_name VARCHAR(150),
    sync_status    VARCHAR(30)  NOT NULL DEFAULT 'success',
    events_pulled  INTEGER      DEFAULT 0,
    error_message  TEXT,
    duration_ms    INTEGER      DEFAULT 0,
    created_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- TIER 4: Core Telemetry (source of truth for all governance data)
-- =============================================================================

CREATE TABLE IF NOT EXISTS telemetry_events (
    id              BIGSERIAL     PRIMARY KEY,
    event_id        VARCHAR(120)  UNIQUE NOT NULL,
    request_id      VARCHAR(120),
    trace_id        VARCHAR(120),

    -- Tenant context
    org_id          VARCHAR(100)  NOT NULL,
    project_id      VARCHAR(100),
    user_id         VARCHAR(100),
    api_key_id      VARCHAR(120),

    -- Tool & model identity
    tool_name       VARCHAR(150),
    provider        VARCHAR(100),
    model_name      VARCHAR(100),
    service_type    VARCHAR(50),
    component_name  VARCHAR(150),
    execution_type  VARCHAR(50),

    -- Decorator-captured context (set by GovernanceDecorator)
    function_name   VARCHAR(255),
    module_path     VARCHAR(500),
    decorator_type  VARCHAR(50),
    execution_env   VARCHAR(50)   DEFAULT 'production',
    sdk_version     VARCHAR(20),
    tool_version    VARCHAR(50),

    -- Execution status
    status          VARCHAR(30),

    -- Token usage
    prompt_tokens      INTEGER  DEFAULT 0,
    completion_tokens  INTEGER  DEFAULT 0,
    total_tokens       INTEGER  DEFAULT 0,

    -- Data volume
    input_data_size_mb  DECIMAL(12,4) DEFAULT 0,
    output_data_size_mb DECIMAL(12,4) DEFAULT 0,

    -- PII-masked input/output previews (first N chars)
    input_preview   TEXT,
    output_preview  TEXT,

    -- Costs (computed by CostEngine)
    llm_cost        DECIMAL(14,6) DEFAULT 0,
    infra_cost      DECIMAL(14,6) DEFAULT 0,
    external_cost   DECIMAL(14,6) DEFAULT 0,
    total_cost      DECIMAL(14,6) DEFAULT 0,

    -- Risk & governance signals
    risk_score              DECIMAL(8,2)  DEFAULT 0,
    anomaly_score           DECIMAL(8,2)  DEFAULT 0,
    misuse_detected         BOOLEAN       DEFAULT FALSE,
    abnormal_usage_spike    BOOLEAN       DEFAULT FALSE,

    -- Timing
    latency_ms   INTEGER   DEFAULT 0,
    started_at   TIMESTAMP,
    completed_at TIMESTAMP,

    -- Flexible metadata
    tags          JSON,
    metadata_json JSON,
    raw_usage_json JSON,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_telemetry_org_project ON telemetry_events (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_trace       ON telemetry_events (trace_id);
CREATE INDEX IF NOT EXISTS idx_telemetry_created     ON telemetry_events (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_tool        ON telemetry_events (tool_name);
CREATE INDEX IF NOT EXISTS idx_telemetry_model       ON telemetry_events (model_name);
CREATE INDEX IF NOT EXISTS idx_telemetry_fn          ON telemetry_events (function_name);

-- =============================================================================
-- TIER 5: Cost, Pipeline, & Trace Detail
-- =============================================================================

CREATE TABLE IF NOT EXISTS cost_breakdown (
    id             BIGSERIAL    PRIMARY KEY,
    event_id       VARCHAR(120) REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    cost_type      VARCHAR(50)  NOT NULL,
    component_name VARCHAR(150),
    unit_cost      DECIMAL(12,6) DEFAULT 0,
    quantity       DECIMAL(12,6) DEFAULT 0,
    total_cost     DECIMAL(12,6) DEFAULT 0,
    created_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS execution_pipeline (
    id              BIGSERIAL    PRIMARY KEY,
    event_id        VARCHAR(120) REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    stage_order     INTEGER      DEFAULT 0,
    stage_name      VARCHAR(150) NOT NULL,
    system_name     VARCHAR(150),
    status          VARCHAR(30),
    stage_latency_ms INTEGER     DEFAULT 0,
    retry_count     INTEGER      DEFAULT 0,
    details         JSON,
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trace_model_usage (
    id             BIGSERIAL    PRIMARY KEY,
    event_id       VARCHAR(120) REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    trace_id       VARCHAR(120),
    org_id         VARCHAR(100) NOT NULL,
    project_id     VARCHAR(100),
    model_name     VARCHAR(120) NOT NULL,
    provider       VARCHAR(100),
    -- Decorator context
    function_name  VARCHAR(255),
    call_sequence  INTEGER      DEFAULT 0,
    -- Usage
    input_tokens   INTEGER      DEFAULT 0,
    output_tokens  INTEGER      DEFAULT 0,
    total_tokens   INTEGER      DEFAULT 0,
    llm_cost       DECIMAL(14,6) DEFAULT 0,
    latency_ms     INTEGER      DEFAULT 0,
    created_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trace_model_event ON trace_model_usage (event_id);
CREATE INDEX IF NOT EXISTS idx_trace_model_org   ON trace_model_usage (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_trace_model_fn    ON trace_model_usage (function_name);

CREATE TABLE IF NOT EXISTS trace_tool_usage (
    id               BIGSERIAL    PRIMARY KEY,
    event_id         VARCHAR(120) REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    trace_id         VARCHAR(120),
    org_id           VARCHAR(100) NOT NULL,
    project_id       VARCHAR(100),
    tool_name        VARCHAR(150) NOT NULL,
    tool_type        VARCHAR(50),
    invocation_count INTEGER      DEFAULT 1,
    execution_time_ms INTEGER     DEFAULT 0,
    cost             DECIMAL(14,6) DEFAULT 0,
    created_at       TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- TIER 6: Decorator Framework
-- Auto-populated by the GovernanceDecorator SDK integration.
-- =============================================================================

-- Registry of every decorated function that has called in.
CREATE TABLE IF NOT EXISTS decorator_registrations (
    id             BIGSERIAL    PRIMARY KEY,
    org_id         VARCHAR(100) NOT NULL,
    project_id     VARCHAR(100),
    tool_name      VARCHAR(150) NOT NULL,
    function_name  VARCHAR(255) NOT NULL,
    module_path    VARCHAR(500),
    decorator_type VARCHAR(50)  NOT NULL DEFAULT 'trace',
    sdk_version    VARCHAR(20),
    python_version VARCHAR(20),
    execution_env  VARCHAR(50)  DEFAULT 'production',
    first_seen     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    last_seen      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    call_count     BIGINT       DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_dec_reg_unique
    ON decorator_registrations (org_id, tool_name, function_name, COALESCE(module_path, ''));

CREATE INDEX IF NOT EXISTS idx_dec_reg_org_tool
    ON decorator_registrations (org_id, tool_name);

-- Daily per-project, per-model aggregation (built by daily-aggregation worker).
CREATE TABLE IF NOT EXISTS project_model_usage (
    id                      BIGSERIAL     PRIMARY KEY,
    org_id                  VARCHAR(100)  NOT NULL,
    project_id              VARCHAR(100),
    model_name              VARCHAR(120)  NOT NULL,
    provider                VARCHAR(100),
    date                    DATE          NOT NULL,
    call_count              INTEGER       DEFAULT 0,
    total_prompt_tokens     INTEGER       DEFAULT 0,
    total_completion_tokens INTEGER       DEFAULT 0,
    total_tokens            INTEGER       DEFAULT 0,
    total_cost              DECIMAL(14,6) DEFAULT 0,
    avg_latency_ms          INTEGER       DEFAULT 0,
    success_count           INTEGER       DEFAULT 0,
    error_count             INTEGER       DEFAULT 0,
    created_at              TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_proj_model_usage_unique
    ON project_model_usage (org_id, COALESCE(project_id, '__none__'), model_name, date);

CREATE INDEX IF NOT EXISTS idx_proj_model_usage_lookup
    ON project_model_usage (org_id, project_id, date DESC);

-- Auto-discovered function catalog per tool (upserted on each call).
CREATE TABLE IF NOT EXISTS tool_api_inventory (
    id             BIGSERIAL     PRIMARY KEY,
    org_id         VARCHAR(100)  NOT NULL,
    project_id     VARCHAR(100),
    tool_name      VARCHAR(150)  NOT NULL,
    function_name  VARCHAR(255)  NOT NULL,
    module_path    VARCHAR(500),
    decorator_type VARCHAR(50),
    description    TEXT,
    first_seen     TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    last_seen      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    total_calls    BIGINT        DEFAULT 0,
    success_calls  BIGINT        DEFAULT 0,
    error_calls    BIGINT        DEFAULT 0,
    avg_latency_ms INTEGER       DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tool_api_inv_unique
    ON tool_api_inventory (org_id, tool_name, function_name);

CREATE INDEX IF NOT EXISTS idx_tool_api_inv_lookup
    ON tool_api_inventory (org_id, project_id, tool_name);

-- Per-call input/output audit trail (PII-masked).
CREATE TABLE IF NOT EXISTS request_response_logs (
    id                BIGSERIAL    PRIMARY KEY,
    event_id          VARCHAR(120) REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    function_name     VARCHAR(255),
    input_preview     TEXT,
    output_preview    TEXT,
    input_size_bytes  INTEGER      DEFAULT 0,
    output_size_bytes INTEGER      DEFAULT 0,
    input_keys        TEXT,
    output_keys       TEXT,
    pii_detected      BOOLEAN      DEFAULT FALSE,
    pii_fields        TEXT,
    created_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_req_resp_event_id ON request_response_logs (event_id);

-- =============================================================================
-- TIER 7: Security & Risk
-- =============================================================================

CREATE TABLE IF NOT EXISTS data_security_logs (
    id                       BIGSERIAL    PRIMARY KEY,
    event_id                 VARCHAR(120) REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    org_id                   VARCHAR(100),
    project_id               VARCHAR(100),
    pii_detected             BOOLEAN      DEFAULT FALSE,
    pii_type                 VARCHAR(100),
    data_out_violation       BOOLEAN      DEFAULT FALSE,
    misuse_pattern_detected  BOOLEAN      DEFAULT FALSE,
    abnormal_usage_spike     BOOLEAN      DEFAULT FALSE,
    masking_applied          BOOLEAN      DEFAULT FALSE,
    risk_score               DECIMAL(8,2) DEFAULT 0,
    data_in_mb               DECIMAL(12,4) DEFAULT 0,
    data_out_mb              DECIMAL(12,4) DEFAULT 0,
    created_at               TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sec_log_org ON data_security_logs (org_id, project_id);

CREATE TABLE IF NOT EXISTS usage_anomalies (
    id             BIGSERIAL     PRIMARY KEY,
    org_id         VARCHAR(100)  NOT NULL,
    project_id     VARCHAR(100),
    tool_name      VARCHAR(150)  NOT NULL,
    event_id       VARCHAR(120),
    anomaly_type   VARCHAR(60)   NOT NULL,
    severity       VARCHAR(20)   NOT NULL DEFAULT 'medium',
    anomaly_score  DECIMAL(8,2)  DEFAULT 0,
    baseline_value DECIMAL(14,6) DEFAULT 0,
    observed_value DECIMAL(14,6) DEFAULT 0,
    message        TEXT,
    status         VARCHAR(20)   NOT NULL DEFAULT 'open',
    created_at     TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_anomaly_org    ON usage_anomalies (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_anomaly_status ON usage_anomalies (status);

-- =============================================================================
-- TIER 8: Governance Rules & Alerts
-- =============================================================================

CREATE TABLE IF NOT EXISTS governance_rules (
    id              BIGSERIAL     PRIMARY KEY,
    rule_name       VARCHAR(150)  UNIQUE NOT NULL,
    description     TEXT,
    metric_name     VARCHAR(100)  NOT NULL,
    operator        VARCHAR(20)   NOT NULL DEFAULT '>',
    threshold_value DECIMAL(14,6) NOT NULL DEFAULT 0,
    severity        VARCHAR(20)   NOT NULL DEFAULT 'medium',
    scope_level     VARCHAR(30)   NOT NULL DEFAULT 'organization',
    scope_reference VARCHAR(150),
    is_active       BOOLEAN       DEFAULT TRUE,
    org_id          VARCHAR(100),
    project_id      VARCHAR(100),
    created_at      TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL    PRIMARY KEY,
    org_id          VARCHAR(100),
    project_id      VARCHAR(100),
    rule_id         BIGINT,
    alert_type      VARCHAR(100),
    severity        VARCHAR(50),
    message         TEXT,
    threshold_value DECIMAL(10,2),
    actual_value    DECIMAL(10,2),
    status          VARCHAR(50)  DEFAULT 'active',
    telemetry_id    BIGINT       REFERENCES telemetry_events(id),
    tool_name       VARCHAR(150),
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_alerts_org    ON alerts (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status);

-- =============================================================================
-- TIER 9: Budget & Rate Limits
-- =============================================================================

CREATE TABLE IF NOT EXISTS budgets (
    id                      BIGSERIAL    PRIMARY KEY,
    org_id                  VARCHAR(100) REFERENCES organizations(id),
    project_id              VARCHAR(100) REFERENCES projects(id),
    budget_type             VARCHAR(50),
    limit_amount            DECIMAL(14,6),
    alert_threshold_percent INTEGER      DEFAULT 80,
    created_at              TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rate_limits (
    id                  BIGSERIAL    PRIMARY KEY,
    org_id              VARCHAR(100),
    tool_name           VARCHAR(150),
    max_requests_per_min INTEGER,
    max_tokens_per_day  INTEGER,
    created_at          TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS rate_limit_violations (
    id             BIGSERIAL    PRIMARY KEY,
    org_id         VARCHAR(100),
    project_id     VARCHAR(100),
    tool_name      VARCHAR(150),
    violation_type VARCHAR(50),
    observed_value INTEGER,
    limit_value    INTEGER,
    created_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- TIER 10: Upload & File Tracking
-- =============================================================================

CREATE TABLE IF NOT EXISTS upload_data (
    id            BIGSERIAL    PRIMARY KEY,
    org_id        VARCHAR(100) REFERENCES organizations(id),
    project_id    VARCHAR(100) REFERENCES projects(id),
    user_id       VARCHAR(100) REFERENCES users(id),
    file_name     TEXT,
    file_type     VARCHAR(50),
    file_size_mb  DECIMAL(10,2),
    storage_path  TEXT,
    upload_source VARCHAR(100),
    created_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- TIER 11: Pre-Aggregated Summaries (built by APScheduler workers)
-- =============================================================================

CREATE TABLE IF NOT EXISTS daily_org_summary (
    id                      BIGSERIAL     PRIMARY KEY,
    org_id                  VARCHAR(100)  NOT NULL,
    project_id              VARCHAR(100),
    tool_name               VARCHAR(150)  NOT NULL,
    date                    DATE          NOT NULL,
    total_events            INTEGER       DEFAULT 0,
    total_cost              DECIMAL(14,6) DEFAULT 0,
    llm_cost                DECIMAL(14,6) DEFAULT 0,
    infra_cost              DECIMAL(14,6) DEFAULT 0,
    external_cost           DECIMAL(14,6) DEFAULT 0,
    total_prompt_tokens     INTEGER       DEFAULT 0,
    total_completion_tokens INTEGER       DEFAULT 0,
    total_tokens            INTEGER       DEFAULT 0,
    avg_latency_ms          INTEGER       DEFAULT 0,
    success_count           INTEGER       DEFAULT 0,
    failure_count           INTEGER       DEFAULT 0,
    anomaly_count           INTEGER       DEFAULT 0,
    misuse_count            INTEGER       DEFAULT 0,
    total_input_mb          DECIMAL(12,4) DEFAULT 0,
    total_output_mb         DECIMAL(12,4) DEFAULT 0,
    avg_risk_score          DECIMAL(8,2)  DEFAULT 0,
    created_at              TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (org_id, project_id, tool_name, date)
);

CREATE TABLE IF NOT EXISTS monthly_org_summary (
    id                      BIGSERIAL     PRIMARY KEY,
    org_id                  VARCHAR(100)  NOT NULL,
    project_id              VARCHAR(100),
    tool_name               VARCHAR(150)  NOT NULL,
    month                   DATE          NOT NULL,
    total_events            INTEGER       DEFAULT 0,
    total_cost              DECIMAL(14,6) DEFAULT 0,
    llm_cost                DECIMAL(14,6) DEFAULT 0,
    infra_cost              DECIMAL(14,6) DEFAULT 0,
    external_cost           DECIMAL(14,6) DEFAULT 0,
    total_tokens            INTEGER       DEFAULT 0,
    total_prompt_tokens     INTEGER       DEFAULT 0,
    total_completion_tokens INTEGER       DEFAULT 0,
    avg_latency_ms          INTEGER       DEFAULT 0,
    success_count           INTEGER       DEFAULT 0,
    failure_count           INTEGER       DEFAULT 0,
    anomaly_count           INTEGER       DEFAULT 0,
    misuse_count            INTEGER       DEFAULT 0,
    created_at              TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (org_id, project_id, tool_name, month)
);

-- =============================================================================
-- TIER 12: Tool-Specific Extension Tables
-- Each AI tool can define its own detail table linked by event_id.
-- =============================================================================

CREATE TABLE IF NOT EXISTS email_agent_logs (
    id                   BIGSERIAL    PRIMARY KEY,
    event_id             VARCHAR(120) REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    email_id             VARCHAR(255),
    sender_domain        VARCHAR(150),
    intent               VARCHAR(100),
    intent_confidence    DECIMAL(5,3),
    pii_masked           BOOLEAN      DEFAULT FALSE,
    masking_types        JSON,
    draft_generated      BOOLEAN      DEFAULT FALSE,
    auto_replied         BOOLEAN      DEFAULT FALSE,
    classification_model VARCHAR(100),
    draft_model          VARCHAR(100),
    stage_latencies      JSON,
    pipeline_status      VARCHAR(30),
    created_at           TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- =============================================================================
-- Summary of tables (26 total):
--
-- Tenancy:      organizations, projects, users, api_keys, user_projects
-- Catalog:      providers, tool_registry, model_registry, model_pricing
-- Connectors:   tool_connectors, connector_sync_logs
-- Telemetry:    telemetry_events, cost_breakdown, execution_pipeline,
--               trace_model_usage, trace_tool_usage
-- Decorators:   decorator_registrations, project_model_usage,
--               tool_api_inventory, request_response_logs
-- Security:     data_security_logs, usage_anomalies
-- Governance:   governance_rules, alerts
-- Limits:       budgets, rate_limits, rate_limit_violations
-- Files:        upload_data
-- Summaries:    daily_org_summary, monthly_org_summary
-- Extensions:   email_agent_logs
-- =============================================================================
