"""Dashboard summary endpoints.

DailyOrgSummary is populated hourly by the APScheduler job in workers/tasks.py
which aggregates AiRequest + RequestCost rows into daily rollups.

MonthlyOrgSummary is populated daily by the same scheduler and covers the
current calendar month. Monthly endpoints read from this pre-aggregated table
rather than re-grouping daily rows at query time.

Note: monthly data is up to 24 hours stale (scheduler cadence). Today's and
daily endpoints remain live.
"""
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import (
    Alert,
    AiRequest,
    Budget,
    DailyOrgSummary,
    GovernanceRule,
    MonthlyOrgSummary,
    RequestCost,
)

router = APIRouter(prefix="/summary", tags=["summary"])


def _org_filter(query, model, *, org_id: Optional[str]):
    if org_id:
        query = query.filter(model.org_id == org_id)
    return query


def _project_filter(query, model, *, project_id: Optional[str]):
    if project_id and hasattr(model, "project_id"):
        query = query.filter(model.project_id == project_id)
    return query


# ---------------------------------------------------------------------------
# Today's totals — live from RequestCost (always accurate)
# ---------------------------------------------------------------------------

@router.get("/today")
def get_today_summary(
    *,
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    today = date.today()
    q = db.query(
        func.count(RequestCost.request_id).label("total_requests"),
        func.sum(RequestCost.input_tokens).label("input_tokens"),
        func.sum(RequestCost.output_tokens).label("output_tokens"),
        func.sum(RequestCost.total_tokens).label("total_tokens"),
        func.sum(RequestCost.total_cost).label("total_cost"),
    ).filter(func.date(RequestCost.created_at) == today)
    q = _org_filter(q, RequestCost, org_id=org_id)
    q = _project_filter(q, RequestCost, project_id=project_id)
    r = q.first()

    return {
        "date": str(today),
        "total_requests": r.total_requests or 0,
        "input_tokens": r.input_tokens or 0,
        "output_tokens": r.output_tokens or 0,
        "total_tokens": r.total_tokens or 0,
        "total_cost": float(r.total_cost or 0),
        "currency": "USD",
    }


# ---------------------------------------------------------------------------
# Daily rollup — from DailyOrgSummary (aggregated by scheduler)
# ---------------------------------------------------------------------------

@router.get("/daily")
def get_daily_summary(
    *,
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> list[dict]:
    q = db.query(DailyOrgSummary)
    if start:
        q = q.filter(DailyOrgSummary.date >= start)
    if end:
        q = q.filter(DailyOrgSummary.date <= end)
    q = _org_filter(q, DailyOrgSummary, org_id=org_id)
    q = _project_filter(q, DailyOrgSummary, project_id=project_id)
    rows = q.order_by(DailyOrgSummary.date.desc()).all()

    return [
        {
            "date": str(r.date),
            "org_id": r.org_id,
            "project_id": r.project_id,
            "model_name": r.tool_name,
            "total_requests": r.total_events or 0,
            "input_tokens": r.total_prompt_tokens or 0,
            "output_tokens": r.total_completion_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "total_cost": float(r.total_cost or 0),
            "success_count": r.success_count or 0,
            "failure_count": r.failure_count or 0,
            "avg_latency_ms": r.avg_latency_ms or 0,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Monthly rollup — from MonthlyOrgSummary (pre-aggregated by scheduler, daily)
# Data is up to 24 hours stale. Current month appears after first scheduler run.
# ---------------------------------------------------------------------------

def _month_cutoff(months_back: int) -> date:
    today = date.today()
    total_months = today.year * 12 + today.month - months_back
    y, m = divmod(total_months, 12)
    if m == 0:
        m = 12
        y -= 1
    return date(y, m, 1)


@router.get("/monthly")
def get_monthly_summary(
    *,
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    months: int = Query(12, ge=1, le=36),
    db: Session = Depends(get_db),
) -> list[dict]:
    cutoff = _month_cutoff(months - 1)
    q = db.query(MonthlyOrgSummary).filter(MonthlyOrgSummary.month >= cutoff)
    if org_id:
        q = q.filter(MonthlyOrgSummary.org_id == org_id)
    if project_id:
        q = q.filter(MonthlyOrgSummary.project_id == project_id)
    rows = q.order_by(MonthlyOrgSummary.month.desc()).all()

    return [
        {
            "month": str(r.month)[:7],
            "org_id": r.org_id,
            "project_id": r.project_id,
            "total_requests": r.total_events or 0,
            "input_tokens": r.total_prompt_tokens or 0,
            "output_tokens": r.total_completion_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "total_cost": float(r.total_cost or 0),
            "success_count": r.success_count or 0,
            "failure_count": r.failure_count or 0,
            "avg_latency_ms": r.avg_latency_ms or 0,
        }
        for r in rows
    ]


@router.get("/monthly-by-model")
def get_monthly_summary_by_model(
    *,
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    months: int = Query(12, ge=1, le=36),
    db: Session = Depends(get_db),
) -> list[dict]:
    """Monthly cost and token breakdown per model.

    Reads from MonthlyOrgSummary.tool_name (which stores model name).
    Useful for charting cost-per-model trends over time.
    """
    cutoff = _month_cutoff(months - 1)
    q = db.query(MonthlyOrgSummary).filter(MonthlyOrgSummary.month >= cutoff)
    if org_id:
        q = q.filter(MonthlyOrgSummary.org_id == org_id)
    if project_id:
        q = q.filter(MonthlyOrgSummary.project_id == project_id)
    rows = q.order_by(MonthlyOrgSummary.month.desc(), MonthlyOrgSummary.total_cost.desc()).all()

    return [
        {
            "month": str(r.month)[:7],
            "org_id": r.org_id,
            "project_id": r.project_id,
            "model_name": r.tool_name,
            "total_requests": r.total_events or 0,
            "input_tokens": r.total_prompt_tokens or 0,
            "output_tokens": r.total_completion_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "total_cost": float(r.total_cost or 0),
            "success_count": r.success_count or 0,
            "failure_count": r.failure_count or 0,
            "avg_latency_ms": r.avg_latency_ms or 0,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Trends — daily cost + token trend for charts
# ---------------------------------------------------------------------------

@router.get("/trends")
def get_usage_trends(
    *,
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> list[dict]:
    cutoff = date.today() - timedelta(days=days - 1)

    q = db.query(
        DailyOrgSummary.date,
        func.sum(DailyOrgSummary.total_events).label("total_requests"),
        func.sum(DailyOrgSummary.total_tokens).label("total_tokens"),
        func.sum(DailyOrgSummary.total_cost).label("total_cost"),
        func.sum(DailyOrgSummary.success_count).label("success_count"),
        func.sum(DailyOrgSummary.failure_count).label("failure_count"),
        func.avg(DailyOrgSummary.avg_latency_ms).label("avg_latency_ms"),
    ).filter(DailyOrgSummary.date >= cutoff)

    q = _org_filter(q, DailyOrgSummary, org_id=org_id)
    q = _project_filter(q, DailyOrgSummary, project_id=project_id)
    rows = (
        q.group_by(DailyOrgSummary.date)
        .order_by(DailyOrgSummary.date.asc())
        .all()
    )

    return [
        {
            "date": str(r.date),
            "total_requests": r.total_requests or 0,
            "total_tokens": r.total_tokens or 0,
            "total_cost": float(r.total_cost or 0),
            "success_count": r.success_count or 0,
            "failure_count": r.failure_count or 0,
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 2),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Overview — headline metrics for the governance dashboard
# ---------------------------------------------------------------------------

@router.get("/overview")
def get_overview(
    *,
    org_id: Optional[str] = Query(None),
    range: Optional[str] = Query(None, description="today | 7d | 30d | 90d | all"),
    db: Session = Depends(get_db),
) -> dict:
    today = date.today()
    range_key = (range or "all").lower()

    if range_key == "today":
        cutoff = today
    elif range_key in {"7d", "week"}:
        cutoff = today - timedelta(days=6)
    elif range_key in {"30d", "month"}:
        cutoff = today - timedelta(days=29)
    elif range_key in {"90d", "quarter"}:
        cutoff = today - timedelta(days=89)
    else:
        cutoff = None

    # Cost totals from DailyOrgSummary
    cost_q = db.query(
        func.sum(DailyOrgSummary.total_events).label("total_requests"),
        func.sum(DailyOrgSummary.total_tokens).label("total_tokens"),
        func.sum(DailyOrgSummary.total_cost).label("total_cost"),
        func.sum(DailyOrgSummary.success_count).label("success_count"),
        func.sum(DailyOrgSummary.failure_count).label("failure_count"),
        func.avg(DailyOrgSummary.avg_latency_ms).label("avg_latency_ms"),
    )
    if cutoff:
        cost_q = cost_q.filter(DailyOrgSummary.date >= cutoff)
    cost_q = _org_filter(cost_q, DailyOrgSummary, org_id=org_id)
    totals = cost_q.first()

    total_requests = totals.total_requests or 0
    success = totals.success_count or 0
    failure = totals.failure_count or 0
    success_rate = round((success / (success + failure) * 100), 2) if (success + failure) > 0 else 100.0

    # Active alerts count
    alert_q = db.query(func.count(Alert.id)).filter(Alert.status == "active")
    alert_q = _org_filter(alert_q, Alert, org_id=org_id)
    active_alerts = alert_q.scalar() or 0

    # Active governance rules
    rules_active = db.query(func.count(GovernanceRule.id)).filter(GovernanceRule.is_active.is_(True)).scalar() or 0

    # Budget tracking — any project over 80% of their budget
    budgets_at_risk = 0
    if org_id:
        budgets = db.query(Budget).filter(Budget.org_id == org_id).all()
        for b in budgets:
            if not b.limit_amount or b.limit_amount <= 0:
                continue
            spent_q = db.query(func.sum(RequestCost.total_cost)).filter(
                RequestCost.org_id == org_id,
            )
            if b.project_id:
                spent_q = spent_q.filter(RequestCost.project_id == b.project_id)
            spent = spent_q.scalar() or Decimal("0")
            pct = float(spent) / float(b.limit_amount) * 100
            if pct >= 80:
                budgets_at_risk += 1

    return {
        "range": range_key,
        "total_requests": total_requests,
        "total_tokens": totals.total_tokens or 0,
        "total_cost": float(totals.total_cost or 0),
        "currency": "USD",
        "success_count": success,
        "failure_count": failure,
        "success_rate_pct": success_rate,
        "avg_latency_ms": round(float(totals.avg_latency_ms or 0), 2),
        "active_alerts": active_alerts,
        "rules_active": rules_active,
        "budgets_at_risk": budgets_at_risk,
    }
