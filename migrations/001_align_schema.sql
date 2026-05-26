-- Migration: align PostgreSQL tables with ORM models
-- Run this against your database to fix schema-vs-ORM mismatches.
-- The ORM (models.py) is the source of truth; the DB tables were created
-- from an older/simpler schema and are missing columns the app needs.

-- ============================================================
-- 1. usage_anomalies  (causes 500 on /summary/overview)
-- ============================================================
-- DB has: service, z_score, telemetry_id
-- ORM expects: tool_name, anomaly_score, event_id, message, status

ALTER TABLE usage_anomalies ADD COLUMN IF NOT EXISTS tool_name VARCHAR(150);
ALTER TABLE usage_anomalies ADD COLUMN IF NOT EXISTS event_id VARCHAR(120);
ALTER TABLE usage_anomalies ADD COLUMN IF NOT EXISTS anomaly_score DECIMAL(8,2) DEFAULT 0;
ALTER TABLE usage_anomalies ADD COLUMN IF NOT EXISTS message TEXT;
ALTER TABLE usage_anomalies ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'open';

-- Migrate existing data from old columns to new columns
UPDATE usage_anomalies SET tool_name = service WHERE tool_name IS NULL AND service IS NOT NULL;
UPDATE usage_anomalies SET anomaly_score = z_score WHERE anomaly_score IS NULL AND z_score IS NOT NULL;

-- ============================================================
-- 2. telemetry_events  (ORM has many extra columns)
-- ============================================================
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS component_name VARCHAR(150);
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS execution_type VARCHAR(50);
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS input_data_size_mb DECIMAL(12,4) DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS output_data_size_mb DECIMAL(12,4) DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS prompt_tokens INT DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS completion_tokens INT DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS total_tokens INT DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS llm_cost DECIMAL(14,6) DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS infra_cost DECIMAL(14,6) DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS external_cost DECIMAL(14,6) DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS total_cost DECIMAL(14,6) DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS risk_score DECIMAL(8,2) DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS anomaly_score DECIMAL(8,2) DEFAULT 0;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS misuse_detected BOOLEAN DEFAULT FALSE;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS abnormal_usage_spike BOOLEAN DEFAULT FALSE;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS started_at TIMESTAMP;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS tags JSON;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS metadata_json JSON;
ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS raw_usage_json JSON;

-- Migrate old columns if they exist
UPDATE telemetry_events SET prompt_tokens = input_tokens WHERE prompt_tokens = 0 AND input_tokens IS NOT NULL AND input_tokens > 0;
UPDATE telemetry_events SET completion_tokens = output_tokens WHERE completion_tokens = 0 AND output_tokens IS NOT NULL AND output_tokens > 0;
UPDATE telemetry_events SET total_cost = cost WHERE total_cost = 0 AND cost IS NOT NULL AND cost > 0;

-- ============================================================
-- 3. daily_org_summary  (ORM has extra cost/token breakdown columns)
-- ============================================================
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS llm_cost DECIMAL(14,6) DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS infra_cost DECIMAL(14,6) DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS external_cost DECIMAL(14,6) DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS total_prompt_tokens INT DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS total_completion_tokens INT DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS success_count INT DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS failure_count INT DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS anomaly_count INT DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS misuse_count INT DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS total_input_mb DECIMAL(12,4) DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS total_output_mb DECIMAL(12,4) DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS avg_risk_score DECIMAL(8,2) DEFAULT 0;
ALTER TABLE daily_org_summary ADD COLUMN IF NOT EXISTS project_id VARCHAR(100);

-- ============================================================
-- 3b. tool_connectors  (DB has tool_id NOT NULL, ORM doesn't use it)
-- ============================================================
ALTER TABLE tool_connectors ALTER COLUMN tool_id DROP NOT NULL;
ALTER TABLE tool_connectors ALTER COLUMN tool_id SET DEFAULT NULL;

-- ============================================================
-- 3c. rate_limits  (DB has tool_id NOT NULL, ORM doesn't use it)
-- ============================================================
ALTER TABLE rate_limits ALTER COLUMN tool_id DROP NOT NULL;
ALTER TABLE rate_limits ALTER COLUMN tool_id SET DEFAULT NULL;

-- ============================================================
-- 4. monthly_org_summary  (ORM uses tool_name, not tool_id/model_id)
-- ============================================================
-- Make old columns nullable so ORM inserts (which omit them) succeed
ALTER TABLE monthly_org_summary ALTER COLUMN tool_id DROP NOT NULL;
ALTER TABLE monthly_org_summary ALTER COLUMN model_id DROP NOT NULL;
ALTER TABLE monthly_org_summary ADD COLUMN IF NOT EXISTS tool_name VARCHAR(150);
ALTER TABLE monthly_org_summary ADD COLUMN IF NOT EXISTS llm_cost DECIMAL(14,6) DEFAULT 0;
ALTER TABLE monthly_org_summary ADD COLUMN IF NOT EXISTS infra_cost DECIMAL(14,6) DEFAULT 0;
ALTER TABLE monthly_org_summary ADD COLUMN IF NOT EXISTS external_cost DECIMAL(14,6) DEFAULT 0;
ALTER TABLE monthly_org_summary ADD COLUMN IF NOT EXISTS total_prompt_tokens INT DEFAULT 0;
ALTER TABLE monthly_org_summary ADD COLUMN IF NOT EXISTS total_completion_tokens INT DEFAULT 0;
ALTER TABLE monthly_org_summary ADD COLUMN IF NOT EXISTS project_id VARCHAR(100);

-- ============================================================
-- 5. data_security_logs  (ORM expects event_id FK)
-- ============================================================
ALTER TABLE data_security_logs ADD COLUMN IF NOT EXISTS event_id VARCHAR(120);

-- ============================================================
-- 6. execution_pipeline  (ORM uses event_id, not telemetry_id)
-- ============================================================
ALTER TABLE execution_pipeline ADD COLUMN IF NOT EXISTS event_id VARCHAR(120);
ALTER TABLE execution_pipeline ADD COLUMN IF NOT EXISTS details JSON;

-- ============================================================
-- 7. tool_connectors  (ORM uses BigInteger id, DB uses VARCHAR)
--    This requires careful handling — skip if tool_connectors is empty
-- ============================================================
-- No structural ALTER needed if table was created by ORM (create_all).
-- If table was created manually from dbtable.txt, you may need to recreate it.

-- ============================================================
-- 8. governance_rules  (ORM has scope_reference)
-- ============================================================
ALTER TABLE governance_rules ADD COLUMN IF NOT EXISTS scope_reference VARCHAR(150);
ALTER TABLE governance_rules ADD COLUMN IF NOT EXISTS org_id VARCHAR(100);
ALTER TABLE governance_rules ADD COLUMN IF NOT EXISTS project_id VARCHAR(100);

-- ============================================================
-- 9. cost_breakdown  (exists in ORM but not in DB schema doc)
-- ============================================================
CREATE TABLE IF NOT EXISTS cost_breakdown (
    id BIGSERIAL PRIMARY KEY,
    event_id VARCHAR(120) REFERENCES telemetry_events(event_id),
    cost_type VARCHAR(50) NOT NULL,
    component_name VARCHAR(150),
    unit_cost DECIMAL(12,6) DEFAULT 0,
    quantity DECIMAL(12,6) DEFAULT 0,
    total_cost DECIMAL(12,6) DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Done. Verify with: SELECT table_name, column_name FROM information_schema.columns
--   WHERE table_schema = 'public' ORDER BY table_name, ordinal_position;
-- ============================================================
