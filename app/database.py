import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import QueuePool

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://localhost:5432/ai_governance")

from app.config import (
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
            }
        )
    return kwargs


engine = create_engine(DATABASE_URL, **_engine_kwargs())
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
