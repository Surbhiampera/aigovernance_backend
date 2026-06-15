from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import RateLimit
from app.schemas import RateLimitCreate, RateLimitResponse

router = APIRouter(prefix="/rate-limits", tags=["rate-limits"])


@router.get("/", response_model=list[RateLimitResponse])
def list_rate_limits(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(RateLimit)
    if org_id:
        query = query.filter(RateLimit.org_id == org_id)
    if project_id:
        query = query.filter(RateLimit.project_id == project_id)
    return query.order_by(RateLimit.created_at.desc()).all()


@router.post("/", response_model=RateLimitResponse)
def create_rate_limit(data: RateLimitCreate, db: Session = Depends(get_db)):
    rl = RateLimit(
        org_id=data.org_id,
        project_id=data.project_id,
        key_id=data.key_id,
        max_requests_per_min=data.max_requests_per_min,
        max_tokens_per_day=data.max_tokens_per_day,
    )
    db.add(rl)
    db.commit()
    db.refresh(rl)
    return rl


@router.put("/{rate_limit_id}", response_model=RateLimitResponse)
def update_rate_limit(rate_limit_id: int, data: RateLimitCreate, db: Session = Depends(get_db)):
    rl = db.query(RateLimit).filter(RateLimit.id == rate_limit_id).first()
    if not rl:
        raise HTTPException(status_code=404, detail="Rate limit not found")
    rl.org_id = data.org_id
    rl.project_id = data.project_id
    rl.key_id = data.key_id
    rl.max_requests_per_min = data.max_requests_per_min
    rl.max_tokens_per_day = data.max_tokens_per_day
    db.commit()
    db.refresh(rl)
    return rl


@router.delete("/{rate_limit_id}")
def delete_rate_limit(rate_limit_id: int, db: Session = Depends(get_db)):
    rl = db.query(RateLimit).filter(RateLimit.id == rate_limit_id).first()
    if not rl:
        raise HTTPException(status_code=404, detail="Rate limit not found")
    db.delete(rl)
    db.commit()
    return {"detail": "Rate limit deleted"}
