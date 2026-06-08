-- =============================================================================
-- AI Governance Proxy — Full Database Schema
-- PostgreSQL (Aiven). Run once on a fresh database.
-- Columns added via _SAFE_ALTERS at startup are already included here so this
-- file stays the authoritative source of truth.
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
-- API keys (proxy governance keys + provider keys)
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
-- Providers & model catalogue
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS providers (
    id              VARCHAR(100) PRIMARY KEY,
    provider_name   VARCHAR(150)
);

CREATE TABLE IF NOT EXISTS model_registry (
    id                  BIGSERIAL PRIMARY KEY,
    model_name          VARCHAR(120) UNIQUE NOT NULL,
    provider            VARCHAR(100),
    model_type          VARCHAR(50),
    cost_per_1k_tokens  NUMERIC(12, 6) DEFAULT 0,
    created_at          TIMESTAMP DEFAULT NOW()
);

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

CREATE TABLE IF NOT EXISTS model_versions (
    id                  BIGSERIAL PRIMARY KEY,
    provider            VARCHAR(100) NOT NULL,
    model_name          VARCHAR(120) NOT NULL,
    version_tag         VARCHAR(50),
    full_model_id       VARCHAR(200),
    context_window      INTEGER,
    max_output_tokens   INTEGER,
    supports_functions  BOOLEAN DEFAULT FALSE,
    supports_vision     BOOLEAN DEFAULT FALSE,
    supports_streaming  BOOLEAN DEFAULT FALSE,
    supports_reasoning  BOOLEAN DEFAULT FALSE,
    is_active           BOOLEAN DEFAULT TRUE,
    released_at         TIMESTAMP,
    deprecated_at       TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW()
);

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
-- Tool registry & connectors
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tool_registry (
    id          BIGSERIAL PRIMARY KEY,
    tool_name   VARCHAR(150) UNIQUE NOT NULL,
    tool_type   VARCHAR(50),
    vendor      VARCHAR(100),
    cost_model  VARCHAR(50),
    base_cost   NUMERIC(12, 6) DEFAULT 0,
    project_id  VARCHAR(100),
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tool_connectors (
    id                      BIGSERIAL PRIMARY KEY,
    connector_name          VARCHAR(150) UNIQUE NOT NULL,
    tool_name               VARCHAR(150) NOT NULL,
    provider                VARCHAR(100),
    endpoint_url            VARCHAR(255),
    auth_type               VARCHAR(50),
    ingestion_mode          VARCHAR(50) NOT NULL DEFAULT 'api',
    status                  VARCHAR(30) NOT NULL DEFAULT 'active',
    org_id                  VARCHAR(100),
    project_id              VARCHAR(100),
    api_key                 VARCHAR(500),
    last_ingested_at        TIMESTAMP,
    sync_enabled            BOOLEAN DEFAULT TRUE,
    pull_interval_minutes   INTEGER DEFAULT 15,
    last_sync_status        VARCHAR(30),
    last_sync_error         TEXT,
    total_events_pulled     INTEGER DEFAULT 0,
    created_at              TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS connector_sync_logs (
    id              BIGSERIAL PRIMARY KEY,
    connector_id    BIGINT NOT NULL REFERENCES tool_connectors(id),
    connector_name  VARCHAR(150),
    sync_status     VARCHAR(30) NOT NULL DEFAULT 'success',
    events_pulled   INTEGER DEFAULT 0,
    error_message   TEXT,
    duration_ms     INTEGER DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW()
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

CREATE TABLE IF NOT EXISTS provider_configs (
    id              BIGSERIAL PRIMARY KEY,
    config_id       VARCHAR(120) UNIQUE NOT NULL,
    org_id          VARCHAR(100) NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    project_id      VARCHAR(100) REFERENCES projects(id) ON DELETE SET NULL,
    provider        VARCHAR(100) NOT NULL,
    api_key         TEXT NOT NULL,
    api_key_hint    VARCHAR(20),
    base_url        VARCHAR(500),
    model_allowlist JSONB,
    max_rpm         INTEGER,
    max_tpm         INTEGER,
    is_active       BOOLEAN DEFAULT TRUE,
    created_by      VARCHAR(100),
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
-- Telemetry events & derived tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS telemetry_events (
    id                      BIGSERIAL PRIMARY KEY,
    event_id                VARCHAR(120) UNIQUE NOT NULL,
    request_id              VARCHAR(120),
    trace_id                VARCHAR(120),
    org_id                  VARCHAR(100) NOT NULL,
    project_id              VARCHAR(100),
    user_id                 VARCHAR(100),
    api_key_id              VARCHAR(120),
    tool_name               VARCHAR(150),
    provider                VARCHAR(100),
    model_name              VARCHAR(100),
    service_type            VARCHAR(50),
    component_name          VARCHAR(150),
    execution_type          VARCHAR(50),
    function_name           VARCHAR(255),
    module_path             VARCHAR(500),
    decorator_type          VARCHAR(50),
    execution_env           VARCHAR(50),
    sdk_version             VARCHAR(20),
    tool_version            VARCHAR(50),
    status                  VARCHAR(30),
    prompt_tokens           INTEGER DEFAULT 0,
    completion_tokens       INTEGER DEFAULT 0,
    total_tokens            INTEGER DEFAULT 0,
    input_token_cost        NUMERIC(14, 6) DEFAULT 0,
    output_token_cost       NUMERIC(14, 6) DEFAULT 0,
    total_token_cost        NUMERIC(14, 6) DEFAULT 0,
    input_data_size_mb      NUMERIC(12, 4) DEFAULT 0,
    output_data_size_mb     NUMERIC(12, 4) DEFAULT 0,
    input_preview           TEXT,
    output_preview          TEXT,
    llm_cost                NUMERIC(14, 6) DEFAULT 0,
    infra_cost              NUMERIC(14, 6) DEFAULT 0,
    external_cost           NUMERIC(14, 6) DEFAULT 0,
    total_cost              NUMERIC(14, 6) DEFAULT 0,
    risk_score              NUMERIC(8, 2) DEFAULT 0,
    anomaly_score           NUMERIC(8, 2) DEFAULT 0,
    misuse_detected         BOOLEAN DEFAULT FALSE,
    abnormal_usage_spike    BOOLEAN DEFAULT FALSE,
    latency_ms              INTEGER DEFAULT 0,
    started_at              TIMESTAMP,
    completed_at            TIMESTAMP,
    tags                    JSONB,
    metadata_json           JSONB,
    raw_usage_json          JSONB,
    created_at              TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS cost_breakdown (
    id              BIGSERIAL PRIMARY KEY,
    event_id        VARCHAR(120) NOT NULL REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    cost_type       VARCHAR(50) NOT NULL,
    component_name  VARCHAR(150),
    unit_cost       NUMERIC(12, 6) DEFAULT 0,
    quantity        NUMERIC(12, 6) DEFAULT 0,
    total_cost      NUMERIC(12, 6) DEFAULT 0,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS execution_pipeline (
    id                  BIGSERIAL PRIMARY KEY,
    event_id            VARCHAR(120) NOT NULL REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    stage_order         INTEGER DEFAULT 0,
    stage_name          VARCHAR(150) NOT NULL,
    system_name         VARCHAR(150),
    status              VARCHAR(30),
    stage_latency_ms    INTEGER DEFAULT 0,
    retry_count         INTEGER DEFAULT 0,
    details             JSONB,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trace_model_usage (
    id                  BIGSERIAL PRIMARY KEY,
    event_id            VARCHAR(120) NOT NULL REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    trace_id            VARCHAR(120),
    org_id              VARCHAR(100) NOT NULL,
    project_id          VARCHAR(100),
    model_name          VARCHAR(120) NOT NULL,
    provider            VARCHAR(100),
    function_name       VARCHAR(255),
    call_sequence       INTEGER DEFAULT 0,
    input_tokens        INTEGER DEFAULT 0,
    output_tokens       INTEGER DEFAULT 0,
    total_tokens        INTEGER DEFAULT 0,
    input_token_cost    NUMERIC(14, 6) DEFAULT 0,
    output_token_cost   NUMERIC(14, 6) DEFAULT 0,
    total_token_cost    NUMERIC(14, 6) DEFAULT 0,
    llm_cost            NUMERIC(14, 6) DEFAULT 0,
    latency_ms          INTEGER DEFAULT 0,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trace_tool_usage (
    id                  BIGSERIAL PRIMARY KEY,
    event_id            VARCHAR(120) NOT NULL REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    trace_id            VARCHAR(120),
    org_id              VARCHAR(100) NOT NULL,
    project_id          VARCHAR(100),
    tool_name           VARCHAR(150) NOT NULL,
    tool_type           VARCHAR(50),
    invocation_count    INTEGER DEFAULT 1,
    execution_time_ms   INTEGER DEFAULT 0,
    cost                NUMERIC(14, 6) DEFAULT 0,
    created_at          TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS data_security_logs (
    id                          BIGSERIAL PRIMARY KEY,
    event_id                    VARCHAR(120) NOT NULL REFERENCES telemetry_events(event_id),
    org_id                      VARCHAR(100),
    project_id                  VARCHAR(100),
    pii_detected                BOOLEAN DEFAULT FALSE,
    pii_type                    VARCHAR(100),
    data_out_violation          BOOLEAN DEFAULT FALSE,
    misuse_pattern_detected     BOOLEAN DEFAULT FALSE,
    abnormal_usage_spike        BOOLEAN DEFAULT FALSE,
    masking_applied             BOOLEAN DEFAULT FALSE,
    risk_score                  NUMERIC(8, 2) DEFAULT 0,
    data_in_mb                  NUMERIC(12, 4) DEFAULT 0,
    data_out_mb                 NUMERIC(12, 4) DEFAULT 0,
    created_at                  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage_anomalies (
    id              BIGSERIAL PRIMARY KEY,
    org_id          VARCHAR(100) NOT NULL,
    project_id      VARCHAR(100),
    tool_name       VARCHAR(150) NOT NULL,
    event_id        VARCHAR(120),
    anomaly_type    VARCHAR(60) NOT NULL,
    severity        VARCHAR(20) NOT NULL DEFAULT 'medium',
    anomaly_score   NUMERIC(8, 2) DEFAULT 0,
    baseline_value  NUMERIC(14, 6) DEFAULT 0,
    observed_value  NUMERIC(14, 6) DEFAULT 0,
    message         TEXT,
    status          VARCHAR(20) NOT NULL DEFAULT 'open',
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS request_response_logs (
    id                      BIGSERIAL PRIMARY KEY,
    event_id                VARCHAR(120) REFERENCES telemetry_events(event_id) ON DELETE CASCADE,
    trace_id                VARCHAR(120),
    user_id                 VARCHAR(100),
    function_name           VARCHAR(255),
    route                   VARCHAR(255),
    model_name              VARCHAR(120),
    provider                VARCHAR(100),
    prompt_tokens           INTEGER,
    completion_tokens       INTEGER,
    total_tokens            INTEGER,
    latency_ms              INTEGER,
    estimated_cost_usd      NUMERIC(14, 8),
    input_preview           TEXT,
    output_preview          TEXT,
    input_size_bytes        INTEGER DEFAULT 0,
    output_size_bytes       INTEGER DEFAULT 0,
    input_keys              VARCHAR(500),
    output_keys             VARCHAR(500),
    pii_detected            BOOLEAN DEFAULT FALSE,
    pii_fields              VARCHAR(500),
    created_at              TIMESTAMP DEFAULT NOW()
);

-- ---------------------------------------------------------------------------
-- Aggregation / rollup tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS daily_org_summary (
    id                      BIGSERIAL PRIMARY KEY,
    org_id                  VARCHAR(100) NOT NULL,
    project_id              VARCHAR(100),
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

CREATE TABLE IF NOT EXISTS project_model_usage (
    id                      BIGSERIAL PRIMARY KEY,
    org_id                  VARCHAR(100) NOT NULL,
    project_id              VARCHAR(100),
    model_name              VARCHAR(120) NOT NULL,
    provider                VARCHAR(100),
    date                    DATE NOT NULL,
    call_count              INTEGER DEFAULT 0,
    total_prompt_tokens     INTEGER DEFAULT 0,
    total_completion_tokens INTEGER DEFAULT 0,
    total_tokens            INTEGER DEFAULT 0,
    input_tokens            INTEGER DEFAULT 0,
    output_tokens           INTEGER DEFAULT 0,
    input_token_cost        NUMERIC(14, 6) DEFAULT 0,
    output_token_cost       NUMERIC(14, 6) DEFAULT 0,
    total_token_cost        NUMERIC(14, 6) DEFAULT 0,
    total_cost              NUMERIC(14, 6) DEFAULT 0,
    avg_latency_ms          INTEGER DEFAULT 0,
    success_count           INTEGER DEFAULT 0,
    error_count             INTEGER DEFAULT 0,
    created_at              TIMESTAMP DEFAULT NOW(),
    UNIQUE (org_id, project_id, model_name, date)
);

-- ---------------------------------------------------------------------------
-- SDK / decorator inventory
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS decorator_registrations (
    id              BIGSERIAL PRIMARY KEY,
    org_id          VARCHAR(100) NOT NULL,
    project_id      VARCHAR(100),
    tool_name       VARCHAR(150) NOT NULL,
    function_name   VARCHAR(255) NOT NULL,
    module_path     VARCHAR(500),
    decorator_type  VARCHAR(50) NOT NULL DEFAULT 'trace',
    sdk_version     VARCHAR(20),
    python_version  VARCHAR(20),
    execution_env   VARCHAR(50) DEFAULT 'production',
    first_seen      TIMESTAMP DEFAULT NOW(),
    last_seen       TIMESTAMP DEFAULT NOW(),
    call_count      BIGINT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tool_api_inventory (
    id              BIGSERIAL PRIMARY KEY,
    org_id          VARCHAR(100) NOT NULL,
    project_id      VARCHAR(100),
    tool_name       VARCHAR(150) NOT NULL,
    function_name   VARCHAR(255) NOT NULL,
    module_path     VARCHAR(500),
    decorator_type  VARCHAR(50),
    description     TEXT,
    first_seen      TIMESTAMP DEFAULT NOW(),
    last_seen       TIMESTAMP DEFAULT NOW(),
    total_calls     BIGINT DEFAULT 0,
    success_calls   BIGINT DEFAULT 0,
    error_calls     BIGINT DEFAULT 0,
    avg_latency_ms  INTEGER DEFAULT 0,
    UNIQUE (org_id, project_id, tool_name, function_name)
);

-- ---------------------------------------------------------------------------
-- Proxy core: routes, requests, responses, token usage, costs
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS ai_routes (
    id                          BIGSERIAL PRIMARY KEY,
    route_id                    VARCHAR(120) UNIQUE NOT NULL,
    org_id                      VARCHAR(100) NOT NULL REFERENCES organizations(id),
    project_id                  VARCHAR(100) REFERENCES projects(id),
    project_ref_id              VARCHAR(100),
    route_name                  VARCHAR(255) NOT NULL,
    route_path                  VARCHAR(500),
    route_type                  VARCHAR(50),
    http_method                 VARCHAR(10) DEFAULT 'POST',
    upstream_url                VARCHAR(500),
    default_provider            VARCHAR(100),
    default_model               VARCHAR(120),
    allowed_models              JSONB,
    model_selection_strategy    VARCHAR(50),
    description                 TEXT,
    tags                        JSONB,
    is_active                   BOOLEAN DEFAULT TRUE,
    requires_auth               BOOLEAN DEFAULT TRUE,
    created_at                  TIMESTAMP DEFAULT NOW(),
    updated_at                  TIMESTAMP DEFAULT NOW()
);

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
    route_id                VARCHAR(120) REFERENCES ai_routes(route_id) ON DELETE SET NULL,
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

CREATE TABLE IF NOT EXISTS route_executions (
    id                  BIGSERIAL PRIMARY KEY,
    execution_id        VARCHAR(120) UNIQUE NOT NULL,
    request_id          VARCHAR(120) NOT NULL REFERENCES ai_requests(request_id) ON DELETE CASCADE,
    route_id            VARCHAR(120) REFERENCES ai_routes(route_id) ON DELETE SET NULL,
    org_id              VARCHAR(100) NOT NULL,
    project_id          VARCHAR(100),
    project_ref_id      VARCHAR(100),
    execution_status    VARCHAR(30) DEFAULT 'pending',
    routing_strategy    VARCHAR(50),
    routing_reason      TEXT,
    original_model      VARCHAR(120),
    selected_model      VARCHAR(120),
    selected_provider   VARCHAR(100),
    proxy_type          VARCHAR(50),
    upstream_url        VARCHAR(500),
    upstream_request_id VARCHAR(255),
    pipeline_stages     JSONB,
    attempt_number      INTEGER DEFAULT 1,
    retry_count         INTEGER DEFAULT 0,
    retry_reasons       JSONB,
    last_failure_reason TEXT,
    total_latency_ms        INTEGER DEFAULT 0,
    routing_latency_ms      INTEGER DEFAULT 0,
    proxy_latency_ms        INTEGER DEFAULT 0,
    upstream_latency_ms     INTEGER DEFAULT 0,
    governance_check_ms     INTEGER DEFAULT 0,
    quota_checked       BOOLEAN DEFAULT FALSE,
    quota_remaining     INTEGER,
    policy_applied      JSONB,
    blocked_by_policy   BOOLEAN DEFAULT FALSE,
    block_reason        TEXT,
    started_at          TIMESTAMP,
    completed_at        TIMESTAMP,
    created_at          TIMESTAMP DEFAULT NOW()
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

CREATE INDEX IF NOT EXISTS ix_audit_logs_policy
    ON audit_logs (org_id, audit_action, occurred_at)
    WHERE policy_triggered = TRUE;

CREATE INDEX IF NOT EXISTS ix_ai_requests_org_project
    ON ai_requests (org_id, project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_ai_requests_governance_key
    ON ai_requests (governance_key_id);

CREATE INDEX IF NOT EXISTS ix_telemetry_events_org_project
    ON telemetry_events (org_id, project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_request_cost_org_project
    ON request_cost (org_id, project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_token_usage_request
    ON token_usage (request_id);

CREATE INDEX IF NOT EXISTS ix_daily_org_summary_org_date
    ON daily_org_summary (org_id, date DESC);

CREATE INDEX IF NOT EXISTS ix_monthly_org_summary_org_month
    ON monthly_org_summary (org_id, month DESC);
