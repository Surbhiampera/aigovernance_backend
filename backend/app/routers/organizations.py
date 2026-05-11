from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.core.deps import get_db
from app.models import Organization
from app.schemas import OrganizationCreate, OrganizationResponse

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
def delete_organization(org_id: str, db: Session = Depends(get_db)):
    org = db.query(Organization).filter(Organization.id == org_id).first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    db.delete(org)
    db.commit()
    return {"detail": "Organization deleted"}
