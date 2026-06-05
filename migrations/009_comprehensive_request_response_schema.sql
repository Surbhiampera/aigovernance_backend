-- =============================================================================
-- Migration: 009_comprehensive_request_response_schema.sql
-- Description: Comprehensive API Request/Response schema for AI Governance Platform.
--              Drops orphaned/dead tables, enhances existing tables, and introduces
--              a full request-lifecycle schema covering routes, requests, responses,
--              token usage, cost tracking, function executions, and audit logging.
-- Compatible:  PostgreSQL 13+
-- Safe to re-run: uses IF NOT EXISTS / IF EXISTS / ADD COLUMN IF NOT EXISTS
-- =============================================================================

BEGIN;

-- =============================================================================
-- STEP 1: DROP ORPHANED / DEAD TABLES
-- These tables have no SQLAlchemy model and are not referenced by any active
-- router or service code.  Migration 008 already targeted them; we repeat the
-- DROPs here so this file is self-contained and idempotent.
-- =============================================================================

-- tool-specific extension with no ORM model
DROP TABLE IF EXISTS email_agent_logs;

-- file-upload tracking table, no ORM model, never queried
DROP TABLE IF EXISTS upload_data;

-- rate-limit violation log, no ORM model, no router references
DROP TABLE IF EXISTS rate_limit_violations;

-- user↔project join table, no ORM model, superseded by project_id on users
DROP TABLE IF EXISTS user_projects;


-- =============================================================================
-- STEP 2: ENHANCE EXISTING TABLES
-- Add new columns to existing tables without breaking backward compatibility.
-- =============================================================================

-- 2.1  projects – add human-readable reference ID and soft-delete flag
ALTER TABLE projects
    ADD COLUMN IF NOT EXISTS project_ref_id  VARCHAR(100),  -- business-level slug / external ID
    ADD COLUMN IF NOT EXISTS description     TEXT,
    ADD COLUMN IF NOT EXISTS is_active       BOOLEAN        DEFAULT TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_projects_ref_id
    ON projects (project_ref_id) WHERE project_ref_id IS NOT NULL;


-- 2.2  tool_registry – link to project, add description
ALTER TABLE tool_registry
    ADD COLUMN IF NOT EXISTS project_id       VARCHAR(100),   -- scope tool to a project
    ADD COLUMN IF NOT EXISTS project_ref_id   VARCHAR(100),
    ADD COLUMN IF NOT EXISTS description      TEXT,
    ADD COLUMN IF NOT EXISTS is_active        BOOLEAN         DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS cost_model_type  VARCHAR(50);    -- per_token | per_request | per_second | fixed


-- 2.3  model_registry – versions, capabilities, lifecycle
ALTER TABLE model_registry
    ADD COLUMN IF NOT EXISTS model_version        VARCHAR(50),
    ADD COLUMN IF NOT EXISTS context_window       INTEGER,
    ADD COLUMN IF NOT EXISTS max_output_tokens    INTEGER,
    ADD COLUMN IF NOT EXISTS capabilities         JSONB,    -- {"functions":true,"vision":true,"streaming":true}
    ADD COLUMN IF NOT EXISTS is_active            BOOLEAN   DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS deprecated_at        TIMESTAMP;


-- 2.4  model_pricing – new token-type pricing columns + lifecycle columns
ALTER TABLE model_pricing
    ADD COLUMN IF NOT EXISTS cached_input_cost_per_1k   DECIMAL(12,8) DEFAULT 0,  -- cache-read discount
    ADD COLUMN IF NOT EXISTS reasoning_cost_per_1k      DECIMAL(12,8) DEFAULT 0,  -- o1/o3 thinking tokens
    ADD COLUMN IF NOT EXISTS batch_input_cost_per_1k    DECIMAL(12,8) DEFAULT 0,  -- async-batch discount
    ADD COLUMN IF NOT EXISTS batch_output_cost_per_1k   DECIMAL(12,8) DEFAULT 0,
    ADD COLUMN IF NOT EXISTS is_active                  BOOLEAN       DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS effective_to               TIMESTAMP,                 -- NULL = still current
    ADD COLUMN IF NOT EXISTS pricing_version            VARCHAR(50),
    ADD COLUMN IF NOT EXISTS context_window_tokens      INTEGER,
    ADD COLUMN IF NOT EXISTS max_output_tokens          INTEGER;


-- 2.5  telemetry_events – back-link to new ai_requests and ai_routes
--      (FKs added later, after the referenced tables are created)
ALTER TABLE telemetry_events
    ADD COLUMN IF NOT EXISTS route_id        VARCHAR(120),  -- FK → ai_routes.route_id (added below)
    ADD COLUMN IF NOT EXISTS request_ref_id  VARCHAR(120);  -- FK → ai_requests.request_id (added below)


-- =============================================================================
-- STEP 3: NEW TABLES
-- Ordered by dependency so FK references are always to already-created tables.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 3.1  AI Routes
-- Route / endpoint definitions for the AI Governance proxy layer.
-- Every incoming API call is matched to one route; routing decisions and
-- model-selection logic are stored in route_executions (3.9).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_routes (
    id                          BIGSERIAL       PRIMARY KEY,
    route_id                    VARCHAR(120)    NOT NULL UNIQUE,

    -- Mandatory project linkage
    org_id                      VARCHAR(100)    NOT NULL REFERENCES organizations(id),
    project_id                  VARCHAR(100)    REFERENCES projects(id),
    project_ref_id              VARCHAR(100),   -- denormalized from projects.project_ref_id

    -- Route identity
    route_name                  VARCHAR(255)    NOT NULL,
    route_path                  VARCHAR(500),
    route_type                  VARCHAR(50),
    -- route_type values: llm_proxy | tool_proxy | function_route | webhook | passthrough
    http_method                 VARCHAR(10)     DEFAULT 'POST',
    upstream_url                VARCHAR(500),

    -- Default model / provider (can be overridden per request)
    default_provider            VARCHAR(100),
    default_model               VARCHAR(120),
    allowed_models              JSONB,          -- ["gpt-4o", "claude-3-5-sonnet-20241022"]
    model_selection_strategy    VARCHAR(50),
    -- strategy values: fixed | cost_optimized | load_balanced | failover | rule_based

    -- Metadata
    description                 TEXT,
    tags                        JSONB,
    is_active                   BOOLEAN         DEFAULT TRUE,
    requires_auth               BOOLEAN         DEFAULT TRUE,

    created_at                  TIMESTAMP       DEFAULT NOW(),
    updated_at                  TIMESTAMP       DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_routes_org_project ON ai_routes (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_ai_routes_route_id    ON ai_routes (route_id);


-- ---------------------------------------------------------------------------
-- 3.2  Model Versions
-- Tracks specific model versions / snapshots beyond the base model_registry row.
-- Allows price-correct attribution when a model is later deprecated or updated.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_versions (
    id                  BIGSERIAL       PRIMARY KEY,
    model_registry_id   BIGINT          REFERENCES model_registry(id) ON DELETE SET NULL,

    provider            VARCHAR(100)    NOT NULL,
    model_name          VARCHAR(120)    NOT NULL,
    version_tag         VARCHAR(50),    -- "2024-11-20", "preview", "turbo-2024-04-09"
    full_model_id       VARCHAR(200),   -- provider's canonical model string

    -- Capabilities
    context_window          INTEGER,
    max_output_tokens       INTEGER,
    supports_functions      BOOLEAN     DEFAULT FALSE,
    supports_vision         BOOLEAN     DEFAULT FALSE,
    supports_streaming      BOOLEAN     DEFAULT FALSE,
    supports_reasoning      BOOLEAN     DEFAULT FALSE,
    supports_batch          BOOLEAN     DEFAULT FALSE,

    -- Lifecycle
    released_at             TIMESTAMP,
    deprecated_at           TIMESTAMP,
    is_active               BOOLEAN     DEFAULT TRUE,
    created_at              TIMESTAMP   DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_model_versions_unique
    ON model_versions (provider, model_name, COALESCE(version_tag, ''));
CREATE INDEX IF NOT EXISTS idx_model_versions_lookup ON model_versions (provider, model_name);


-- ---------------------------------------------------------------------------
-- 3.3  Model Deployments
-- Per-org / per-project model deployment and endpoint configuration.
-- Supports Azure OpenAI, AWS Bedrock, GCP Vertex, and self-hosted deployments
-- alongside direct provider API endpoints.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS model_deployments (
    id                  BIGSERIAL       PRIMARY KEY,
    deployment_id       VARCHAR(120)    NOT NULL UNIQUE,

    -- Mandatory project linkage
    org_id              VARCHAR(100)    NOT NULL REFERENCES organizations(id),
    project_id          VARCHAR(100)    REFERENCES projects(id),
    project_ref_id      VARCHAR(100),

    -- Model reference
    provider            VARCHAR(100)    NOT NULL,
    model_name          VARCHAR(120)    NOT NULL,
    model_version_id    BIGINT          REFERENCES model_versions(id) ON DELETE SET NULL,

    -- Deployment config
    deployment_name     VARCHAR(255),
    endpoint_url        VARCHAR(500),
    deployment_type     VARCHAR(50)     DEFAULT 'api',
    -- deployment_type: api | azure_openai | bedrock | vertex | local | custom

    auth_type           VARCHAR(50),    -- api_key | bearer | iam | oauth2
    api_key_ref         VARCHAR(500),   -- env-var name or secret-manager reference (never the key itself)

    -- Parameter defaults (overridden per request)
    default_parameters  JSONB,          -- {"temperature":0.7,"max_tokens":4096,"top_p":1.0}

    -- Rate limits for this deployment
    rate_limit_rpm      INTEGER,        -- requests per minute
    rate_limit_tpm      INTEGER,        -- tokens per minute

    is_active           BOOLEAN         DEFAULT TRUE,
    created_at          TIMESTAMP       DEFAULT NOW(),
    updated_at          TIMESTAMP       DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_model_deployments_org_project ON model_deployments (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_model_deployments_provider    ON model_deployments (provider, model_name);


-- ---------------------------------------------------------------------------
-- 3.4  Function Definitions
-- Central registry of all function / tool definitions per project.
-- Replaces the scattered decorator_registrations + tool_api_inventory approach
-- with a richer schema that stores JSON Schema for inputs/outputs and full
-- runtime metadata.  The older tables are NOT dropped because they are still
-- populated by the decorator SDK; they are superseded for new integrations.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS function_definitions (
    id                  BIGSERIAL       PRIMARY KEY,
    function_def_id     VARCHAR(120)    NOT NULL UNIQUE,

    -- Mandatory project linkage
    org_id              VARCHAR(100)    NOT NULL REFERENCES organizations(id),
    project_id          VARCHAR(100)    REFERENCES projects(id),
    project_ref_id      VARCHAR(100),

    -- Identity
    tool_name           VARCHAR(150)    NOT NULL,
    function_name       VARCHAR(255)    NOT NULL,
    function_type       VARCHAR(50),
    -- function_type: llm_tool | api_call | webhook | code_exec | retrieval | custom

    module_path         VARCHAR(500),
    function_signature  TEXT,           -- Python / TS signature string
    description         TEXT,

    -- JSON Schema definitions
    input_schema        JSONB,          -- OpenAPI / JSON Schema for parameters
    output_schema       JSONB,          -- JSON Schema for return value
    examples            JSONB,          -- [{input:{...}, output:{...}}]

    -- Runtime
    runtime_language    VARCHAR(50),    -- python | javascript | typescript | go
    sdk_version         VARCHAR(20),
    decorator_type      VARCHAR(50),
    execution_env       VARCHAR(50)     DEFAULT 'production',
    version             VARCHAR(50),

    -- Lifecycle
    first_seen          TIMESTAMP       DEFAULT NOW(),
    last_seen           TIMESTAMP       DEFAULT NOW(),
    is_active           BOOLEAN         DEFAULT TRUE,
    call_count          BIGINT          DEFAULT 0,
    created_at          TIMESTAMP       DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_func_def_unique
    ON function_definitions (org_id, COALESCE(project_id, '__none__'), tool_name, function_name);
CREATE INDEX IF NOT EXISTS idx_func_def_org_project ON function_definitions (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_func_def_tool        ON function_definitions (tool_name, function_name);


-- ---------------------------------------------------------------------------
-- 3.5  AI Requests
-- Central table for the complete incoming request lifecycle.
-- Every API call routed through the platform gets one row here.
-- Linked to: organizations, projects, telemetry_events, ai_routes,
--            model_deployments.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_requests (
    id                      BIGSERIAL       PRIMARY KEY,
    request_id              VARCHAR(120)    NOT NULL UNIQUE,

    -- Mandatory project linkage
    org_id                  VARCHAR(100)    NOT NULL REFERENCES organizations(id),
    project_id              VARCHAR(100)    REFERENCES projects(id),
    project_ref_id          VARCHAR(100),   -- denormalized from projects.project_ref_id

    -- Correlation / distributed tracing
    event_id                VARCHAR(120)    REFERENCES telemetry_events(event_id) ON DELETE SET NULL,
    trace_id                VARCHAR(120),
    span_id                 VARCHAR(120),
    parent_span_id          VARCHAR(120),
    session_id              VARCHAR(120),
    conversation_id         VARCHAR(120),

    -- Route and deployment reference
    route_id                VARCHAR(120)    REFERENCES ai_routes(route_id) ON DELETE SET NULL,
    deployment_id           VARCHAR(120)    REFERENCES model_deployments(deployment_id) ON DELETE SET NULL,

    -- Request classification
    request_type            VARCHAR(50)     NOT NULL DEFAULT 'chat_completion',
    -- request_type: chat_completion | text_completion | embedding | image_generation |
    --               tool_call | function_call | audio | vision | rerank | custom
    request_status          VARCHAR(30)     NOT NULL DEFAULT 'pending',
    -- request_status: pending | processing | completed | failed | cancelled | timeout | blocked

    -- User / identity context
    user_id                 VARCHAR(100),
    user_email              VARCHAR(150),
    user_role               VARCHAR(50),
    api_key_id              VARCHAR(120),
    client_ip               VARCHAR(50),
    user_agent              VARCHAR(500),

    -- Model information at request time
    provider                VARCHAR(100),
    model_name              VARCHAR(120),
    model_version           VARCHAR(50),
    requested_model         VARCHAR(120),   -- what the caller asked for
    routed_model            VARCHAR(120),   -- what the platform actually used

    -- Function / tool context (from GovernanceDecorator)
    function_name           VARCHAR(255),
    tool_name               VARCHAR(150),
    module_path             VARCHAR(500),
    decorator_type          VARCHAR(50),
    sdk_version             VARCHAR(20),
    execution_env           VARCHAR(50),

    -- ── Complete input request capture ──────────────────────────────────
    request_payload         JSONB,          -- full sanitised request body
    prompt_text             TEXT,           -- extracted user / human message
    system_prompt           TEXT,           -- system message
    messages                JSONB,          -- full messages array (chat models)
    request_parameters      JSONB,          -- temperature, max_tokens, top_p, stop, etc.
    request_headers         JSONB,          -- sanitised HTTP headers
    request_metadata        JSONB,          -- arbitrary caller-supplied metadata

    -- Input characteristics
    input_token_estimate    INTEGER         DEFAULT 0,
    prompt_char_count       INTEGER         DEFAULT 0,
    num_messages            INTEGER         DEFAULT 0,
    has_system_prompt       BOOLEAN         DEFAULT FALSE,
    has_tool_definitions    BOOLEAN         DEFAULT FALSE,   -- tools/functions sent in request
    has_images              BOOLEAN         DEFAULT FALSE,
    has_documents           BOOLEAN         DEFAULT FALSE,
    has_audio               BOOLEAN         DEFAULT FALSE,
    num_tool_definitions    INTEGER         DEFAULT 0,       -- count of tools/functions defined

    -- User / session context
    user_context            JSONB,          -- caller-supplied user context object
    session_context         JSONB,          -- session-level state snapshot

    -- Security at ingestion
    pii_detected            BOOLEAN         DEFAULT FALSE,
    pii_types               JSONB,          -- ["EMAIL","PHONE","SSN"]
    pii_masked              BOOLEAN         DEFAULT FALSE,
    content_policy_flags    JSONB,

    -- Timing
    received_at             TIMESTAMP       NOT NULL DEFAULT NOW(),
    processing_started_at   TIMESTAMP,
    created_at              TIMESTAMP       DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_req_org_project    ON ai_requests (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_ai_req_request_id     ON ai_requests (request_id);
CREATE INDEX IF NOT EXISTS idx_ai_req_trace_id       ON ai_requests (trace_id);
CREATE INDEX IF NOT EXISTS idx_ai_req_event_id       ON ai_requests (event_id);
CREATE INDEX IF NOT EXISTS idx_ai_req_user_id        ON ai_requests (user_id);
CREATE INDEX IF NOT EXISTS idx_ai_req_model          ON ai_requests (provider, model_name);
CREATE INDEX IF NOT EXISTS idx_ai_req_status         ON ai_requests (request_status);
CREATE INDEX IF NOT EXISTS idx_ai_req_received_at    ON ai_requests (received_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_req_session        ON ai_requests (session_id)       WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_ai_req_function       ON ai_requests (function_name, tool_name);
CREATE INDEX IF NOT EXISTS idx_ai_req_route          ON ai_requests (route_id)         WHERE route_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 3.6  AI Responses
-- Complete response data for every ai_requests row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ai_responses (
    id                          BIGSERIAL       PRIMARY KEY,
    response_id                 VARCHAR(120)    NOT NULL UNIQUE,

    -- Mandatory request linkage
    request_id                  VARCHAR(120)    NOT NULL REFERENCES ai_requests(request_id) ON DELETE CASCADE,

    -- Denormalized project linkage for direct querying
    org_id                      VARCHAR(100)    NOT NULL,
    project_id                  VARCHAR(100),
    project_ref_id              VARCHAR(100),

    -- Model actually used
    provider                    VARCHAR(100),
    model_name                  VARCHAR(120),
    model_version               VARCHAR(50),
    deployment_id               VARCHAR(120)    REFERENCES model_deployments(deployment_id) ON DELETE SET NULL,

    -- Response status
    response_status             VARCHAR(30)     NOT NULL DEFAULT 'pending',
    -- response_status: pending | streaming | completed | error | cancelled | timeout
    finish_reason               VARCHAR(50),
    -- finish_reason: stop | length | tool_calls | content_filter | error | timeout | null
    is_streaming                BOOLEAN         DEFAULT FALSE,
    is_cached                   BOOLEAN         DEFAULT FALSE,

    -- ── Complete output response capture ────────────────────────────────
    response_payload            JSONB,          -- full raw response body
    response_text               TEXT,           -- extracted assistant / completion text
    tool_calls                  JSONB,          -- [{id, type, function:{name, arguments}}]
    tool_call_results           JSONB,          -- [{tool_call_id, role:"tool", content:...}]
    function_call               JSONB,          -- legacy function_call field
    response_metadata           JSONB,          -- provider-specific metadata in response
    response_headers            JSONB,          -- response HTTP headers

    -- Output characteristics
    output_char_count           INTEGER         DEFAULT 0,
    num_tool_calls              INTEGER         DEFAULT 0,
    has_citations               BOOLEAN         DEFAULT FALSE,
    response_languages          JSONB,

    -- Error details
    error_code                  VARCHAR(50),
    error_type                  VARCHAR(100),
    error_message               TEXT,
    error_details               JSONB,
    is_retried                  BOOLEAN         DEFAULT FALSE,
    retry_count                 INTEGER         DEFAULT 0,

    -- Provider-native correlation IDs
    provider_request_id         VARCHAR(255),
    provider_response_id        VARCHAR(255),

    -- Security on output
    output_pii_detected         BOOLEAN         DEFAULT FALSE,
    output_pii_types            JSONB,
    output_content_policy_flags JSONB,

    -- Timing
    response_started_at         TIMESTAMP,
    response_completed_at       TIMESTAMP,
    first_token_at              TIMESTAMP,      -- when first streamed chunk arrived
    latency_ms                  INTEGER         DEFAULT 0,
    ttft_ms                     INTEGER         DEFAULT 0,  -- time-to-first-token (ms)

    created_at                  TIMESTAMP       DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ai_resp_request_id     ON ai_responses (request_id);
CREATE INDEX IF NOT EXISTS idx_ai_resp_org_project    ON ai_responses (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_ai_resp_status         ON ai_responses (response_status);
CREATE INDEX IF NOT EXISTS idx_ai_resp_finish_reason  ON ai_responses (finish_reason);
CREATE INDEX IF NOT EXISTS idx_ai_resp_model          ON ai_responses (provider, model_name);
CREATE INDEX IF NOT EXISTS idx_ai_resp_completed_at   ON ai_responses (response_completed_at DESC);


-- ---------------------------------------------------------------------------
-- 3.7  Token Usage
-- Dedicated, granular token tracking per request/response.
-- Covers all current OpenAI / Anthropic token categories (prompt, completion,
-- cached, reasoning, audio, batch, predicted output) and is extensible for
-- any future provider-specific token types via raw_usage JSONB.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS token_usage (
    id                              BIGSERIAL       PRIMARY KEY,
    token_usage_id                  VARCHAR(120)    NOT NULL UNIQUE,

    -- Request/Response linkage
    request_id                      VARCHAR(120)    NOT NULL REFERENCES ai_requests(request_id) ON DELETE CASCADE,
    response_id                     VARCHAR(120)    REFERENCES ai_responses(response_id) ON DELETE SET NULL,

    -- Denormalized project linkage
    org_id                          VARCHAR(100)    NOT NULL,
    project_id                      VARCHAR(100),
    project_ref_id                  VARCHAR(100),

    -- Model context
    provider                        VARCHAR(100),
    model_name                      VARCHAR(120),
    model_version                   VARCHAR(50),

    -- ── Core token counts ───────────────────────────────────────────────
    prompt_tokens                   INTEGER         DEFAULT 0,  -- input tokens (alias for input_tokens)
    completion_tokens               INTEGER         DEFAULT 0,  -- output tokens (alias for output_tokens)
    total_tokens                    INTEGER         DEFAULT 0,  -- prompt + completion

    -- ── Input token breakdown ────────────────────────────────────────────
    input_tokens                    INTEGER         DEFAULT 0,  -- total input tokens
    cached_tokens                   INTEGER         DEFAULT 0,  -- input tokens served from cache
    uncached_tokens                 INTEGER         DEFAULT 0,  -- input_tokens - cached_tokens
    audio_input_tokens              INTEGER         DEFAULT 0,  -- audio-modality input tokens
    image_input_tokens              INTEGER         DEFAULT 0,  -- image-modality input tokens
    document_input_tokens           INTEGER         DEFAULT 0,  -- document / file input tokens

    -- ── Output token breakdown ───────────────────────────────────────────
    output_tokens                   INTEGER         DEFAULT 0,  -- total output tokens
    reasoning_tokens                INTEGER         DEFAULT 0,  -- o1/o3 thinking / reasoning tokens
    audio_output_tokens             INTEGER         DEFAULT 0,  -- audio-modality output tokens
    accepted_prediction_tokens      INTEGER         DEFAULT 0,  -- for predicted-output feature
    rejected_prediction_tokens      INTEGER         DEFAULT 0,

    -- ── Tool / function token overhead ──────────────────────────────────
    tool_definition_tokens          INTEGER         DEFAULT 0,  -- tokens used by tool definitions
    tool_result_tokens              INTEGER         DEFAULT 0,  -- tokens in tool call results
    system_tokens                   INTEGER         DEFAULT 0,  -- tokens in system prompt

    -- ── Batch API specifics ──────────────────────────────────────────────
    batch_input_tokens              INTEGER         DEFAULT 0,
    batch_output_tokens             INTEGER         DEFAULT 0,

    -- ── Context window utilisation ───────────────────────────────────────
    context_window_limit            INTEGER,        -- model max context (from model_versions)
    context_utilization_pct         DECIMAL(6,3),   -- total_tokens / context_window_limit * 100

    -- Raw provider usage object (as-returned, for any future fields)
    raw_usage                       JSONB,

    created_at                      TIMESTAMP       DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_token_usage_request  ON token_usage (request_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_org_project     ON token_usage (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_token_usage_model           ON token_usage (provider, model_name);


-- ---------------------------------------------------------------------------
-- 3.8  Request Cost
-- Detailed cost breakdown per request — every cost dimension tracked, with
-- a pricing snapshot stored at calculation time for immutable auditability.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS request_cost (
    id                      BIGSERIAL       PRIMARY KEY,
    cost_id                 VARCHAR(120)    NOT NULL UNIQUE,

    -- Linkage
    request_id              VARCHAR(120)    NOT NULL REFERENCES ai_requests(request_id) ON DELETE CASCADE,
    response_id             VARCHAR(120)    REFERENCES ai_responses(response_id) ON DELETE SET NULL,
    token_usage_id          VARCHAR(120)    REFERENCES token_usage(token_usage_id) ON DELETE SET NULL,
    pricing_ref_id          BIGINT          REFERENCES model_pricing(id) ON DELETE SET NULL,

    -- Denormalized project linkage
    org_id                  VARCHAR(100)    NOT NULL,
    project_id              VARCHAR(100),
    project_ref_id          VARCHAR(100),

    -- Model context
    provider                VARCHAR(100),
    model_name              VARCHAR(120),
    model_version           VARCHAR(50),

    -- ── Input cost breakdown ─────────────────────────────────────────────
    input_token_cost        DECIMAL(14,8)   DEFAULT 0,  -- standard input token cost
    cached_token_cost       DECIMAL(14,8)   DEFAULT 0,  -- discounted cache-read cost
    uncached_token_cost     DECIMAL(14,8)   DEFAULT 0,  -- full-rate input cost
    audio_input_cost        DECIMAL(14,8)   DEFAULT 0,
    image_input_cost        DECIMAL(14,8)   DEFAULT 0,

    -- ── Output cost breakdown ────────────────────────────────────────────
    output_token_cost       DECIMAL(14,8)   DEFAULT 0,  -- standard output token cost
    reasoning_token_cost    DECIMAL(14,8)   DEFAULT 0,  -- o1/o3 reasoning token cost
    audio_output_cost       DECIMAL(14,8)   DEFAULT 0,

    -- ── Tool / function costs ────────────────────────────────────────────
    tool_cost               DECIMAL(14,8)   DEFAULT 0,  -- cost from tool/function calls
    function_execution_cost DECIMAL(14,8)   DEFAULT 0,  -- external function execution cost

    -- ── Infrastructure costs ─────────────────────────────────────────────
    infra_cost              DECIMAL(14,8)   DEFAULT 0,  -- latency-based compute cost
    gateway_cost            DECIMAL(14,8)   DEFAULT 0,  -- governance platform overhead

    -- ── Subtotals & total ────────────────────────────────────────────────
    llm_cost                DECIMAL(14,8)   DEFAULT 0,  -- pure LLM provider cost
    total_cost              DECIMAL(14,8)   DEFAULT 0,  -- all-in total before discounts

    -- ── Currency & pricing metadata ──────────────────────────────────────
    currency                VARCHAR(10)     DEFAULT 'USD',
    pricing_version         VARCHAR(50),
    pricing_snapshot        JSONB,
    -- pricing_snapshot stores rates at calculation time, e.g.:
    -- {"input_per_1k":0.003,"output_per_1k":0.015,"cached_per_1k":0.00075}
    cost_model_type         VARCHAR(50),
    -- cost_model_type: per_token | per_request | per_second | fixed | custom | pre-computed

    -- ── Discount / adjustments ───────────────────────────────────────────
    discount_pct            DECIMAL(6,3)    DEFAULT 0,
    discount_reason         VARCHAR(255),
    adjusted_total_cost     DECIMAL(14,8)   DEFAULT 0,  -- total_cost after discounts

    created_at              TIMESTAMP       DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_request_cost_request    ON request_cost (request_id);
CREATE INDEX IF NOT EXISTS idx_request_cost_org_project       ON request_cost (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_request_cost_model             ON request_cost (provider, model_name);
CREATE INDEX IF NOT EXISTS idx_request_cost_created_at        ON request_cost (created_at DESC);


-- ---------------------------------------------------------------------------
-- 3.9  Route Executions
-- Captures the proxy / routing layer execution for each request.
-- Tracks routing decisions, model selection logic, retries, failure reasons,
-- execution stages, and policy enforcement.
-- NOTE: execution_pipeline is kept (still written by telemetry router).
--       New integrations should write here instead.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS route_executions (
    id                      BIGSERIAL       PRIMARY KEY,
    execution_id            VARCHAR(120)    NOT NULL UNIQUE,

    -- Linkage
    request_id              VARCHAR(120)    NOT NULL REFERENCES ai_requests(request_id) ON DELETE CASCADE,
    route_id                VARCHAR(120)    REFERENCES ai_routes(route_id) ON DELETE SET NULL,
    event_id                VARCHAR(120)    REFERENCES telemetry_events(event_id) ON DELETE SET NULL,

    -- Denormalized project linkage
    org_id                  VARCHAR(100)    NOT NULL,
    project_id              VARCHAR(100),
    project_ref_id          VARCHAR(100),

    -- Execution state
    execution_status        VARCHAR(30)     DEFAULT 'pending',
    -- execution_status: pending | routing | proxying | completed | failed | timeout | blocked

    -- ── Routing decision detail ──────────────────────────────────────────
    routing_strategy        VARCHAR(50),
    routing_reason          TEXT,           -- human-readable explanation of routing decision
    original_model          VARCHAR(120),   -- model the caller requested
    selected_model          VARCHAR(120),   -- model used after routing
    selected_provider       VARCHAR(100),
    selected_deployment_id  VARCHAR(120),
    model_override_applied  BOOLEAN         DEFAULT FALSE,
    fallback_triggered      BOOLEAN         DEFAULT FALSE,
    fallback_reason         TEXT,

    -- ── Proxy processing ────────────────────────────────────────────────
    proxy_type              VARCHAR(50),
    -- proxy_type: passthrough | transform | aggregate | stream_proxy
    upstream_url            VARCHAR(500),
    upstream_request_id     VARCHAR(255),   -- ID assigned by upstream AI provider

    -- Execution stages stored as a JSON array for flexible pipeline tracking
    pipeline_stages         JSONB,
    -- [{"stage_name":"pii_check","status":"passed","latency_ms":12,"details":{}}]

    -- ── Retry details ────────────────────────────────────────────────────
    attempt_number          INTEGER         DEFAULT 1,
    max_attempts            INTEGER         DEFAULT 3,
    retry_count             INTEGER         DEFAULT 0,
    retry_reasons           JSONB,          -- [{"attempt":2,"reason":"rate_limit","ts":"..."}]
    last_failure_reason     TEXT,
    failure_code            VARCHAR(50),

    -- ── Timing breakdown (ms) ────────────────────────────────────────────
    total_latency_ms        INTEGER         DEFAULT 0,
    routing_latency_ms      INTEGER         DEFAULT 0,
    proxy_latency_ms        INTEGER         DEFAULT 0,
    upstream_latency_ms     INTEGER         DEFAULT 0,
    governance_check_ms     INTEGER         DEFAULT 0,

    -- ── Quota / policy enforcement ───────────────────────────────────────
    quota_checked           BOOLEAN         DEFAULT FALSE,
    quota_remaining         INTEGER,
    policy_applied          JSONB,          -- governance policies that were evaluated
    blocked_by_policy       BOOLEAN         DEFAULT FALSE,
    block_reason            TEXT,

    started_at              TIMESTAMP,
    completed_at            TIMESTAMP,
    created_at              TIMESTAMP       DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_route_exec_request_id   ON route_executions (request_id);
CREATE INDEX IF NOT EXISTS idx_route_exec_org_project  ON route_executions (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_route_exec_route_id     ON route_executions (route_id)  WHERE route_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_route_exec_status       ON route_executions (execution_status);


-- ---------------------------------------------------------------------------
-- 3.10  Function Executions
-- Per-request function / tool invocation tracking.
-- One row per tool-call turn within a request (a single request may invoke
-- multiple tools; each call gets its own row, linked by call_sequence).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS function_executions (
    id                      BIGSERIAL       PRIMARY KEY,
    execution_id            VARCHAR(120)    NOT NULL UNIQUE,

    -- Linkage
    request_id              VARCHAR(120)    NOT NULL REFERENCES ai_requests(request_id) ON DELETE CASCADE,
    response_id             VARCHAR(120)    REFERENCES ai_responses(response_id) ON DELETE SET NULL,
    function_def_id         VARCHAR(120)    REFERENCES function_definitions(function_def_id) ON DELETE SET NULL,
    event_id                VARCHAR(120)    REFERENCES telemetry_events(event_id) ON DELETE SET NULL,

    -- Denormalized project linkage
    org_id                  VARCHAR(100)    NOT NULL,
    project_id              VARCHAR(100),
    project_ref_id          VARCHAR(100),

    -- Function / tool identity
    tool_name               VARCHAR(150)    NOT NULL,
    function_name           VARCHAR(255)    NOT NULL,
    function_type           VARCHAR(50),
    -- function_type: llm_tool | api_call | webhook | code_exec | retrieval | custom
    call_id                 VARCHAR(120),   -- provider-assigned tool_call_id
    call_sequence           INTEGER         DEFAULT 1,  -- position in multi-call sequence

    -- Execution
    execution_status        VARCHAR(30)     DEFAULT 'pending',
    -- execution_status: pending | running | completed | error | skipped | timeout
    invocation_count        INTEGER         DEFAULT 1,

    -- ── Input / Output capture ───────────────────────────────────────────
    function_arguments      JSONB,          -- full input arguments as passed by the model
    function_result         JSONB,          -- full output / return value
    function_result_text    TEXT,           -- text preview for fast querying
    error_message           TEXT,
    error_details           JSONB,

    -- Tokens consumed by this specific tool call
    tool_input_tokens       INTEGER         DEFAULT 0,
    tool_output_tokens      INTEGER         DEFAULT 0,
    tool_total_tokens       INTEGER         DEFAULT 0,

    -- Timing
    started_at              TIMESTAMP,
    completed_at            TIMESTAMP,
    execution_time_ms       INTEGER         DEFAULT 0,

    -- Cost attributed to this function execution
    attributed_cost         DECIMAL(14,8)   DEFAULT 0,

    created_at              TIMESTAMP       DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_func_exec_request_id    ON function_executions (request_id);
CREATE INDEX IF NOT EXISTS idx_func_exec_org_project   ON function_executions (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_func_exec_tool_fn       ON function_executions (tool_name, function_name);
CREATE INDEX IF NOT EXISTS idx_func_exec_status        ON function_executions (execution_status);
CREATE INDEX IF NOT EXISTS idx_func_exec_call_id       ON function_executions (call_id) WHERE call_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- 3.11  Request Metadata
-- Structured key-value store for additional request attributes that do not fit
-- the fixed columns of ai_requests.  Enables ad-hoc querying on arbitrary
-- dimensions (A/B test variants, feature flags, business metadata) without
-- requiring schema changes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS request_metadata (
    id                  BIGSERIAL       PRIMARY KEY,

    -- Linkage
    request_id          VARCHAR(120)    NOT NULL REFERENCES ai_requests(request_id) ON DELETE CASCADE,

    -- Denormalized project linkage
    org_id              VARCHAR(100)    NOT NULL,
    project_id          VARCHAR(100),
    project_ref_id      VARCHAR(100),

    -- Metadata entry
    meta_category       VARCHAR(100)    NOT NULL,
    -- meta_category: user_context | ab_test | feature_flag | business | custom | provider
    meta_key            VARCHAR(255)    NOT NULL,
    meta_value_text     TEXT,           -- use for string values
    meta_value_json     JSONB,          -- use for structured / nested values
    meta_value_num      DECIMAL(20,6),  -- use for numeric values (for range queries)

    created_at          TIMESTAMP       DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_req_meta_request_id   ON request_metadata (request_id);
CREATE INDEX IF NOT EXISTS idx_req_meta_org_project  ON request_metadata (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_req_meta_key          ON request_metadata (meta_category, meta_key);


-- ---------------------------------------------------------------------------
-- 3.12  Audit Logs
-- Immutable, append-only audit trail for all request/response/routing/cost/
-- configuration changes.  Supports compliance (SOC2, GDPR, HIPAA), forensic
-- investigation, and governance reporting.
-- Rows are never updated — only inserted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id                  BIGSERIAL       PRIMARY KEY,
    audit_id            VARCHAR(120)    NOT NULL UNIQUE,

    -- Denormalized project linkage (mandatory)
    org_id              VARCHAR(100)    NOT NULL,
    project_id          VARCHAR(100),
    project_ref_id      VARCHAR(100),

    -- Actor
    actor_type          VARCHAR(50)     NOT NULL,
    -- actor_type: user | api_key | system | scheduler | sdk | governance_engine
    actor_id            VARCHAR(100),
    actor_email         VARCHAR(150),
    actor_ip            VARCHAR(50),

    -- Event classification
    audit_category      VARCHAR(50)     NOT NULL,
    -- audit_category: request | response | routing | token_usage | cost |
    --                 config | governance | security | model | function | budget
    audit_action        VARCHAR(100)    NOT NULL,
    -- audit_action: created | completed | failed | routed | blocked | retried |
    --               updated | deleted | policy_triggered | pii_detected | anomaly_detected
    audit_status        VARCHAR(30)     DEFAULT 'success',
    -- audit_status: success | failure | warning | info

    -- Target entity
    entity_type         VARCHAR(100),   -- 'ai_request' | 'ai_response' | 'route' | etc.
    entity_id           VARCHAR(120),

    -- Correlation IDs
    request_id          VARCHAR(120),
    trace_id            VARCHAR(120),
    event_id            VARCHAR(120),

    -- Change payload (for config changes / updates)
    old_value           JSONB,          -- state before the change
    new_value           JSONB,          -- state after the change
    change_summary      TEXT,           -- human-readable diff summary

    -- Governance flags
    policy_triggered    BOOLEAN         DEFAULT FALSE,
    compliance_relevant BOOLEAN         DEFAULT FALSE,
    requires_review     BOOLEAN         DEFAULT FALSE,

    -- Supplementary context
    metadata            JSONB,

    occurred_at         TIMESTAMP       NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMP       DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_org_project     ON audit_logs (org_id, project_id);
CREATE INDEX IF NOT EXISTS idx_audit_category_action ON audit_logs (audit_category, audit_action);
CREATE INDEX IF NOT EXISTS idx_audit_request_id      ON audit_logs (request_id)  WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_entity          ON audit_logs (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_actor           ON audit_logs (actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_occurred_at     ON audit_logs (occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_compliance      ON audit_logs (org_id, compliance_relevant)
    WHERE compliance_relevant = TRUE;


-- =============================================================================
-- STEP 4: ADD DEFERRED FK CONSTRAINTS
-- Now that ai_requests / ai_routes exist, back-fill the FK columns we added to
-- telemetry_events in Step 2.
-- =============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_telemetry_route_id'
          AND table_name = 'telemetry_events'
    ) THEN
        ALTER TABLE telemetry_events
            ADD CONSTRAINT fk_telemetry_route_id
                FOREIGN KEY (route_id) REFERENCES ai_routes(route_id) ON DELETE SET NULL;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'fk_telemetry_request_ref_id'
          AND table_name = 'telemetry_events'
    ) THEN
        ALTER TABLE telemetry_events
            ADD CONSTRAINT fk_telemetry_request_ref_id
                FOREIGN KEY (request_ref_id) REFERENCES ai_requests(request_id) ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_telemetry_route_id       ON telemetry_events (route_id)       WHERE route_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_telemetry_request_ref_id ON telemetry_events (request_ref_id) WHERE request_ref_id IS NOT NULL;


COMMIT;

-- =============================================================================
-- Post-migration checklist (manual steps — do NOT include in automated run):
--
-- 1. decorator/models.py → RequestResponseLog  — still valid; table NOT dropped.
--    New code should write to ai_requests + ai_responses instead.
--
-- 2. app/models.py → ExecutionPipeline — still valid; table NOT dropped.
--    New code should write to route_executions instead.
--
-- 3. Populate projects.project_ref_id for existing rows if you have a slug /
--    external ID convention:
--      UPDATE projects SET project_ref_id = id WHERE project_ref_id IS NULL;
--
-- 4. Backfill tool_registry.project_id if tools are already scoped by project
--    in your usage.
-- =============================================================================
