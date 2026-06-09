from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_cors_origins
from app.routers import (
    alerts,
    apikeys,
    audit_logs,
    auth,
    budgets,
    costs,
    governance,
    governance_keys,
    lookups,
    models,
    organizations,
    pricing,
    projects,
    summary,
)
from app.routers.deployments import router as deployments_router
from app.routers.proxy import router as proxy_router

# Safe schema additions — all use IF NOT EXISTS, safe to re-run on every startup.
_SAFE_ALTERS = [
    # Governance keys on api_keys table
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS hashed_key VARCHAR(64)",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS raw_key_hint VARCHAR(30)",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS is_proxy_key BOOLEAN DEFAULT FALSE",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at TIMESTAMP",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMP",
    "ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS role VARCHAR(50)",
    # Proxy request tables
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS governance_key_id VARCHAR(120)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS source_ip VARCHAR(60)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS user_agent VARCHAR(500)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP",
    # Audit log
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS request_id VARCHAR(120)",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS trace_id VARCHAR(120)",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS old_value JSONB",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS new_value JSONB",
    "ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS audit_metadata JSONB",
    # Token usage provenance
    "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS input_token_source VARCHAR(30)",
    "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS output_token_source VARCHAR(30)",
    "ALTER TABLE token_usage ADD COLUMN IF NOT EXISTS is_estimated BOOLEAN DEFAULT FALSE",
    # Rate limit — per-key and per-project granularity
    "ALTER TABLE rate_limits ADD COLUMN IF NOT EXISTS project_id VARCHAR(100)",
    "ALTER TABLE rate_limits ADD COLUMN IF NOT EXISTS key_id VARCHAR(120)",
    # Provenance: deployment tracked per request
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS source_system VARCHAR(255)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS deployment_name VARCHAR(255)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS completed_at TIMESTAMP",
    # Provenance: token counts co-located with cost for single-table reporting
    "ALTER TABLE request_cost ADD COLUMN IF NOT EXISTS input_tokens INTEGER DEFAULT 0",
    "ALTER TABLE request_cost ADD COLUMN IF NOT EXISTS output_tokens INTEGER DEFAULT 0",
    "ALTER TABLE request_cost ADD COLUMN IF NOT EXISTS total_tokens INTEGER DEFAULT 0",
    "ALTER TABLE request_cost ADD COLUMN IF NOT EXISTS cost_model_type VARCHAR(50)",
    "ALTER TABLE request_cost ADD COLUMN IF NOT EXISTS pricing_snapshot JSONB",
    "ALTER TABLE request_cost ADD COLUMN IF NOT EXISTS pricing_version VARCHAR(50)",
    # DB-generated IDs so proxy omitting them never causes NOT NULL violations
    "ALTER TABLE token_usage ALTER COLUMN token_usage_id SET DEFAULT concat('tu-', replace(gen_random_uuid()::text, '-', ''))",
    "ALTER TABLE request_cost ALTER COLUMN cost_id SET DEFAULT concat('cu-', replace(gen_random_uuid()::text, '-', ''))",
    # Audit index for policy-violation dashboard queries
    (
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_policy "
        "ON audit_logs (org_id, audit_action, occurred_at) "
        "WHERE policy_triggered = TRUE"
    ),
    # Multi-deployment routing columns (added to existing model_deployments table)
    "ALTER TABLE model_deployments ADD COLUMN IF NOT EXISTS api_key TEXT",
    "ALTER TABLE model_deployments ADD COLUMN IF NOT EXISTS api_version VARCHAR(50)",
    "ALTER TABLE model_deployments ADD COLUMN IF NOT EXISTS is_default BOOLEAN DEFAULT FALSE",
    # PII-masked version of the prompt stored alongside the original
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS sanitized_prompt_text TEXT",
    # PII detection outcome columns
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS pii_action_taken VARCHAR(20)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS pii_types JSONB",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS content_policy_flags JSONB",
    # Request lifecycle timestamp
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMP",
    # Request Cost Log query columns (added after initial schema)
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS client_ip VARCHAR(50)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS provider VARCHAR(100)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS received_at TIMESTAMP DEFAULT NOW()",
]

_ALL_ROUTERS = [
    auth.router,
    summary.router,
    models.router,
    alerts.router,
    costs.router,
    governance.router,
    governance_keys.router,
    organizations.router,
    projects.router,
    budgets.router,
    pricing.router,
    apikeys.router,
    lookups.router,
    audit_logs.router,
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    from sqlalchemy import text
    from app.database import Base, engine
    from app.scheduler import start_scheduler, stop_scheduler

    try:
        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
            for stmt in _SAFE_ALTERS:
                try:
                    conn.execute(text(stmt))
                except Exception:
                    pass
            conn.commit()
    except Exception:
        pass

    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()
        engine.dispose()


app = FastAPI(
    title="AI Governance Platform",
    version="3.0.0",
    description="Centralized governance proxy — all AI traffic flows through this server.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Propagate X-Request-Id from enforcement-gate exceptions to response headers.

    When our budget/governance/rate-limit services raise HTTPException with a
    dict detail containing 'request_id', that ID is surfaced as a response header
    so callers can correlate blocked requests with audit_logs without parsing the body.
    """
    detail = exc.detail
    headers: dict = dict(exc.headers or {})
    if isinstance(detail, dict) and "request_id" in detail:
        headers["X-Request-Id"] = detail["request_id"]
    content = detail if isinstance(detail, dict) else {"detail": detail}
    return JSONResponse(status_code=exc.status_code, content=content, headers=headers)

for _router in _ALL_ROUTERS:
    app.include_router(_router)

# Proxy registered at root — stable URL for external teams' SDK base_url.
app.include_router(proxy_router)
app.include_router(deployments_router)


@app.get("/health")
def health_check():
    return {"status": "healthy", "version": "3.0.0"}
