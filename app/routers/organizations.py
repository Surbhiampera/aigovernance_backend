from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import (
    Alert, AiRequest, AiResponse, ApiKey, AuditLog, Budget,
    DailyOrgSummary, GovernanceRule, MonthlyOrgSummary,
    Organization, Project, RateLimit, RequestCost, TokenUsage, User,
)
from app.schemas import OrganizationCreate, OrganizationResponse, OrganizationUpdate

router = APIRouter(prefix="/organizations", tags=["organizations"])


@router.get("/", response_model=list[OrganizationResponse])
def list_organizations(*, db: Session = Depends(get_db)):
    return db.query(Organization).all()


@router.get("/{org_id}", response_model=OrganizationResponse)
def get_organization(*, org_id: str, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


DEFAULT_ORG_RATE_LIMIT_RPM = 1200


@router.post("/", response_model=OrganizationResponse)
def create_organization(*, data: OrganizationCreate, db: Session = Depends(get_db)):
    org = Organization(id=data.id, org_name=data.org_name, plan_type=data.plan_type)
    db.add(org)
    # Org-level default (project_id=NULL) so every project under this org,
    # including ones created later, is covered without a separate row.
    db.add(RateLimit(org_id=org.id, max_requests_per_min=DEFAULT_ORG_RATE_LIMIT_RPM))
    db.commit()
    db.refresh(org)
    return org


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
