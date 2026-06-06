-- Migration 002: add missing columns to tool_connectors
-- These columns exist in the ORM (models.py) but were never added to the
-- existing DB table, causing 500 errors on GET /tools/connectors and
-- GET /ingestion/status when SQLAlchemy tries to SELECT them.

ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS org_id         VARCHAR(100);
ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS project_id     VARCHAR(100);
ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS api_key        VARCHAR(500);
ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS last_ingested_at TIMESTAMP;

-- governance_rules.org_id / project_id were added in 001 but may be missing
-- on databases that only partially ran that migration.
ALTER TABLE governance_rules ADD COLUMN IF NOT EXISTS org_id    VARCHAR(100);
ALTER TABLE governance_rules ADD COLUMN IF NOT EXISTS project_id VARCHAR(100);
