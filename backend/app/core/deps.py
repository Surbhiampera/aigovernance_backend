import logging
import os
from typing import Generator

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal

_logger = logging.getLogger(__name__)


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_api_key(
    x_api_key: str = Header(default=None),
    db: Session = Depends(get_db),
):
    """Validate X-API-Key against api_keys table or GOVERNANCE_MASTER_KEY env var.

    Set GOVERNANCE_MASTER_KEY on the server for bootstrap / admin access before
    any per-org keys are created.  All SDK clients must pass their key as the
    X-API-Key request header.
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header is required")

    master = os.getenv("GOVERNANCE_MASTER_KEY", "")
    if master and x_api_key == master:
        return {"key_name": "master", "org_id": None}

    from app.models import ApiKey
    key_record = db.query(ApiKey).filter(ApiKey.id == x_api_key).first()
    if not key_record:
        _logger.warning("Rejected request: unknown API key (prefix=%s...)", x_api_key[:8])
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    return key_record
