"""Governance API key management.

Creates, verifies, and revokes governance keys issued to external teams.
Keys are stored hashed (SHA-256) in api_keys.hashed_key — the raw value
is only returned once at creation time.
"""

import hashlib
import secrets
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session


def _generate_raw_key() -> str:
    """Generate a 40-char URL-safe governance key with a recognisable prefix."""
    return "gov-" + secrets.token_urlsafe(30)


def _hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _hint(raw: str) -> str:
    return raw[:7] + "..." + raw[-4:]


def create_governance_key(
    db: Session,
    *,
    org_id: str,
    project_id: Optional[str] = None,
    key_name: str,
    created_by: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> dict:
    """Create a new governance key. Returns dict with raw_key (shown once only)."""
    from app.models import ApiKey

    raw = _generate_raw_key()
    key_id = f"gk-{uuid.uuid4().hex[:16]}"

    row = ApiKey(
        id=key_id,
        org_id=org_id,
        project_id=project_id,
        key_name=key_name,
        provider="governance",
        hashed_key=_hash_key(raw),
        raw_key_hint=_hint(raw),
        is_proxy_key=True,
        is_active=True,
        expires_at=expires_at,
    )
    db.add(row)
    db.flush()

    return {
        "key_id":        key_id,
        "raw_key":       raw,           # shown ONCE — not stored
        "raw_key_hint":  _hint(raw),
        "org_id":        org_id,
        "project_id":    project_id,
        "key_name":      key_name,
        "expires_at":    expires_at,
        "created_at":    datetime.utcnow().isoformat(),
    }


def verify_governance_key(db: Session, raw_key: str) -> Optional[dict]:
    """Verify a raw governance key. Returns {org_id, project_id, key_id} or None."""
    from app.models import ApiKey

    hashed = _hash_key(raw_key)
    row = (
        db.query(ApiKey)
        .filter(
            ApiKey.hashed_key == hashed,
            ApiKey.is_proxy_key == True,
            ApiKey.is_active == True,
        )
        .first()
    )
    if row is None:
        return None
    if row.expires_at and row.expires_at < datetime.utcnow():
        return None

    # Update last_used_at
    row.last_used_at = datetime.utcnow()
    db.flush()

    return {
        "key_id":     row.id,
        "org_id":     row.org_id,
        "project_id": row.project_id,
        "key_name":   row.key_name,
    }


def revoke_governance_key(db: Session, key_id: str) -> bool:
    from app.models import ApiKey

    row = db.query(ApiKey).filter(ApiKey.id == key_id, ApiKey.is_proxy_key == True).first()
    if row is None:
        return False
    row.is_active = False
    db.flush()
    return True


def list_governance_keys(db: Session, org_id: str) -> list[dict]:
    from app.models import ApiKey

    rows = (
        db.query(ApiKey)
        .filter(ApiKey.org_id == org_id, ApiKey.is_proxy_key == True)
        .order_by(ApiKey.created_at.desc())
        .all()
    )
    return [
        {
            "key_id":       r.id,
            "key_name":     r.key_name,
            "raw_key_hint": r.raw_key_hint,
            "project_id":   r.project_id,
            "is_active":    r.is_active,
            "last_used_at": r.last_used_at.isoformat() if r.last_used_at else None,
            "expires_at":   r.expires_at.isoformat() if r.expires_at else None,
            "created_at":   r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
