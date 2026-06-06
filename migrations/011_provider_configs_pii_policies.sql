-- =============================================================================
-- Migration: 011_provider_configs_pii_policies.sql
-- Description: Adds two tables required for the proxy layer:
--   1. provider_configs  — per-org AI provider credentials (OpenAI key, etc.)
--   2. pii_policies      — configurable PII type → action rules per org
-- Compatible:  PostgreSQL 13+
-- Safe to re-run: uses IF NOT EXISTS
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. provider_configs
--    Stores the real AI provider API key per org so the proxy can forward
--    requests. The api_key column stores an encrypted value; the application
--    layer is responsible for encrypt/decrypt (AES-256 or similar).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS provider_configs (
    id                  BIGSERIAL       PRIMARY KEY,
    config_id           VARCHAR(120)    NOT NULL UNIQUE,

    org_id              VARCHAR(100)    NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    project_id          VARCHAR(100)    REFERENCES projects(id) ON DELETE SET NULL,

    -- Provider identity
    provider            VARCHAR(100)    NOT NULL,
    -- provider values: openai | anthropic | google | azure_openai | aws_bedrock | cohere | mistral | custom

    -- Credentials (api_key stored encrypted — never log in plaintext)
    api_key             TEXT            NOT NULL,
    api_key_hint        VARCHAR(20),    -- last 4 chars shown in UI, e.g. "...ab12"

    -- Optional endpoint override (Azure OpenAI, custom deployments, etc.)
    base_url            VARCHAR(500),

    -- Allowed models for this config (NULL = all models allowed)
    model_allowlist     JSONB,          -- ["gpt-4o", "gpt-4-turbo", "gpt-3.5-turbo"]

    -- Rate limit caps this org is allowed to send through this config
    max_rpm             INTEGER,        -- max requests per minute (NULL = unlimited)
    max_tpm             INTEGER,        -- max tokens per minute (NULL = unlimited)

    is_active           BOOLEAN         DEFAULT TRUE,
    created_by          VARCHAR(100),   -- user_id who created this config
    created_at          TIMESTAMP       DEFAULT NOW(),
    updated_at          TIMESTAMP       DEFAULT NOW(),

    UNIQUE (org_id, provider)           -- one config per provider per org
);

CREATE INDEX IF NOT EXISTS idx_provider_configs_org     ON provider_configs (org_id);
CREATE INDEX IF NOT EXISTS idx_provider_configs_active  ON provider_configs (org_id, provider) WHERE is_active = TRUE;


-- ---------------------------------------------------------------------------
-- 2. pii_policies
--    Defines what action to take for each PII type, per org.
--    Rows with org_id = NULL are global defaults applied to all orgs unless
--    an org-specific row overrides them.
--
--    action values:
--      mask    — replace PII with a placeholder, e.g. [EMAIL], request still forwarded
--      block   — reject the request entirely, return 403 to the caller
--      alert   — forward the request but fire an alert in the audit log
--      allow   — pass through with no action (useful to override a global rule)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pii_policies (
    id              BIGSERIAL       PRIMARY KEY,
    policy_id       VARCHAR(120)    NOT NULL UNIQUE,

    -- Scope: NULL org_id = global default; specific org_id = org override
    org_id          VARCHAR(100)    REFERENCES organizations(id) ON DELETE CASCADE,

    -- PII classification
    pii_type        VARCHAR(100)    NOT NULL,
    -- pii_type values: email | phone | name | national_id | aadhar | ssn |
    --                  credit_card | date_of_birth | ip_address | custom

    risk_level      VARCHAR(20)     NOT NULL DEFAULT 'medium',
    -- risk_level: low | medium | high | critical

    -- Action to take when this PII type is detected
    action          VARCHAR(20)     NOT NULL DEFAULT 'mask',
    -- action: mask | block | alert | allow

    -- When action = mask, what placeholder string to use
    mask_pattern    VARCHAR(100)    DEFAULT '[{pii_type}]',
    -- e.g. '[EMAIL]', '[PHONE]', '***-**-{last4}'

    -- Whether to also create an audit_log entry on detection
    log_detection   BOOLEAN         DEFAULT TRUE,

    -- Priority: higher number = evaluated first when multiple policies match
    priority        INTEGER         DEFAULT 0,

    description     TEXT,
    is_active       BOOLEAN         DEFAULT TRUE,
    created_at      TIMESTAMP       DEFAULT NOW(),
    updated_at      TIMESTAMP       DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pii_policies_org         ON pii_policies (org_id);
CREATE INDEX IF NOT EXISTS idx_pii_policies_type        ON pii_policies (pii_type);
CREATE INDEX IF NOT EXISTS idx_pii_policies_active      ON pii_policies (is_active) WHERE is_active = TRUE;

-- ---------------------------------------------------------------------------
-- 3. Seed global default PII policies
--    These match exactly the table from the governance document.
--    org_id = NULL means they apply to every org unless overridden.
-- ---------------------------------------------------------------------------
INSERT INTO pii_policies (policy_id, org_id, pii_type, risk_level, action, mask_pattern, priority, description)
VALUES
    ('global-email',       NULL, 'email',       'high',     'mask',  '[EMAIL]',       10, 'Email address — mask before forwarding'),
    ('global-phone',       NULL, 'phone',       'high',     'mask',  '[PHONE]',       10, 'Phone number — mask before forwarding'),
    ('global-name',        NULL, 'name',        'medium',   'mask',  '[NAME]',         5, 'Customer name — mask before forwarding'),
    ('global-national_id', NULL, 'national_id', 'critical', 'block', '[NATIONAL_ID]', 20, 'National ID (Aadhar/SSN) — block request'),
    ('global-aadhar',      NULL, 'aadhar',      'critical', 'block', '[AADHAR]',      20, 'Aadhar number — block request'),
    ('global-ssn',         NULL, 'ssn',         'critical', 'block', '[SSN]',         20, 'SSN — block request'),
    ('global-credit_card', NULL, 'credit_card', 'critical', 'block', '[CREDIT_CARD]', 20, 'Credit card number — block request')
ON CONFLICT (policy_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. Add governance_key column to api_keys
--    External teams authenticate to the proxy using a governance key stored
--    in api_keys. Add a hashed_key column so we can verify without storing
--    the raw value, and a is_proxy_key flag to distinguish proxy keys from
--    other api_keys.
-- ---------------------------------------------------------------------------
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS hashed_key     VARCHAR(255),
    ADD COLUMN IF NOT EXISTS raw_key_hint   VARCHAR(20),    -- e.g. "gov-...ab12" shown in UI
    ADD COLUMN IF NOT EXISTS is_proxy_key   BOOLEAN DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS is_active      BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS last_used_at   TIMESTAMP,
    ADD COLUMN IF NOT EXISTS expires_at     TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_api_keys_hashed     ON api_keys (hashed_key) WHERE hashed_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_api_keys_proxy_active ON api_keys (is_proxy_key, is_active) WHERE is_proxy_key = TRUE;

COMMIT;
