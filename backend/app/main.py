import logging
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_cors_origins, get_log_level
from app.routers import (
    alerts,
    alerts_security,
    apikeys,
    auth,
    budgets,
    control,
    costs,
    governance,
    ingestion,
    lookups,
    models,
    organizations,
    pricing,
    projects,
    security,
    summary,
    telemetry,
    tools,
    workers,
)
from decorator.router import router as decorator_router

logging.basicConfig(
    level=get_log_level(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_SAFE_ALTERS = [
    "ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS org_id VARCHAR(100)",
    "ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS project_id VARCHAR(100)",
    "ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS api_key VARCHAR(500)",
    "ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS last_ingested_at TIMESTAMP",
    "ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS sync_enabled BOOLEAN DEFAULT TRUE",
    "ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS pull_interval_minutes INTEGER DEFAULT 15",
    "ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS last_sync_status VARCHAR(30)",
    "ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS last_sync_error TEXT",
    "ALTER TABLE tool_connectors ADD COLUMN IF NOT EXISTS total_events_pulled INTEGER DEFAULT 0",
    "ALTER TABLE governance_rules ADD COLUMN IF NOT EXISTS org_id VARCHAR(100)",
    "ALTER TABLE governance_rules ADD COLUMN IF NOT EXISTS project_id VARCHAR(100)",
    # Org/project traceability columns
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS project_id VARCHAR(100)",
    "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS tool_name VARCHAR(150)",
    "ALTER TABLE data_security_logs ADD COLUMN IF NOT EXISTS org_id VARCHAR(100)",
    "ALTER TABLE data_security_logs ADD COLUMN IF NOT EXISTS project_id VARCHAR(100)",
    "ALTER TABLE usage_anomalies ADD COLUMN IF NOT EXISTS project_id VARCHAR(100)",
    # Decorator-framework columns (migration 003)
    "ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS tool_name VARCHAR(150)",
    "ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS function_name VARCHAR(255)",
    "ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS module_path VARCHAR(500)",
    "ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS decorator_type VARCHAR(50)",
    "ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS execution_env VARCHAR(50) DEFAULT 'production'",
    "ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS sdk_version VARCHAR(20)",
    "ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS tool_version VARCHAR(50)",
    "ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS input_preview TEXT",
    "ALTER TABLE telemetry_events ADD COLUMN IF NOT EXISTS output_preview TEXT",
    "ALTER TABLE trace_model_usage ADD COLUMN IF NOT EXISTS function_name VARCHAR(255)",
    "ALTER TABLE trace_model_usage ADD COLUMN IF NOT EXISTS call_sequence INTEGER DEFAULT 0",
]

_ALL_ROUTERS = [
    auth.router, telemetry.router, summary.router, tools.router, models.router,
    alerts.router, costs.router, security.router, alerts_security.router,
    governance.router, organizations.router, projects.router, budgets.router,
    pricing.router, apikeys.router, workers.router, lookups.router,
    ingestion.router, control.router, decorator_router,
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    from sqlalchemy import text
    from app.database import Base, engine
    from app.scheduler import start_scheduler, stop_scheduler

    logger.info("Starting AI Governance API")
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        for stmt in _SAFE_ALTERS:
            try:
                conn.execute(text(stmt))
            except Exception as exc:
                logger.warning("Auto-migration skipped: %s — %s", stmt, exc)
        conn.commit()

    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()
        engine.dispose()
        logger.info("Shutting down AI Governance API")


app = FastAPI(
    title="AI Governance Tool",
    version="2.0.0",
    description="Vendor-agnostic AI governance platform — zero hardcoding, schema-driven.",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router in _ALL_ROUTERS:
    app.include_router(_router)

api_v1 = APIRouter(prefix="/api/v1")
for _router in _ALL_ROUTERS:
    api_v1.include_router(_router)
app.include_router(api_v1)


@app.get("/health")
def health_check():
    return {"status": "healthy", "version": "2.0.0"}
