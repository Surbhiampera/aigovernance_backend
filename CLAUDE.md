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

This is a **FastAPI governance proxy** for AI API traffic. All requests from external teams go through this server, which enforces policies before forwarding to the actual AI provider (Azure OpenAI). The server also provides admin APIs for dashboards.

### Request lifecycle (proxy.py → Azure OpenAI)

```
X-Governance-Key header
  → governance_key_service: resolve org_id + project_id
  → governance_rule_service: model allow/block lists, token limits
  → budget_service: org & project monthly spend limits
  → rate_limit_service: per-key & per-project rate limits
  → pii_engine: mask or block sensitive data
  → token_counter (tiktoken): estimate input tokens, store AiRequest row
  → httpx: forward to Azure OpenAI using admin credentials from config.py
  → cost_engine: calculate cost from token counts + pricing
  → store AiResponse, TokenUsage, RequestCost, AuditLog
```

All enforcement decisions are logged to `audit_logs`. A 403 from governance rules, 429 from budget/rate limits.

### Key files

| File | Role |
|------|------|
| `app/main.py` | App factory, router registration, startup/shutdown lifecycle |
| `app/routers/proxy.py` | Main enforcement gateway (~35KB) |
| `app/models.py` | All SQLAlchemy ORM models (~40KB) |
| `app/services/governance_rule_service.py` | Model allow/block, token ceiling enforcement |
| `app/services/budget_service.py` | Monthly spend limit checks, alert generation |
| `app/services/rate_limit_service.py` | Per-key and per-project rate limiting |
| `app/services/cost_engine.py` | Cost calculation from token counts + pricing catalog |
| `app/services/pii_engine.py` | Regex-based PII detection and masking |
| `app/scheduler.py` | APScheduler: hourly daily-agg, daily monthly-agg |
| `app/config.py` | All environment variable accessors |

### Database

PostgreSQL (Aiven cloud). The app runs safe `ALTER TABLE` statements at startup to add missing columns — no separate migration tool. Tests use an in-memory SQLite DB (see `tests/conftest.py`).

Connection pool: conservative defaults (`pool_size=3`, `max_overflow=0`, `pool_recycle=1800`) to handle Aiven's idle connection timeout.

### Multi-tenancy

All data is scoped by `org_id` and `project_id`. API keys (`api_keys` table, hashed) resolve to an org+project pair. The governance rules, budgets, and rate limits can be set at both org and project scope.

### Vendor adapters (ingestion)

`app/services/ingestion/` contains a pluggable adapter registry. Each vendor (openai, anthropic, google, generic) registers with `@adapter_registry.register`. New providers only need a new adapter file — no changes to core ingestion logic.

### Background jobs

APScheduler runs in-process (not Celery). Two jobs:
- Every **1 hour**: roll up `AiRequest + RequestCost` → `DailyOrgSummary`
- Every **24 hours**: roll up `DailyOrgSummary` → `MonthlyOrgSummary`

### Environment variables

Key variables expected in `.env`:
- `DATABASE_URL` — PostgreSQL connection string
- `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME`, `OPENAI_API_VERSION` — Admin-only credentials used for all proxied calls; never exposed to tenants
- `CORS_ORIGINS` — Comma-separated allowed origins
- `SCHEDULER_MAX_WORKERS` — APScheduler thread count (default 2)
- Threshold variables: `ALERT_BUDGET_DEFAULT_THRESHOLD_PCT`, `ANOMALY_SPIKE_RATIO`, etc. (see `app/config.py` for full list)

`.env` is gitignored. Never commit it. The `.claude/settings.json` explicitly blocks reading `.env` files.
