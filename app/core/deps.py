import os
from typing import Generator

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.database import SessionLocal

# Recognized role values in ascending privilege order
ROLES = ("viewer", "security_reviewer", "admin")


def get_db() -> Generator:
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
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
        return {"key_name": "master", "org_id": None, "role": "admin"}

    from app.models import ApiKey
    key_record = db.query(ApiKey).filter(ApiKey.id == x_api_key).first()
    if not key_record:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key")

    return key_record


def require_role(*allowed_roles: str):
    """Dependency factory: enforce one of *allowed_roles* on the calling API key.

    Master key is always treated as 'admin'.  Keys with role=None default to
    'viewer'.  Usage::

        @router.get("/sensitive")
        def endpoint(_key=Depends(require_role("admin", "security_reviewer"))):
            ...
    """
    def _check(key=Depends(require_api_key)):
        role = key.get("role") if isinstance(key, dict) else getattr(key, "role", None)
        effective_role = role or "viewer"
        if effective_role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"Role '{effective_role}' is not permitted for this endpoint. "
                       f"Required: {list(allowed_roles)}",
            )
        return key
    return _check
