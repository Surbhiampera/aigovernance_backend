"""Admin endpoints for registering and managing AI deployments.

Access requires X-API-Key (admin / GOVERNANCE_MASTER_KEY).
External teams never touch these endpoints — they only use GOVERNANCE_KEY.

POST   /deployments          — register a new deployment for an org/project
GET    /deployments          — list deployments (api_key redacted)
GET    /deployments/{id}     — get one deployment (api_key redacted)
PATCH  /deployments/{id}     — update (rotate key, change endpoint, etc.)
DELETE /deployments/{id}     — deactivate
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_api_key

router = APIRouter(prefix="/deployments", tags=["deployments"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class DeploymentCreate(BaseModel):
    org_id: str
    project_id: Optional[str] = None
    provider: str                         # azure_openai | openai | anthropic | …
    model_name: str                       # canonical name used for pricing (gpt-4o, gpt-5-nano)
    deployment_name: Optional[str] = None # provider-specific resource name (Azure: deployment ID)
    endpoint_url: str                     # provider base URL
    api_key: str                          # server-side credential
    api_version: Optional[str] = None     # 2025-01-01-preview etc. (Azure)
    is_default: bool = False              # set True to auto-select for this org/project


class DeploymentUpdate(BaseModel):
    deployment_name: Optional[str] = None
    endpoint_url: Optional[str] = None
    api_key: Optional[str] = None
    api_version: Optional[str] = None
    is_default: Optional[bool] = None
    is_active: Optional[bool] = None


def _safe_row(row) -> dict:
    return {
        "deployment_id":   row.deployment_id,
        "org_id":          row.org_id,
        "project_id":      row.project_id,
        "provider":        row.provider,
        "model_name":      row.model_name,
        "deployment_name": row.deployment_name,
        "endpoint_url":    row.endpoint_url,
        "api_key":         f"***{(row.api_key or '')[-4:]}",  # last 4 chars only
        "api_version":     getattr(row, "api_version", None),
        "is_default":      getattr(row, "is_default", False),
        "is_active":       row.is_active,
        "created_at":      row.created_at.isoformat() if row.created_at else None,
        "updated_at":      row.updated_at.isoformat() if row.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("", status_code=201, dependencies=[Depends(require_api_key)])
def create_deployment(body: DeploymentCreate, db: Session = Depends(get_db)):
    from app.models import ModelDeployment

    row = ModelDeployment(
        deployment_id=f"depl-{uuid.uuid4().hex[:20]}",
        org_id=body.org_id,
        project_id=body.project_id,
        provider=body.provider,
        model_name=body.model_name,
        deployment_name=body.deployment_name or body.model_name,
        endpoint_url=body.endpoint_url,
        api_key=body.api_key,
        api_version=body.api_version,
        is_default=body.is_default,
        is_active=True,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _safe_row(row)


@router.get("", dependencies=[Depends(require_api_key)])
def list_deployments(
    org_id: Optional[str] = None,
    project_id: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from app.models import ModelDeployment

    q = db.query(ModelDeployment)
    if org_id:
        q = q.filter(ModelDeployment.org_id == org_id)
    if project_id:
        q = q.filter(ModelDeployment.project_id == project_id)
    return [_safe_row(r) for r in q.order_by(ModelDeployment.created_at.desc()).all()]


@router.get("/{deployment_id}", dependencies=[Depends(require_api_key)])
def get_deployment(deployment_id: str, db: Session = Depends(get_db)):
    from app.models import ModelDeployment

    row = db.query(ModelDeployment).filter(
        ModelDeployment.deployment_id == deployment_id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Deployment not found.")
    return _safe_row(row)


@router.patch("/{deployment_id}", dependencies=[Depends(require_api_key)])
def update_deployment(
    deployment_id: str,
    body: DeploymentUpdate,
    db: Session = Depends(get_db),
):
    from app.models import ModelDeployment

    row = db.query(ModelDeployment).filter(
        ModelDeployment.deployment_id == deployment_id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Deployment not found.")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(row, field, value)
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _safe_row(row)


@router.delete("/{deployment_id}", dependencies=[Depends(require_api_key)])
def delete_deployment(deployment_id: str, db: Session = Depends(get_db)):
    from app.models import ModelDeployment

    row = db.query(ModelDeployment).filter(
        ModelDeployment.deployment_id == deployment_id
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Deployment not found.")
    row.is_active = False
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"detail": "Deployment deactivated.", "deployment_id": deployment_id}
