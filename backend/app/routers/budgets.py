from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from app.core.deps import get_db
from app.models import Budget
from app.schemas import BudgetCreate, BudgetResponse

router = APIRouter(prefix="/budgets", tags=["budgets"])

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
