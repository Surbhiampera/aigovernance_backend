import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import QueuePool

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

def _build_database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    name = os.getenv("DB_NAME", "ai_governance")
    user = os.getenv("DB_USER", "postgres")
    password = os.getenv("DB_PASSWORD", "")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"

DATABASE_URL = _build_database_url()

from app.config import (
    get_db_connect_timeout,
    get_db_max_overflow,
    get_db_pool_recycle,
    get_db_pool_size,
    get_db_pool_timeout,
)


def _engine_kwargs() -> dict:
    """Keep the app below managed Postgres connection limits.

    Defaults are intentionally conservative. Each Uvicorn worker owns its own
    pool, so total possible DB connections are roughly:

        workers * (DB_POOL_SIZE + DB_MAX_OVERFLOW)
    """
    kwargs = {
        "pool_pre_ping": True,
        "pool_recycle": get_db_pool_recycle(),
    }
    if not DATABASE_URL.startswith("sqlite"):
        kwargs.update(
            {
                "poolclass": QueuePool,
                "pool_size": get_db_pool_size(),
                "max_overflow": get_db_max_overflow(),
                "pool_timeout": get_db_pool_timeout(),
                # Bounds the initial TCP/SSL handshake to Postgres. Without this,
                # a connection attempt that can't complete (e.g. the network
                # blip a fresh container can hit right after a redeploy) hangs
                # on the OS-default socket timeout — which, during
                # app/main.py's lifespan startup, blocks Uvicorn from ever
                # opening the port, so every request 504s until it resolves.
                # A bounded failure here lets startup fail fast and retry
                # instead of hanging indefinitely.
                "connect_args": {"connect_timeout": get_db_connect_timeout()},
            }
        )
    return kwargs


engine = create_engine(DATABASE_URL, **_engine_kwargs())
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
