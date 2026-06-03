-- Drop tables that have no SQLAlchemy models and are not referenced anywhere in the codebase.
-- Safe to remove: no FK references point to any of these tables from other tables.

DROP TABLE IF EXISTS email_agent_logs;
DROP TABLE IF EXISTS upload_data;
DROP TABLE IF EXISTS rate_limit_violations;
DROP TABLE IF EXISTS user_projects;
