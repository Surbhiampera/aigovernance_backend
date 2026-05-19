from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session
from app.core.deps import get_db
from app.models import ApiKey, Project
from app.schemas import ProjectCreate, ProjectResponse

router = APIRouter(prefix="/projects", tags=["projects"])

# Non-FK tables that carry project_id and should be cleaned up on cascade delete.
_PROJECT_CHILD_TABLES = [
    "telemetry_events", "cost_breakdown", "data_security_logs", "alerts",
    "usage_anomalies", "governance_rules", "trace_model_usage", "trace_tool_usage",
    "execution_pipeline", "daily_org_summary", "monthly_org_summary",
    "rate_limit_violations", "tool_connectors",
]

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
def delete_project(
    project_id: str,
    cascade: bool = Query(True, description="Also delete child api_keys/telemetry rows"),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    try:
        if cascade:
            # Clear FK-bound children first (no ON DELETE CASCADE in schema).
            db.query(ApiKey).filter(ApiKey.project_id == project_id).delete(synchronize_session=False)

            # Best-effort cleanup of non-FK tables that carry project_id.
            for tbl in _PROJECT_CHILD_TABLES:
                try:
                    db.execute(text(f"DELETE FROM {tbl} WHERE project_id = :pid"), {"pid": project_id})
                except SQLAlchemyError:
                    db.rollback()  # ignore missing table / missing column

        db.delete(project)
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete project — related records still exist: {exc.orig}",
        )
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error while deleting: {exc}")

    return {"detail": "Project deleted", "project_id": project_id, "cascade": cascade}
