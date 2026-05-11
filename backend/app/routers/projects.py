from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.core.deps import get_db
from app.models import Project
from app.schemas import ProjectCreate, ProjectResponse

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
    db.delete(project)
    db.commit()
    return {"detail": "Project deleted"}
