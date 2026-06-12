from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import RateLimit
from app.schemas import RateLimitResponse

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
