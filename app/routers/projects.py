from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import (
    Alert, ApiKey, Budget, ConnectorSyncLog, CostBreakdown, DailyOrgSummary,
    DataSecurityLog, ExecutionPipeline, GovernanceRule, MonthlyOrgSummary,
    Project, TelemetryEvent, ToolConnector,
    TraceModelUsage, TraceToolUsage, UsageAnomaly,
)
from app.schemas import ProjectCreate, ProjectResponse
from decorator.models import DecoratorRegistration, ProjectModelUsage, RequestResponseLog, ToolApiInventory

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("/", response_model=list[ProjectResponse])
def list_projects(org_id: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(Project)
    if org_id:
        query = query.filter(Project.org_id == org_id)
    return query.all()


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/", response_model=ProjectResponse)
def create_project(data: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(id=data.id, org_id=data.org_id, project_name=data.project_name, environment=data.environment)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.put("/{project_id}", response_model=ProjectResponse)
def update_project(project_id: str, data: ProjectCreate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project.org_id = data.org_id
    project.project_name = data.project_name
    project.environment = data.environment
    db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}")
def delete_project(project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        connector_ids_subq = (
            db.query(ToolConnector.id).filter(ToolConnector.project_id == project_id).subquery()
        )
        db.query(ConnectorSyncLog).filter(
            ConnectorSyncLog.connector_id.in_(connector_ids_subq)
        ).delete(synchronize_session=False)

        event_ids_subq = (
            db.query(TelemetryEvent.event_id).filter(TelemetryEvent.project_id == project_id).subquery()
        )
        telemetry_ids_subq = (
            db.query(TelemetryEvent.id).filter(TelemetryEvent.project_id == project_id).subquery()
        )

        db.query(DataSecurityLog).filter(DataSecurityLog.project_id == project_id).delete(synchronize_session=False)
        db.query(Alert).filter(Alert.project_id == project_id).delete(synchronize_session=False)
        db.query(Alert).filter(Alert.telemetry_id.in_(telemetry_ids_subq)).delete(synchronize_session=False)
        db.query(CostBreakdown).filter(CostBreakdown.event_id.in_(event_ids_subq)).delete(synchronize_session=False)
        db.query(ExecutionPipeline).filter(ExecutionPipeline.event_id.in_(event_ids_subq)).delete(synchronize_session=False)
        db.query(RequestResponseLog).filter(RequestResponseLog.event_id.in_(event_ids_subq)).delete(synchronize_session=False)

        db.query(TraceModelUsage).filter(TraceModelUsage.project_id == project_id).delete(synchronize_session=False)
        db.query(TraceToolUsage).filter(TraceToolUsage.project_id == project_id).delete(synchronize_session=False)
        db.query(TelemetryEvent).filter(TelemetryEvent.project_id == project_id).delete(synchronize_session=False)

        db.query(UsageAnomaly).filter(UsageAnomaly.project_id == project_id).delete(synchronize_session=False)
        db.query(GovernanceRule).filter(GovernanceRule.project_id == project_id).delete(synchronize_session=False)
        db.query(DailyOrgSummary).filter(DailyOrgSummary.project_id == project_id).delete(synchronize_session=False)
        db.query(MonthlyOrgSummary).filter(MonthlyOrgSummary.project_id == project_id).delete(synchronize_session=False)
        db.query(ToolConnector).filter(ToolConnector.project_id == project_id).delete(synchronize_session=False)
        db.query(DecoratorRegistration).filter(DecoratorRegistration.project_id == project_id).delete(synchronize_session=False)
        db.query(ProjectModelUsage).filter(ProjectModelUsage.project_id == project_id).delete(synchronize_session=False)
        db.query(ToolApiInventory).filter(ToolApiInventory.project_id == project_id).delete(synchronize_session=False)

        db.query(Budget).filter(Budget.project_id == project_id).delete(synchronize_session=False)
        db.query(ApiKey).filter(ApiKey.project_id == project_id).delete(synchronize_session=False)

        db.delete(project)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    return {"detail": "Project deleted", "project_id": project_id}
