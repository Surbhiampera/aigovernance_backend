"""Cost analytics endpoints — all data sourced from proxy-generated tables.

AiRequest  : one row per proxy request (input tokens, model, org, project)
RequestCost: one row per proxy request (input_cost, output_cost, total_cost)
TokenUsage : one row per proxy request (input/output/total token counts)
"""
from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import AiRequest, MonthlyOrgSummary, RequestCost, TokenUsage

router = APIRouter(prefix="/costs", tags=["costs"])


def _date_filter(query, model, *, start: Optional[date], end: Optional[date]):
    if start:
        query = query.filter(func.date(model.created_at) >= start)
    if end:
        query = query.filter(func.date(model.created_at) <= end)
    return query


def _org_filter(query, model, *, org_id: Optional[str]):
    if org_id:
        query = query.filter(model.org_id == org_id)
    return query


def _project_filter(query, model, *, project_id: Optional[str]):
    if project_id and hasattr(model, "project_id"):
        query = query.filter(model.project_id == project_id)
    return query


# ---------------------------------------------------------------------------
# Cost breakdown by model
# ---------------------------------------------------------------------------

@router.get("/by-model")
def cost_by_model(
    *,
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    db: Session = Depends(get_db),
) -> list[dict]:
    q = db.query(
        RequestCost.model_name,
        func.count(RequestCost.request_id).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("input_tokens"),
        func.sum(RequestCost.output_tokens).label("output_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.input_token_cost).label("input_token_cost"),
        func.sum(RequestCost.output_token_cost).label("output_token_cost"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    )
    q = _org_filter(q, RequestCost, org_id=org_id)
    q = _project_filter(q, RequestCost, project_id=project_id)
    q = _date_filter(q, RequestCost, start=start, end=end)
    rows = q.group_by(RequestCost.model_name).order_by(func.sum(RequestCost.total_cost).desc()).all()

    return [
        {
            "model_name": r.model_name,
            "total_requests": r.total_requests or 0,
            "input_tokens": r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "input_cost": float(r.input_token_cost or 0),
            "output_cost": float(r.output_token_cost or 0),
            "total_cost": float(r.total_cost or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Cost breakdown by project
# ---------------------------------------------------------------------------

@router.get("/by-project")
def cost_by_project(
    *,
    org_id: Optional[str] = Query(None),
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    db: Session = Depends(get_db),
) -> list[dict]:
    q = db.query(
        RequestCost.org_id,
        RequestCost.project_id,
        func.count(RequestCost.request_id).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("input_tokens"),
        func.sum(RequestCost.output_tokens).label("output_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    )
    q = _org_filter(q, RequestCost, org_id=org_id)
    q = _date_filter(q, RequestCost, start=start, end=end)
    rows = (
        q.group_by(RequestCost.org_id, RequestCost.project_id)
        .order_by(func.sum(RequestCost.total_cost).desc())
        .all()
    )

    return [
        {
            "org_id": r.org_id,
            "project_id": r.project_id,
            "total_requests": r.total_requests or 0,
            "input_tokens": r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "total_cost": float(r.total_cost or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Cost breakdown by organisation
# ---------------------------------------------------------------------------

@router.get("/by-org")
def cost_by_org(
    *,
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    db: Session = Depends(get_db),
) -> list[dict]:
    q = db.query(
        RequestCost.org_id,
        func.count(RequestCost.request_id).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("input_tokens"),
        func.sum(RequestCost.output_tokens).label("output_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    )
    q = _date_filter(q, RequestCost, start=start, end=end)
    rows = (
        q.group_by(RequestCost.org_id)
        .order_by(func.sum(RequestCost.total_cost).desc())
        .all()
    )

    return [
        {
            "org_id": r.org_id,
            "total_requests": r.total_requests or 0,
            "input_tokens": r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "total_cost": float(r.total_cost or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Daily cost trend
# ---------------------------------------------------------------------------

@router.get("/trend/daily")
def cost_trend_daily(
    *,
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> list[dict]:
    cutoff = date.today() - timedelta(days=days - 1)

    q = db.query(
        func.date(RequestCost.created_at).label("day"),
        func.count(RequestCost.request_id).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("input_tokens"),
        func.sum(RequestCost.output_tokens).label("output_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.input_token_cost).label("input_token_cost"),
        func.sum(RequestCost.output_token_cost).label("output_token_cost"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    ).filter(func.date(RequestCost.created_at) >= cutoff)

    q = _org_filter(q, RequestCost, org_id=org_id)
    q = _project_filter(q, RequestCost, project_id=project_id)
    rows = (
        q.group_by(func.date(RequestCost.created_at))
        .order_by(func.date(RequestCost.created_at).asc())
        .all()
    )

    return [
        {
            "date": str(r.day),
            "total_requests": r.total_requests or 0,
            "input_tokens": r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "input_cost": float(r.input_token_cost or 0),
            "output_cost": float(r.output_token_cost or 0),
            "total_cost": float(r.total_cost or 0),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Monthly cost trend — from MonthlyOrgSummary (pre-aggregated, up to 24h stale)
# ---------------------------------------------------------------------------

def _month_cutoff(months_back: int) -> date:
    today = date.today()
    total_months = today.year * 12 + today.month - months_back
    y, m = divmod(total_months, 12)
    if m == 0:
        m = 12
        y -= 1
    return date(y, m, 1)


@router.get("/trend/monthly")
def cost_trend_monthly(
    *,
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    months: int = Query(12, ge=1, le=36),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Monthly cost trend aggregated across all models per org/project.

    Reads from MonthlyOrgSummary — pre-aggregated by the daily scheduler.
    Data is up to 24 hours stale. Use /trend/daily for same-day accuracy.
    """
    cutoff = _month_cutoff(months - 1)

    q = db.query(
        MonthlyOrgSummary.month,
        MonthlyOrgSummary.org_id,
        MonthlyOrgSummary.project_id,
        func.sum(MonthlyOrgSummary.total_events).label("total_requests"),
        func.sum(MonthlyOrgSummary.total_prompt_tokens).label("input_tokens"),
        func.sum(MonthlyOrgSummary.total_completion_tokens).label("output_tokens"),
        func.sum(MonthlyOrgSummary.total_tokens).label("total_tokens"),
        func.sum(MonthlyOrgSummary.total_cost).label("total_cost"),
        func.sum(MonthlyOrgSummary.success_count).label("success_count"),
        func.sum(MonthlyOrgSummary.failure_count).label("failure_count"),
        func.avg(MonthlyOrgSummary.avg_latency_ms).label("avg_latency_ms"),
    ).filter(MonthlyOrgSummary.month >= cutoff)

    if org_id:
        q = q.filter(MonthlyOrgSummary.org_id == org_id)
    if project_id:
        q = q.filter(MonthlyOrgSummary.project_id == project_id)

    rows = (
        q.group_by(
            MonthlyOrgSummary.month,
            MonthlyOrgSummary.org_id,
            MonthlyOrgSummary.project_id,
        )
        .order_by(MonthlyOrgSummary.month.asc())
        .all()
    )

    return [
        {
            "month": str(r.month)[:7],
            "org_id": r.org_id,
            "project_id": r.project_id,
            "total_requests": r.total_requests or 0,
            "input_tokens": r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "total_cost": float(r.total_cost or 0),
            "success_count": r.success_count or 0,
            "failure_count": r.failure_count or 0,
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 2),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Single-request cost detail
# ---------------------------------------------------------------------------

@router.get("/request/{request_id}")
def cost_for_request(
    *,
    request_id: str,
    db: Session = Depends(get_db),
) -> dict:
    cost = db.query(RequestCost).filter(RequestCost.request_id == request_id).first()
    if not cost:
        raise HTTPException(status_code=404, detail="Request not found.")

    req = db.query(AiRequest).filter(AiRequest.request_id == request_id).first()

    return {
        "request_id": cost.request_id,
        "org_id": cost.org_id,
        "project_id": cost.project_id,
        "model_name": cost.model_name,
        "input_tokens": cost.input_tokens,
        "output_tokens": cost.output_tokens,
        "total_tokens": cost.total_tokens,
        "input_cost": float(cost.input_token_cost or 0),
        "output_cost": float(cost.output_token_cost or 0),
        "total_cost": float(cost.total_cost or 0),
        "currency": cost.currency,
        "request_type": req.request_type if req else None,
        "status": req.request_status if req else None,
        "created_at": cost.created_at.isoformat() if cost.created_at else None,
    }


# ---------------------------------------------------------------------------
# Summary totals (overview dashboard)
# ---------------------------------------------------------------------------

@router.get("/summary")
def cost_summary(
    *,
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(
        func.count(RequestCost.request_id).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("input_tokens"),
        func.sum(RequestCost.output_tokens).label("output_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.input_token_cost).label("input_token_cost"),
        func.sum(RequestCost.output_token_cost).label("output_token_cost"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    )
    q = _org_filter(q, RequestCost, org_id=org_id)
    q = _project_filter(q, RequestCost, project_id=project_id)
    q = _date_filter(q, RequestCost, start=start, end=end)
    r = q.first()

    return {
        "total_requests": r.total_requests or 0,
        "input_tokens": r.input_tokens or 0,
        "output_tokens": r.output_tokens or 0,
        "total_tokens": r.total_tokens or 0,
        "input_cost": float(r.input_token_cost or 0),
        "output_cost": float(r.output_token_cost or 0),
        "total_cost": float(r.total_cost or 0),
        "currency": "USD",
    }
