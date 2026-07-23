import logging
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
    optimization_tips,
    organizations,
    pricing,
    projects,
    summary,
)
from app.routers.rate_limits import router as rate_limits_router
from app.routers.deployments import router as deployments_router
from app.routers.proxy import router as proxy_router
from app.routers.proxy import proxy_chat_openai_compat

_log = logging.getLogger(__name__)

# Tables declared in app/models.py that no live code path reads or writes, and
# that schema_clean.sql therefore does not create. Excluded from the startup
# schema check so their absence is not treated as an incomplete migration.
# Keep in sync with the "INTENTIONALLY OMITTED TABLES" footer in schema_clean.sql.
_UNUSED_TABLES = frozenset({
    "model_registry",
    "tool_registry",
    "tool_connectors",
    "connector_sync_logs",
    "providers",
    "model_versions",
    "ai_routes",
    "project_model_usage",
    "provider_configs",
    "decorator_registrations",
    "tool_api_inventory",
    "telemetry_events",
    "cost_breakdown",
    "execution_pipeline",
    "trace_model_usage",
    "trace_tool_usage",
    "request_response_logs",
})

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
    optimization_tips.router,
]


class SchemaOutOfDateError(RuntimeError):
    """Raised at startup when the database is missing required tables/columns."""


def _verify_schema() -> None:
    """Read-only check that the database matches app/models.py.

    The application performs no DDL — schema_clean.sql must be applied before the
    backend starts. This is a single pair of catalogue queries, deliberately
    read-only: it takes no locks and holds no transaction open, so it can never
    block on a concurrent ALTER the way the old startup DDL pass did.
    """
    from sqlalchemy import text
    from app.database import Base, engine

    expected = {
        t.name: {c.name.lower() for c in t.columns}
        for t in Base.metadata.sorted_tables
        if t.name not in _UNUSED_TABLES
    }

    with engine.connect() as conn:
        # Belt and braces: if anything unexpected does hold a lock, fail fast
        # instead of hanging the whole startup indefinitely.
        conn.execute(text("SET lock_timeout = '5s'"))
        conn.execute(text("SET statement_timeout = '15s'"))
        rows = conn.execute(text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        )).all()

    actual: dict[str, set[str]] = {}
    for table_name, column_name in rows:
        actual.setdefault(table_name, set()).add(column_name.lower())

    missing_tables = sorted(t for t in expected if t not in actual)
    missing_columns = {
        t: sorted(cols - actual[t])
        for t, cols in expected.items()
        if t in actual and (cols - actual[t])
    }

    if not missing_tables and not missing_columns:
        _log.info("Schema check passed (%d tables verified)", len(expected))
        return

    problems = []
    if missing_tables:
        problems.append(f"missing tables: {', '.join(missing_tables)}")
    for t, cols in sorted(missing_columns.items()):
        problems.append(f"{t} is missing columns: {', '.join(cols)}")

    detail = "\n  - ".join(problems)
    raise SchemaOutOfDateError(
        f"Database schema is incomplete:\n  - {detail}\n\n"
        f"The application does not create or alter tables. Apply the schema first:\n"
        f"    psql \"$DATABASE_URL\" -v ON_ERROR_STOP=1 -f schema_clean.sql"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.database import engine
    from app.scheduler import start_scheduler, stop_scheduler

    # Refuse to serve traffic against a half-migrated database. Raising here
    # aborts startup, so the port is never opened.
    _verify_schema()

    # Starts the hourly/daily aggregation jobs plus a one-shot summary backfill.
    # The backfill runs on a scheduler thread rather than inline, so startup does
    # not block on up to 90 days of aggregation before binding the port.
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
