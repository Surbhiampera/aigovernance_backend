# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the development server
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run all tests
pytest tests/

# Run a single test file
pytest tests/test_proxy.py

# Run a single test by name
pytest tests/test_proxy.py::test_function_name -v

# Install dependencies
pip install -r requirements.txt
```

## Architecture

This is a **FastAPI governance proxy** for AI API traffic, multi-provider (Azure OpenAI, OpenAI, Anthropic, Google Gemini). All requests from external teams go through this server, which enforces policies before forwarding to the resolved provider deployment. The server also provides admin APIs for dashboards.

> For the authoritative, code-verified walkthrough of every pre-flight stage, failure mode, and known gap, see [`GOVERNANCE_WORKFLOW.md`](GOVERNANCE_WORKFLOW.md). For the full external-facing API reference (all endpoints, request/response shapes), see [`How_to_use_proxyserver_REFERENCE.md`](How_to_use_proxyserver_REFERENCE.md). The summary below is a condensed pointer, not a replacement — when the two disagree, trust `GOVERNANCE_WORKFLOW.md` (or re-derive from the code).

### Request lifecycle (`app/routers/proxy.py`, mounted at `/proxy`)

Implemented in `_run_pre_flight()`, shared by `/proxy`, `/proxy/stream`, and the `/proxy/chat/completions` and `/proxy/v1/chat/completions` aliases. Root-level `/chat/completions` and `/v1/chat/completions` also exist in `app/main.py` as aliases for misconfigured SDK clients pointing `base_url` at the bare host.

```
X-Governance-Key header → verify_governance_key(): resolve org_id + project_id
  → rate_limit_service: per-key/project/org check (runs BEFORE body parse — fails open)
  → parse JSON body
  → deployment_service: resolve candidate provider deployments for the requested model (404 if none)
  → budget_service: org & project monthly spend limits (fails open)
  → pii_engine (Presidio): mask/block/alert per entity-type policy (fails open)
  → store AiRequest row (request_status="pending") — durable before hitting upstream
  → forward to resolved provider (retry + failover across candidate deployments)
  → background task (non-stream) / sync at generator end (stream): token_counter,
    proxy.py's own _calculate_cost(), store AiResponse/TokenUsage/RequestCost/AuditLog/RouteExecution
```

All enforcement decisions are logged to `audit_logs`. 403 = PII block, 429 = budget/rate-limit, 404 = unresolvable model/deployment, 502/503 = upstream unreachable / circuit breaker / concurrency limiter full.

**Known gap:** `governance_rule_service.py` (model allow/block lists, token ceilings) is fully implemented and administered via its router, but is **not called anywhere in the live proxy path** — it currently has zero effect on traffic. See §6/§14 of `GOVERNANCE_WORKFLOW.md`.

### Multi-provider deployments

`app/services/deployment_service.py` resolves the Azure/OpenAI/Anthropic/Google deployment for a given org/project/model via the `ModelDeployment` table (`app/models.py`), ranked project-specific → org-wide, default → non-default, earliest-registered first. Falls back to synthetic deployments built from `.env` vars (`AZURE_OPENAI_*`, `OPENAI_*`, `AZURE_*`, `TTS_AZURE_OPENAI_*`) when no DB row matches, with a warning logged. Admins register deployments via `POST /deployments` (`app/routers/deployments.py`); `provision_standard_deployments()` seeds org-wide defaults for new orgs from `config.get_standard_model_deployments()`.

### Per-user tracking

Optional `X-User-Id` (plus `X-User-Email`/`X-User-Role`) proxy headers attribute requests to an individual user. Rolled up into `DailyUserUsage` alongside the existing `DailyOrgSummary`, and exposed via `GET /costs/by-user`. Requests without a user header are excluded from per-user tables but still counted org-wide.

### Optimization tips engine

`app/services/optimization/` evaluates cost/prompt-shape advice on a daily schedule (`optimization_tips` job in `app/scheduler.py`, 24h interval → `_generate_optimization_tips()` in `app/workers/tasks.py`), scanning a rolling 30-day window and writing `OptimizationTip` rows exposed via `app/routers/optimization_tips.py`. Rules are pluggable, following the same self-registration pattern as the ingestion adapters: each rule in `app/services/optimization/rules/` subclasses `TipRule` and self-registers with `@tip_registry.register` (`app/services/optimization/registry.py`); the scheduled job catches exceptions per-rule so one broken rule can't block the others. Current rules: cache-opportunity (repeated/duplicate prompts), model-substitution (cheaper deployed model would suffice), oversized-prompt, response-length, and response-truncated (finish_reason == "length"). Notably, this job reads `GovernanceRule` rows directly to filter suggestions to models the org has deployed and hasn't block-listed — it's a second, independent consumer of governance rules despite the live proxy path not calling `governance_rule_service` at all (see the known gap above).

### Key files

| File | Role |
|------|------|
| `app/main.py` | App factory, router registration, root-level SDK-compat aliases, startup/shutdown lifecycle (incl. daily-summary backfill on boot) |
| `app/routers/proxy.py` | Main enforcement gateway, mounted at `/proxy` (~35KB) |
| `app/routers/deployments.py` | CRUD for `ModelDeployment` rows (multi-provider deployment registry) |
| `app/models.py` | All SQLAlchemy ORM models, incl. `ModelDeployment`, `DailyUserUsage`, `UsageAnomaly` (~40KB) |
| `app/services/deployment_service.py` | Resolves provider deployment for org/project/model, builds provider-specific request URL/headers |
| `app/services/governance_rule_service.py` | Model allow/block, token ceiling enforcement — **implemented but not wired into the proxy path** |
| `app/services/budget_service.py` | Monthly spend limit checks, alert generation |
| `app/services/rate_limit_service.py` | Per-key and per-project rate limiting (Redis-backed, Postgres fallback) |
| `app/services/cost_engine.py` | Cost calc used only by the ingestion pipeline — the live proxy path uses its own `_calculate_cost()` in `proxy.py` instead; the two can drift |
| `app/services/pii_engine.py` | Presidio-based PII detection and masking (Aadhaar/PAN custom recognizers) |
| `app/core/deps.py` | `get_db`, `require_api_key`/`require_role(...)` — admin-API auth (distinct from the proxy's `X-Governance-Key`) |
| `app/services/optimization/registry.py`, `app/services/optimization/rules/*` | Pluggable optimization-tip rules (self-register via `@tip_registry.register`) |
| `app/workers/tasks.py` | Aggregation functions called by the scheduler: `_rebuild_daily_summary`, `_rebuild_daily_user_summary`, `_detect_daily_anomalies`, `_rebuild_monthly_summary`, `_generate_optimization_tips` |
| `app/scheduler.py` | APScheduler: hourly daily-agg (summary + user summary + anomaly detection), daily monthly-agg |
| `app/config.py` | All environment variable accessors |

### Database

PostgreSQL (Azure Database for PostgreSQL).

**The application performs no DDL.** There is no `create_all` and no startup `ALTER TABLE` pass. [`schema_clean.sql`](schema_clean.sql) is the single source of truth and must be applied **before** the backend starts:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema_clean.sql   # idempotent, safe to re-run
```

On boot, `_verify_schema()` in `app/main.py` does a read-only `information_schema` check against `Base.metadata` and **refuses to start** if any required table or column is missing, naming exactly what's absent. When you add a column to `app/models.py`, add it to `schema_clean.sql` in the same commit or the next boot will fail.

`app/models.py` still declares 17 tables that no live code path touches (ingestion pipeline, telemetry). These are deliberately absent from the schema and are listed in `_UNUSED_TABLES` in `app/main.py`; the two lists must stay in sync.

Tests run against the real `DATABASE_URL`, each inside a transaction that is rolled back on teardown — no tables are created or dropped (see `tests/conftest.py`).

Connection pool: conservative defaults (`pool_size=3`, `max_overflow=0`, `pool_recycle=1800`) to survive the managed server's idle connection timeout.

### Multi-tenancy

All data is scoped by `org_id` and `project_id`. API keys (`api_keys` table, hashed) resolve to an org+project pair. The governance rules, budgets, and rate limits can be set at both org and project scope.

### Two separate auth mechanisms — don't confuse them

- **`X-Governance-Key`** (tenant traffic): consumed only by `/proxy/*` and the root-level SDK-compat aliases in `app/main.py`. `verify_governance_key()` resolves it to an org+project pair; this is what external teams put in their SDK client config.
- **`X-API-Key`** (admin/dashboard APIs): consumed by every other router (deployments, budgets, costs, optimization-tips, etc.) via `require_api_key`/`require_role(...)` in `app/core/deps.py`. Checked against the `api_keys` table, or against `GOVERNANCE_MASTER_KEY` (env var) for bootstrap access before any per-org keys exist. Roles are `viewer` < `security_reviewer` < `admin`; a key with no role defaults to `viewer`, and the master key is always treated as `admin`.

These two header/key spaces are unrelated — a valid governance key does not grant access to admin endpoints and vice versa.

### Vendor adapters (ingestion)

`app/services/ingestion/` contains a pluggable adapter registry. Each vendor (openai, anthropic, google, generic) registers with `@adapter_registry.register`. New providers only need a new adapter file — no changes to core ingestion logic.

### Background jobs

APScheduler runs in-process (not Celery), `coalesce=True, max_instances=1`. Two scheduled jobs:
- Every **1 hour** (`daily_agg`): `_rebuild_daily_summary` (`AiRequest + RequestCost` → `DailyOrgSummary`) → `_rebuild_daily_user_summary` (→ `DailyUserUsage`) → `_detect_daily_anomalies` (compares today vs an N-day baseline → `UsageAnomaly` rows, backfills `DailyOrgSummary.anomaly_count`)
- Every **24 hours** (`monthly_agg`): roll up `DailyOrgSummary` → `MonthlyOrgSummary`

Both are idempotent (delete-then-reinsert for the current period). Manual re-trigger/backfill: `POST /summary/admin/rebuild-daily?days_back=N`. On app startup, `app/main.py`'s lifespan handler backfills any missing `DailyOrgSummary`/`DailyUserUsage` days for dates that already have `AiRequest` rows.

### Environment variables

Key variables expected in `.env`:
- `DATABASE_URL` — PostgreSQL connection string
- `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME`, `OPENAI_API_VERSION` — Admin-only credentials, used as the env-fallback deployment when no `ModelDeployment` DB row matches; never exposed to tenants
- `OPENAI_API_KEY`, `OPENAI_ENDPOINT`, `OPENAI_DEPLOYMENT_NAME`, `OPENAI_API_VERSION` — env-fallback deployment for a second OpenAI-routed model
- `AZURE_API_KEY`, `AZURE_ENDPOINT`, `AZURE_DEPLOYMENT`, `AZURE_API_VERSION` — env-fallback deployment (e.g. gpt-4o)
- `TTS_AZURE_OPENAI_API_KEY`, `TTS_AZURE_OPENAI_ENDPOINT`, `TTS_AZURE_OPENAI_DEPLOYMENT`, `TTS_AZURE_OPENAI_API_VERSION` — env-fallback deployment registered for resolution only; no `/audio/speech` route exists yet
- `REDIS_URL` — backs `rate_limit_service`; falls back to Postgres counts if unset/unreachable
- `CORS_ORIGINS` — Comma-separated allowed origins
- `SCHEDULER_MAX_WORKERS` — APScheduler thread count (default 2)
- Threshold variables: `ALERT_BUDGET_DEFAULT_THRESHOLD_PCT`, `ANOMALY_SPIKE_RATIO`, `ANOMALY_HIGH_SEVERITY_RATIO`, `ANOMALY_BASELINE_DAYS`, etc. (see `app/config.py` for full list)

`.env` is gitignored. Never commit it. The `.claude/settings.json` explicitly blocks reading `.env` files.
