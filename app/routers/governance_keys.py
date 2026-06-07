"""HTTP endpoints for governance key management.

Governance keys are issued to external teams. They are stored hashed —
the raw value is returned only once at creation time.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.services.governance_key_service import (
    create_governance_key,
    list_governance_keys,
    revoke_governance_key,
)

router = APIRouter(prefix="/governance-keys", tags=["governance-keys"])


class GovernanceKeyCreate(BaseModel):
    org_id: str
    project_id: Optional[str] = None
    key_name: str
    expires_at: Optional[datetime] = None


@router.post("/")
def create_key(data: GovernanceKeyCreate, db: Session = Depends(get_db)):
    """Create a new governance key. Raw key is returned once — store it securely."""
    from app.models import Organization, Project

    org = db.query(Organization).filter(Organization.id == data.org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail=f"Organisation '{data.org_id}' not found.")

    if data.project_id:
        proj = db.query(Project).filter(
            Project.id == data.project_id,
            Project.org_id == data.org_id,
        ).first()
        if not proj:
            raise HTTPException(status_code=404, detail=f"Project '{data.project_id}' not found in org '{data.org_id}'.")

    result = create_governance_key(
        db,
        org_id=data.org_id,
        project_id=data.project_id,
        key_name=data.key_name,
        expires_at=data.expires_at,
    )
    db.commit()
    return result


@router.get("/")
def list_keys(org_id: str = Query(...), db: Session = Depends(get_db)):
    """List all governance keys for an organisation (raw keys not included)."""
    return list_governance_keys(db, org_id=org_id)


@router.delete("/{key_id}")
def revoke_key(key_id: str, db: Session = Depends(get_db)):
    """Revoke (deactivate) a governance key."""
    ok = revoke_governance_key(db, key_id=key_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Governance key not found.")
    db.commit()
    return {"detail": "Key revoked."}
