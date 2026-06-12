from datetime import datetime
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import Budget, RequestCost
from app.schemas import BudgetCreate, BudgetResponse, BudgetUtilizationResponse

router = APIRouter(prefix="/budgets", tags=["budgets"])

@router.get("/utilization", response_model=list[BudgetUtilizationResponse])
def get_budget_utilization(
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    today = datetime.utcnow()
    month_start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    query = db.query(Budget)
    if org_id:
        query = query.filter(Budget.org_id == org_id)
    budgets = query.all()

    results = []
    for budget in budgets:
        spend_query = db.query(
            func.coalesce(func.sum(RequestCost.total_cost), Decimal("0"))
        ).filter(
            RequestCost.org_id == budget.org_id,
            RequestCost.created_at >= month_start,
        )
        if budget.project_id:
            spend_query = spend_query.filter(RequestCost.project_id == budget.project_id)
        current_spend = float(spend_query.scalar() or 0)

        limit = float(budget.limit_amount) if budget.limit_amount else None
        threshold_pct = budget.alert_threshold_percent or 80

        if limit and limit > 0:
            utilization_pct = round((current_spend / limit) * 100, 1)
            if utilization_pct >= 100:
                status = "exceeded"
            elif utilization_pct >= threshold_pct:
                status = "warning"
            else:
                status = "ok"
        else:
            utilization_pct = 0.0
            status = "no_budget"

        results.append(BudgetUtilizationResponse(
            org_id=budget.org_id,
            project_id=budget.project_id,
            budget_type=budget.budget_type,
            limit_amount=limit,
            current_spend=current_spend,
            alert_threshold_percent=threshold_pct,
            utilization_percent=utilization_pct,
            status=status,
        ))

    return results


@router.get("/", response_model=list[BudgetResponse])
def list_budgets(org_id: Optional[str] = Query(None), db: Session = Depends(get_db)):
    query = db.query(Budget)
    if org_id:
        query = query.filter(Budget.org_id == org_id)
    return query.all()

@router.get("/{budget_id}", response_model=BudgetResponse)
def get_budget(budget_id: int, db: Session = Depends(get_db)):
    budget = db.query(Budget).filter(Budget.id == budget_id).first()
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    return budget

@router.post("/", response_model=BudgetResponse)
def create_budget(data: BudgetCreate, db: Session = Depends(get_db)):
    budget = Budget(
        org_id=data.org_id,
        project_id=data.project_id,
        budget_type=data.budget_type,
        limit_amount=data.limit_amount,
        alert_threshold_percent=data.alert_threshold_percent,
    )
    db.add(budget)
    db.commit()
    db.refresh(budget)
    return budget

@router.put("/{budget_id}", response_model=BudgetResponse)
def update_budget(budget_id: int, data: BudgetCreate, db: Session = Depends(get_db)):
    budget = db.query(Budget).filter(Budget.id == budget_id).first()
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    budget.org_id = data.org_id
    budget.project_id = data.project_id
    budget.budget_type = data.budget_type
    budget.limit_amount = data.limit_amount
    budget.alert_threshold_percent = data.alert_threshold_percent
    db.commit()
    db.refresh(budget)
    return budget

@router.delete("/{budget_id}")
def delete_budget(budget_id: int, db: Session = Depends(get_db)):
    budget = db.query(Budget).filter(Budget.id == budget_id).first()
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    db.delete(budget)
    db.commit()
    return {"detail": "Budget deleted"}
