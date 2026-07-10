from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.config import get_cors_origins
from app.core.deps import get_db
from app.routers import (
    alerts,
    alerts_security,
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
from app.routers.rate_limits import router as rate_limits_router
from app.routers.deployments import router as deployments_router
from app.routers.proxy import router as proxy_router
from app.routers.proxy import proxy_chat_openai_compat

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
    # Correlation id so multiple LLM calls from one user turn (e.g. a
    # classify -> tool-select -> response-generation chatbot pipeline) can
    # be grouped together via GET /proxy/v1/requests?group_by=trace_id
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS trace_id VARCHAR(120)",
    "CREATE INDEX IF NOT EXISTS ix_ai_requests_trace_id ON ai_requests (trace_id)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS parent_request_id VARCHAR(120)",
    "CREATE INDEX IF NOT EXISTS ix_ai_requests_parent_request_id ON ai_requests (parent_request_id)",
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
    # Structured failure logging — every blocked/errored request gets a code + reason
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS failure_code VARCHAR(50)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS failure_reason TEXT",
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
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS entry_point VARCHAR(100)",
    # PII detection flags (may be missing on DBs created before presidio integration)
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS pii_detected BOOLEAN DEFAULT FALSE",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS pii_masked BOOLEAN DEFAULT FALSE",
    # PII severity and entity counts for per-request detail view
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS pii_severity VARCHAR(10)",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS pii_entities_detected INTEGER DEFAULT 0",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS pii_entities_masked INTEGER DEFAULT 0",
    "ALTER TABLE ai_requests ADD COLUMN IF NOT EXISTS pii_detail JSONB",
    # Backfill severity + entity count for existing rows using stored pii_types JSONB.
    # WHERE pii_severity IS NULL ensures this is a no-op on every subsequent startup.
    (
        "UPDATE ai_requests SET"
        "  pii_entities_detected = COALESCE(jsonb_array_length(pii_types::jsonb), 0),"
        "  pii_severity = CASE"
        "    WHEN pii_types::jsonb ?| ARRAY['aadhar','credit_card','passport','ssn','bank_account','national_id'] THEN 'high'"
        "    WHEN jsonb_array_length(pii_types::jsonb) >= 5 THEN 'high'"
        "    WHEN jsonb_array_length(pii_types::jsonb) >= 3 THEN 'medium'"
        "    WHEN jsonb_array_length(pii_types::jsonb) >= 1 THEN 'low'"
        "    ELSE NULL"
        "  END"
        " WHERE pii_detected = TRUE"
        "   AND pii_severity IS NULL"
        "   AND pii_types IS NOT NULL"
    ),
]

_ALL_ROUTERS = [
    auth.router,
    summary.router,
    models.router,
    alerts.router,
    alerts_security.router,
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
    rate_limits_router,
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

    # Backfill DailyOrgSummary for any past days that have AiRequest data but no summary.
    import datetime as _dt
    import logging as _logging
    _bflog = _logging.getLogger(__name__)
    try:
        from app.database import SessionLocal
        from app.workers.tasks import (
            _detect_daily_anomalies,
            _rebuild_daily_summary,
            _rebuild_daily_user_summary,
        )
        from app.models import AiRequest as _AiReq, DailyOrgSummary as _Daily, DailyUserUsage as _DailyUser
        from sqlalchemy import func as _func
        db = SessionLocal()
        try:
            dates_with_requests = (
                db.query(_func.date(_AiReq.created_at).label("d"))
                .filter(_AiReq.created_at >= _dt.datetime.utcnow() - _dt.timedelta(days=90))
                .distinct()
                .all()
            )
            dates_already_summarised = {
                r[0] for r in db.query(_Daily.date).distinct().all()
            }
            dates_already_user_summarised = {
                r[0] for r in db.query(_DailyUser.date).distinct().all()
            }
            for (d,) in dates_with_requests:
                if d not in dates_already_summarised:
                    try:
                        _rebuild_daily_summary(db=db, summary_date=d)
                        _detect_daily_anomalies(db=db, summary_date=d)
                        db.commit()
                        _bflog.info("Backfilled DailyOrgSummary for %s", d)
                    except Exception:
                        db.rollback()
                        _bflog.exception("Backfill failed for %s", d)
                if d not in dates_already_user_summarised:
                    try:
                        _rebuild_daily_user_summary(db=db, summary_date=d)
                        db.commit()
                    except Exception:
                        db.rollback()
                        _bflog.exception("DailyUserUsage backfill failed for %s", d)
        finally:
            db.close()
    except Exception:
        _bflog.exception("Startup backfill failed")

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

# All proxy endpoints live under /proxy (see router prefix in app/routers/proxy.py).
# External teams must set their SDK base_url to "https://<host>/proxy".
app.include_router(proxy_router)
app.include_router(deployments_router)


# Root-level aliases for misconfigured OpenAI SDK clients that set base_url to
# the bare host (or "/v1") instead of "/proxy" — the SDK then requests
# /chat/completions or /v1/chat/completions at root, which would otherwise 404.
@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def root_chat_completions_alias(
    request: Request,
    background_tasks: BackgroundTasks,
    model: Optional[str] = Query(None, description="AI model name (overrides body 'model' field)"),
    x_governance_key: str = Header(..., alias="X-Governance-Key"),
    x_trace_id: Optional[str] = Header(None, alias="X-Trace-Id"),
    db: Session = Depends(get_db),
):
    return await proxy_chat_openai_compat(
        request=request, background_tasks=background_tasks, model=model,
        x_governance_key=x_governance_key, x_trace_id=x_trace_id, db=db,
    )


@app.get("/health")
def health_check():
    return {"status": "healthy", "version": "3.0.0"}


@app.get("/health/detailed")
def health_check_detailed():
    """Capacity/health diagnostics for alerting — not used as the liveness
    probe (kept separate from /health so a slow DB or open circuit doesn't
    make the platform think the instance itself is dead and kill it).
    """
    from app.database import engine
    from app.routers.proxy import _azure_circuit, _azure_limiter
    from app.scheduler import get_scheduler_heartbeat

    pool = engine.pool
    db_pool_stats = {}
    for attr in ("checkedout", "checkedin", "overflow", "size"):
        try:
            db_pool_stats[attr] = getattr(pool, attr)()
        except Exception:
            pass
    return {
        "status": "healthy",
        "db_pool": db_pool_stats,
        "azure": {
            "concurrent_in_flight": _azure_limiter.in_flight,
            "concurrent_max": _azure_limiter.max_concurrent,
            "circuit_open": _azure_circuit.is_open,
        },
        "scheduler": get_scheduler_heartbeat(),
    }
