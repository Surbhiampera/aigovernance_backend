-- =============================================================================
-- Migration: 010_drop_logger_tables.sql
-- Purpose:   Remove all tables that belonged to the old push/pull logger
--            approach and are fully superseded by the proxy server schema.
--
-- BEFORE (39 tables)  →  AFTER (26 tables)
--
-- REMOVED (13 tables):
--   connector_sync_logs   → was: pull-connector sync log
--   tool_connectors       → was: pull-connector config
--   execution_pipeline    → replaced by: route_executions
--   trace_model_usage     → replaced by: token_usage
--   trace_tool_usage      → replaced by: function_executions
--   request_response_logs → replaced by: ai_requests + ai_responses
--   cost_breakdown        → replaced by: request_cost
--   decorator_registrations → replaced by: function_definitions
--   tool_api_inventory    → replaced by: function_definitions
--   project_model_usage   → derived from: request_cost
--   providers             → redundant: data lives in model_pricing
--   tool_registry         → replaced by: function_definitions + model_deployments
--   telemetry_events      → replaced by: ai_requests (core proxy event table)
--
-- KEPT (26 tables):
--   Multi-tenancy   : organizations, projects, users, api_keys
--   Model catalog   : model_registry, model_pricing, model_versions, model_deployments
--   Proxy core      : ai_routes, ai_requests, ai_responses
--   Tracking        : token_usage, request_cost, route_executions
--   Functions       : function_definitions, function_executions
--   Metadata/Audit  : request_metadata, audit_logs
--   Security        : data_security_logs, usage_anomalies
--   Governance      : governance_rules, alerts
--   Budget/Limits   : budgets, rate_limits
--   Summaries       : daily_org_summary, monthly_org_summary
--
-- Safe to re-run: all DROPs use IF EXISTS.
-- Compatible: PostgreSQL 13+
-- =============================================================================

BEGIN;

-- =============================================================================
-- STEP 1: Drop FK columns added to new tables that pointed back to
--         telemetry_events (all these tables are empty — 0 rows).
--         Must happen before telemetry_events can be dropped.
-- =============================================================================

-- ai_requests.event_id → telemetry_events
ALTER TABLE ai_requests
    DROP COLUMN IF EXISTS event_id;

-- route_executions.event_id → telemetry_events
ALTER TABLE route_executions
    DROP COLUMN IF EXISTS event_id;

-- function_executions.event_id → telemetry_events
ALTER TABLE function_executions
    DROP COLUMN IF EXISTS event_id;

-- alerts.telemetry_id → telemetry_events (old logger reference, no longer meaningful)
ALTER TABLE alerts
    DROP COLUMN IF EXISTS telemetry_id;


-- =============================================================================
-- STEP 2: Fix data_security_logs — keep the table but detach it from
--         telemetry_events. Rename event_id to legacy_event_id (historical
--         reference only) and add request_id for future proxy linkage.
-- =============================================================================

-- Drop FK constraint so we can drop telemetry_events
ALTER TABLE data_security_logs
    DROP CONSTRAINT IF EXISTS data_security_logs_event_id_fkey;

-- Mark the old column as legacy so it's clear it's historical
ALTER TABLE data_security_logs
    RENAME COLUMN event_id TO legacy_event_id;

-- Add new column that will link to ai_requests going forward
ALTER TABLE data_security_logs
    ADD COLUMN IF NOT EXISTS request_id VARCHAR(120);

CREATE INDEX IF NOT EXISTS idx_data_security_request_id
    ON data_security_logs (request_id) WHERE request_id IS NOT NULL;


-- =============================================================================
-- STEP 3: Drop telemetry_events FK constraints on telemetry_events itself
--         (back-links we added in migration 009).
-- =============================================================================

ALTER TABLE telemetry_events
    DROP CONSTRAINT IF EXISTS fk_telemetry_route_id;

ALTER TABLE telemetry_events
    DROP CONSTRAINT IF EXISTS fk_telemetry_request_ref_id;


-- =============================================================================
-- STEP 4: Drop all old-logger tables in dependency order
--         (children first, then parents).
-- =============================================================================

-- Children of telemetry_events (logger-only, all superseded)
DROP TABLE IF EXISTS cost_breakdown          CASCADE;
DROP TABLE IF EXISTS execution_pipeline      CASCADE;
DROP TABLE IF EXISTS trace_model_usage       CASCADE;
DROP TABLE IF EXISTS trace_tool_usage        CASCADE;
DROP TABLE IF EXISTS request_response_logs   CASCADE;

-- telemetry_events itself (replaced by ai_requests)
DROP TABLE IF EXISTS telemetry_events        CASCADE;

-- Connector / pull-ingestion tables (entire pull model removed)
DROP TABLE IF EXISTS connector_sync_logs     CASCADE;
DROP TABLE IF EXISTS tool_connectors         CASCADE;

-- Old decorator SDK logger tables (replaced by function_definitions)
DROP TABLE IF EXISTS decorator_registrations CASCADE;
DROP TABLE IF EXISTS tool_api_inventory      CASCADE;

-- Old aggregation table (derive from request_cost going forward)
DROP TABLE IF EXISTS project_model_usage     CASCADE;

-- Trivial lookup table (data lives in model_pricing already)
DROP TABLE IF EXISTS providers               CASCADE;

-- Old tool catalog (replaced by function_definitions + model_deployments)
DROP TABLE IF EXISTS tool_registry           CASCADE;


COMMIT;

-- =============================================================================
-- Final table inventory after this migration (26 tables):
--
-- Multi-tenancy:
--   organizations, projects, users, api_keys
--
-- Model catalog:
--   model_registry, model_pricing, model_versions, model_deployments
--
-- Proxy core (request lifecycle):
--   ai_routes
--   ai_requests          ← replaces telemetry_events
--   ai_responses
--
-- Tracking:
--   token_usage          ← replaces trace_model_usage
--   request_cost         ← replaces cost_breakdown
--   route_executions     ← replaces execution_pipeline
--
-- Functions/Tools:
--   function_definitions ← replaces decorator_registrations + tool_api_inventory
--   function_executions  ← replaces trace_tool_usage
--
-- Metadata & Audit:
--   request_metadata
--   audit_logs
--
-- Security:
--   data_security_logs   (kept, FK migrated to request_id)
--   usage_anomalies
--
-- Governance:
--   governance_rules
--   alerts               (telemetry_id column dropped)
--
-- Budget & Rate limits:
--   budgets
--   rate_limits
--
-- Summaries (dashboard):
--   daily_org_summary
--   monthly_org_summary
-- =============================================================================
