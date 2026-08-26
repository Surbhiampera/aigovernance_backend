from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import (
    Alert, AiRequest, AiResponse, ApiKey, AuditLog, Budget,
    DailyOrgSummary, GovernanceRule, MonthlyOrgSummary,
    Project, RequestCost, TokenUsage,
)
from app.schemas import ProjectCreate, ProjectResponse
from app.services.model_selection_service import get_model_selection, set_allowed_models

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectResponse])
def list_projects(*, org_id: Optional[str] = Query(None), db: Session = Depends(get_db)):
    q = db.query(Project)
    if org_id:
        q = q.filter(Project.org_id == org_id)
    return q.all()


@router.get("/{project_id}", response_model=ProjectResponse)
def get_project(*, project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("", response_model=ProjectResponse)
def create_project(*, data: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(
        id=data.id,
        org_id=data.org_id,
        project_name=data.project_name,
        environment=data.environment,
    )
    db.add(project)
    # Model selection chosen at creation time (allow-list + default). If
    # omitted, the project inherits the org's selection at request time.
    set_allowed_models(
        db, org_id=data.org_id, project_id=project.id,
        allowed_models=data.allowed_models, default_model=data.default_model,
    )
    db.commit()
    db.refresh(project)
    return project


@router.put("/{project_id}", response_model=ProjectResponse)
def update_project(*, project_id: str, data: ProjectCreate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project.org_id = data.org_id
    project.project_name = data.project_name
    project.environment = data.environment
    set_allowed_models(
        db, org_id=data.org_id, project_id=project.id,
        allowed_models=data.allowed_models, default_model=data.default_model,
    )
    db.commit()
    db.refresh(project)
    return project


class ModelSelectionUpdate(BaseModel):
    allowed_models: Optional[List[str]] = None
    default_model: Optional[str] = None


@router.get("/{project_id}/models")
def get_project_models(*, project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return get_model_selection(db, org_id=project.org_id, project_id=project_id)


@router.put("/{project_id}/models")
def update_project_models(*, project_id: str, data: ModelSelectionUpdate, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    set_allowed_models(
        db, org_id=project.org_id, project_id=project_id,
        allowed_models=data.allowed_models, default_model=data.default_model,
    )
    db.commit()
    return get_model_selection(db, org_id=project.org_id, project_id=project_id)


@router.delete("/{project_id}")
def delete_project(*, project_id: str, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        # Proxy-era tables (cascade by project_id)
        request_ids_subq = (
            db.query(AiRequest.request_id).filter(AiRequest.project_id == project_id).subquery()
        )
        db.query(AiResponse).filter(AiResponse.request_id.in_(request_ids_subq)).delete(synchronize_session=False)
        db.query(TokenUsage).filter(TokenUsage.project_id == project_id).delete(synchronize_session=False)
        db.query(RequestCost).filter(RequestCost.project_id == project_id).delete(synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.project_id == project_id).delete(synchronize_session=False)
        db.query(AiRequest).filter(AiRequest.project_id == project_id).delete(synchronize_session=False)

        # Governance / admin tables
        db.query(Alert).filter(Alert.project_id == project_id).delete(synchronize_session=False)
        db.query(GovernanceRule).filter(GovernanceRule.project_id == project_id).delete(synchronize_session=False)
        db.query(DailyOrgSummary).filter(DailyOrgSummary.project_id == project_id).delete(synchronize_session=False)
        db.query(MonthlyOrgSummary).filter(MonthlyOrgSummary.project_id == project_id).delete(synchronize_session=False)
        db.query(Budget).filter(Budget.project_id == project_id).delete(synchronize_session=False)
        db.query(ApiKey).filter(ApiKey.project_id == project_id).delete(synchronize_session=False)

        db.delete(project)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    return {"detail": "Project deleted", "project_id": project_id}
