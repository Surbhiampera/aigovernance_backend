from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import GovernanceRule
from app.schemas import GovernanceRuleCreate, GovernanceRuleResponse

router = APIRouter(prefix="/governance", tags=["governance"])


@router.get("/rules", response_model=list[GovernanceRuleResponse])
def list_rules(
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(GovernanceRule).filter(GovernanceRule.is_active.is_(True))
    if org_id:
        query = query.filter(GovernanceRule.org_id == org_id)
    return query.order_by(GovernanceRule.created_at.desc()).all()


@router.post("/rules", response_model=GovernanceRuleResponse)
def create_rule(rule_data: GovernanceRuleCreate, db: Session = Depends(get_db)):
    rule = db.query(GovernanceRule).filter(GovernanceRule.rule_name == rule_data.rule_name).first()
    if rule:
        for field, value in rule_data.model_dump().items():
            setattr(rule, field, value)
    else:
        rule = GovernanceRule(**rule_data.model_dump())
        db.add(rule)
    db.commit()
    db.refresh(rule)
    return rule
