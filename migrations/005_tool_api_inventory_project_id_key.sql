-- Migration: add project_id to tool_api_inventory unique constraint
-- so each project has its own per-tool latency stats instead of sharing one row.

ALTER TABLE tool_api_inventory
    DROP CONSTRAINT IF EXISTS tool_api_inventory_org_id_tool_name_function_name_key;

ALTER TABLE tool_api_inventory
    ADD CONSTRAINT tool_api_inventory_org_id_project_id_tool_name_function_name_key
    UNIQUE (org_id, project_id, tool_name, function_name);
