from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import (
    Alert, ApiKey, Budget, ConnectorSyncLog, CostBreakdown, DailyOrgSummary,
    DataSecurityLog, ExecutionPipeline, GovernanceRule, MonthlyOrgSummary,
    Organization, Project, RateLimit, TelemetryEvent, ToolConnector,
    TraceModelUsage, TraceToolUsage, UsageAnomaly, User,
)
from app.schemas import OrganizationCreate, OrganizationUpdate, OrganizationResponse
from app.models import DecoratorRegistration, ProjectModelUsage, RequestResponseLog, ToolApiInventory

router = APIRouter(prefix="/organizations", tags=["organizations"])


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
    org = Organization(id=data.id, org_name=data.org_name, plan_type=data.plan_type)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


@router.put("/{org_id}", response_model=OrganizationResponse)
def update_organization(org_id: str, data: OrganizationUpdate, db: Session = Depends(get_db)):
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
def delete_organization(org_id: str, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    try:
        connector_ids_subq = (
            db.query(ToolConnector.id).filter(ToolConnector.org_id == org_id).subquery()
        )
        db.query(ConnectorSyncLog).filter(
            ConnectorSyncLog.connector_id.in_(connector_ids_subq)
        ).delete(synchronize_session=False)

        event_ids_subq = (
            db.query(TelemetryEvent.event_id).filter(TelemetryEvent.org_id == org_id).subquery()
        )
        telemetry_ids_subq = (
            db.query(TelemetryEvent.id).filter(TelemetryEvent.org_id == org_id).subquery()
        )

        db.query(DataSecurityLog).filter(DataSecurityLog.org_id == org_id).delete(synchronize_session=False)
        db.query(Alert).filter(Alert.org_id == org_id).delete(synchronize_session=False)
        db.query(Alert).filter(Alert.telemetry_id.in_(telemetry_ids_subq)).delete(synchronize_session=False)
        db.query(CostBreakdown).filter(CostBreakdown.event_id.in_(event_ids_subq)).delete(synchronize_session=False)
        db.query(ExecutionPipeline).filter(ExecutionPipeline.event_id.in_(event_ids_subq)).delete(synchronize_session=False)
        db.query(RequestResponseLog).filter(RequestResponseLog.event_id.in_(event_ids_subq)).delete(synchronize_session=False)

        db.query(TraceModelUsage).filter(TraceModelUsage.org_id == org_id).delete(synchronize_session=False)
        db.query(TraceToolUsage).filter(TraceToolUsage.org_id == org_id).delete(synchronize_session=False)
        db.query(TelemetryEvent).filter(TelemetryEvent.org_id == org_id).delete(synchronize_session=False)

        db.query(UsageAnomaly).filter(UsageAnomaly.org_id == org_id).delete(synchronize_session=False)
        db.query(GovernanceRule).filter(GovernanceRule.org_id == org_id).delete(synchronize_session=False)
        db.query(DailyOrgSummary).filter(DailyOrgSummary.org_id == org_id).delete(synchronize_session=False)
        db.query(MonthlyOrgSummary).filter(MonthlyOrgSummary.org_id == org_id).delete(synchronize_session=False)
        db.query(RateLimit).filter(RateLimit.org_id == org_id).delete(synchronize_session=False)
        db.query(ToolConnector).filter(ToolConnector.org_id == org_id).delete(synchronize_session=False)
        db.query(DecoratorRegistration).filter(DecoratorRegistration.org_id == org_id).delete(synchronize_session=False)
        db.query(ProjectModelUsage).filter(ProjectModelUsage.org_id == org_id).delete(synchronize_session=False)
        db.query(ToolApiInventory).filter(ToolApiInventory.org_id == org_id).delete(synchronize_session=False)

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
