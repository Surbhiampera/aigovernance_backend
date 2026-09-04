import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import PiiPolicy
from app.schemas import PiiPolicyCreate, PiiPolicyResponse, PiiPolicyUpdate

router = APIRouter(prefix="/pii-policies", tags=["pii-policies"])


@router.get("/", response_model=list[PiiPolicyResponse])
def list_pii_policies(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(PiiPolicy)
    if org_id:
        query = query.filter(PiiPolicy.org_id == org_id)
    if project_id:
        query = query.filter(PiiPolicy.project_id == project_id)
    return query.order_by(PiiPolicy.priority.desc()).all()


@router.get("/{policy_id}", response_model=PiiPolicyResponse)
def get_pii_policy(policy_id: str, db: Session = Depends(get_db)):
    policy = db.query(PiiPolicy).filter(PiiPolicy.policy_id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="PII policy not found")
    return policy


@router.post("/", response_model=PiiPolicyResponse)
def create_pii_policy(data: PiiPolicyCreate, db: Session = Depends(get_db)):
    policy = PiiPolicy(
        policy_id=f"pii-{uuid.uuid4().hex[:20]}",
        org_id=data.org_id,
        project_id=data.project_id,
        pii_type=data.pii_type,
        risk_level=data.risk_level,
        action=data.action,
        mask_pattern=data.mask_pattern,
        log_detection=data.log_detection,
        priority=data.priority,
        description=data.description,
        is_active=data.is_active,
    )
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return policy


@router.put("/{policy_id}", response_model=PiiPolicyResponse)
def update_pii_policy(policy_id: str, data: PiiPolicyUpdate, db: Session = Depends(get_db)):
    policy = db.query(PiiPolicy).filter(PiiPolicy.policy_id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="PII policy not found")

    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(policy, field, value)

    db.commit()
    db.refresh(policy)
    return policy


@router.delete("/{policy_id}")
def delete_pii_policy(policy_id: str, db: Session = Depends(get_db)):
    policy = db.query(PiiPolicy).filter(PiiPolicy.policy_id == policy_id).first()
    if not policy:
        raise HTTPException(status_code=404, detail="PII policy not found")
    db.delete(policy)
    db.commit()
    return {"detail": "PII policy deleted"}
