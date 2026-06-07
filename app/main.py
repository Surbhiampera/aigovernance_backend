from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_cors_origins
from app.routers import (
    alerts,
    apikeys,
    audit_logs,
    auth,
    budgets,
    costs,
    governance,
    lookups,
    models,
    organizations,
    pricing,
    projects,
    summary,
)
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
]

_ALL_ROUTERS = [
    auth.router,
    summary.router,
    models.router,
    alerts.router,
    costs.router,
    governance.router,
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

for _router in _ALL_ROUTERS:
    app.include_router(_router)

# Proxy registered at root — stable URL for external teams' SDK base_url.
app.include_router(proxy_router)


@app.get("/health")
def health_check():
    return {"status": "healthy", "version": "3.0.0"}
