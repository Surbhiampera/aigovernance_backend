from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from app.core.deps import get_db
from app.models import ApiKey, Organization, Project, User
from app.schemas import OrganizationCreate, OrganizationResponse

router = APIRouter(prefix="/organizations", tags=["organizations"])

# Non-FK tables that carry org_id and should be cleaned up on delete.
# (FK tables — projects, users, api_keys, rate_limits — are handled via ORM below.)
_ORG_CHILD_TABLES = [
    "telemetry_events",
    "cost_breakdown",
    "data_security_logs",
    "alerts",
    "usage_anomalies",
    "governance_rules",
    "trace_model_usage",
    "trace_tool_usage",
    "execution_pipeline",
    "daily_org_summary",
    "monthly_org_summary",
    "rate_limit_violations",
    "tool_connectors",
]


@router.get("/", response_model=list[OrganizationResponse])
def list_organizations(db: Session = Depends(get_db)):
    return db.query(Organization).all()


@router.get("/{org_id}", response_model=OrganizationResponse)
def get_organization(org_id: str, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


@router.post("/", response_model=OrganizationResponse)
def create_organization(data: OrganizationCreate, db: Session = Depends(get_db)):
    org = Organization(id=data.id, org_name=data.org_name, plan_type=data.plan_type, budget_limit=data.budget_limit)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


@router.put("/{org_id}", response_model=OrganizationResponse)
def update_organization(org_id: str, data: OrganizationCreate, db: Session = Depends(get_db)):
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
def delete_organization(
    org_id: str,
    cascade: bool = Query(True, description="Also delete child projects/users/api_keys/telemetry"),
    db: Session = Depends(get_db),
):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    try:
        if cascade:
            # Clear FK-bound children first (no ON DELETE CASCADE in schema).
            db.query(ApiKey).filter(ApiKey.org_id == org_id).delete(synchronize_session=False)
            db.query(User).filter(User.org_id == org_id).delete(synchronize_session=False)
            db.query(Project).filter(Project.org_id == org_id).delete(synchronize_session=False)

            # Best-effort cleanup of non-FK tables that carry org_id.
            for tbl in _ORG_CHILD_TABLES:
                try:
                    db.execute(text(f"DELETE FROM {tbl} WHERE org_id = :oid"), {"oid": org_id})
                except SQLAlchemyError:
                    db.rollback()  # ignore missing table / missing column

        db.delete(org)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete organization — related records still exist: {exc.orig}",
        )
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error while deleting: {exc}")

    return {"detail": "Organization deleted", "org_id": org_id, "cascade": cascade}
