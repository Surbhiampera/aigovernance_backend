from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import (
    Alert, AiRequest, AiResponse, ApiKey, AuditLog, Budget,
    DailyOrgSummary, GovernanceRule, ModelDeployment, MonthlyOrgSummary,
    Organization, Project, RateLimit, RequestCost, TokenUsage, User,
)
from app.schemas import OrganizationCreate, OrganizationResponse, OrganizationUpdate
from app.services.deployment_service import provision_standard_deployments
from app.services.model_selection_service import get_model_selection, set_allowed_models

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("", response_model=list[OrganizationResponse])
def list_organizations(*, db: Session = Depends(get_db)):
    return db.query(Organization).all()


@router.get("/{org_id}", response_model=OrganizationResponse)
def get_organization(*, org_id: str, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


DEFAULT_ORG_RATE_LIMIT_RPM = 1200


@router.post("", response_model=OrganizationResponse)
def create_organization(*, data: OrganizationCreate, db: Session = Depends(get_db)):
    org = Organization(id=data.id, org_name=data.org_name, plan_type=data.plan_type)
    db.add(org)
    # Org-level default (project_id=NULL) so every project under this org,
    # including ones created later, is covered without a separate row.
    db.add(RateLimit(org_id=org.id, max_requests_per_min=DEFAULT_ORG_RATE_LIMIT_RPM))
    # Auto-provision the standard model deployments (see STANDARD_MODEL_DEPLOYMENTS
    # in config.py) so every org gets all configured models without a manual
    # POST /deployments call per model.
    provision_standard_deployments(db, org_id=org.id)
    # Model selection chosen at creation time (allow-list + default), enforced
    # by the proxy on every request for this org's projects.
    set_allowed_models(
        db, org_id=org.id, project_id=None,
        allowed_models=data.allowed_models, default_model=data.default_model,
    )
    db.commit()
    db.refresh(org)
    return org


class ModelSelectionUpdate(BaseModel):
    allowed_models: Optional[List[str]] = None
    default_model: Optional[str] = None


@router.get("/{org_id}/models")
def get_organization_models(*, org_id: str, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return get_model_selection(db, org_id=org_id, project_id=None)


@router.put("/{org_id}/models")
def update_organization_models(*, org_id: str, data: ModelSelectionUpdate, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    set_allowed_models(
        db, org_id=org_id, project_id=None,
        allowed_models=data.allowed_models, default_model=data.default_model,
    )
    db.commit()
    return get_model_selection(db, org_id=org_id, project_id=None)


@router.post("/{org_id}/provision-deployments")
def provision_deployments_for_org(*, org_id: str, db: Session = Depends(get_db)):
    """Backfill the standard model deployments for an org created before
    STANDARD_MODEL_DEPLOYMENTS auto-provisioning existed."""
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    created = provision_standard_deployments(db, org_id=org_id)
    db.commit()
    return {"org_id": org_id, "deployments_created": created}


@router.put("/{org_id}", response_model=OrganizationResponse)
def update_organization(*, org_id: str, data: OrganizationUpdate, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    org.org_name = data.org_name
    org.plan_type = data.plan_type
    org.budget_limit = data.budget_limit
    db.commit()
    db.refresh(org)
    return org


@router.delete("/{org_id}")
def delete_organization(*, org_id: str, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    try:
        # Proxy-era tables (cascade by org_id)
        request_ids_subq = (
            db.query(AiRequest.request_id).filter(AiRequest.org_id == org_id).subquery()
        )
        db.query(AiResponse).filter(AiResponse.request_id.in_(request_ids_subq)).delete(synchronize_session=False)
        db.query(TokenUsage).filter(TokenUsage.org_id == org_id).delete(synchronize_session=False)
        db.query(RequestCost).filter(RequestCost.org_id == org_id).delete(synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.org_id == org_id).delete(synchronize_session=False)
        db.query(AiRequest).filter(AiRequest.org_id == org_id).delete(synchronize_session=False)

        # Governance / admin tables
        db.query(Alert).filter(Alert.org_id == org_id).delete(synchronize_session=False)
        db.query(GovernanceRule).filter(GovernanceRule.org_id == org_id).delete(synchronize_session=False)
        db.query(DailyOrgSummary).filter(DailyOrgSummary.org_id == org_id).delete(synchronize_session=False)
        db.query(MonthlyOrgSummary).filter(MonthlyOrgSummary.org_id == org_id).delete(synchronize_session=False)
        db.query(RateLimit).filter(RateLimit.org_id == org_id).delete(synchronize_session=False)
        db.query(ModelDeployment).filter(ModelDeployment.org_id == org_id).delete(synchronize_session=False)
        db.query(Budget).filter(Budget.org_id == org_id).delete(synchronize_session=False)
        db.query(ApiKey).filter(ApiKey.org_id == org_id).delete(synchronize_session=False)
        db.query(User).filter(User.org_id == org_id).delete(synchronize_session=False)
        db.query(Project).filter(Project.org_id == org_id).delete(synchronize_session=False)

        db.delete(org)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    return {"detail": "Organization deleted", "org_id": org_id}
