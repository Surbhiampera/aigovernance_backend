"""Scheduled task functions called by APScheduler.

All DB sessions are managed by the caller (scheduler.py).
Only two jobs remain:
  _rebuild_daily_summary  — aggregates AiRequest + RequestCost → DailyOrgSummary
  _rebuild_monthly_summary — rolls up DailyOrgSummary → MonthlyOrgSummary
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import (
    AiRequest,
    DailyOrgSummary,
    MonthlyOrgSummary,
    Organization,
    RequestCost,
)


# ---------------------------------------------------------------------------
# Daily aggregation — AiRequest + RequestCost → DailyOrgSummary
# ---------------------------------------------------------------------------

def _rebuild_daily_summary(*, db: Session, summary_date: date) -> int:
    rows = (
        db.query(
            RequestCost.org_id,
            RequestCost.project_id,
            RequestCost.model_name,
            func.count(RequestCost.request_id).label("total_requests"),
            func.sum(RequestCost.input_tokens).label("input_tokens"),
            func.sum(RequestCost.output_tokens).label("output_tokens"),
            func.sum(RequestCost.total_tokens).label("total_tokens"),
            func.sum(RequestCost.total_cost).label("total_cost"),
            func.sum(RequestCost.input_cost).label("input_cost"),
            func.sum(RequestCost.output_cost).label("output_cost"),
        )
        .filter(func.date(RequestCost.created_at) == summary_date)
        .filter(RequestCost.org_id.isnot(None))
        .group_by(RequestCost.org_id, RequestCost.project_id, RequestCost.model_name)
        .all()
    )

    # Latency and status counts come from AiRequest
    req_rows = (
        db.query(
            AiRequest.org_id,
            AiRequest.project_id,
            AiRequest.model_name,
            func.count(AiRequest.request_id).label("total"),
            func.avg(
                func.extract("epoch", AiRequest.completed_at - AiRequest.created_at) * 1000
            ).label("avg_latency_ms"),
            func.sum(
                func.cast(AiRequest.status == "success", func.Integer())
            ).label("success_count"),
            func.sum(
                func.cast(AiRequest.status != "success", func.Integer())
            ).label("failure_count"),
        )
        .filter(func.date(AiRequest.created_at) == summary_date)
        .filter(AiRequest.org_id.isnot(None))
        .group_by(AiRequest.org_id, AiRequest.project_id, AiRequest.model_name)
        .all()
    )

    req_index: dict[tuple, Any] = {
        (r.org_id, r.project_id, r.model_name): r for r in req_rows
    }

    db.query(DailyOrgSummary).filter(DailyOrgSummary.date == summary_date).delete(
        synchronize_session=False
    )

    inserted = 0
    for row in rows:
        org_id = (row.org_id or "").strip()
        if not org_id:
            continue

        req = req_index.get((row.org_id, row.project_id, row.model_name))
        success_count = int(req.success_count or 0) if req else 0
        failure_count = int(req.failure_count or 0) if req else 0
        avg_latency_ms = int(req.avg_latency_ms or 0) if req else 0

        db.add(DailyOrgSummary(
            org_id=org_id,
            project_id=row.project_id,
            tool_name=(row.model_name or "").strip() or "unknown",
            date=summary_date,
            total_events=row.total_requests or 0,
            total_cost=row.total_cost or Decimal("0"),
            llm_cost=row.total_cost or Decimal("0"),
            infra_cost=Decimal("0"),
            external_cost=Decimal("0"),
            total_prompt_tokens=row.input_tokens or 0,
            total_completion_tokens=row.output_tokens or 0,
            total_tokens=row.total_tokens or 0,
            avg_latency_ms=avg_latency_ms,
            success_count=success_count,
            failure_count=failure_count,
            anomaly_count=0,
            misuse_count=0,
            total_input_mb=Decimal("0"),
            total_output_mb=Decimal("0"),
            avg_risk_score=Decimal("0"),
        ))
        inserted += 1

    db.flush()
    return inserted


# ---------------------------------------------------------------------------
# Monthly aggregation — DailyOrgSummary → MonthlyOrgSummary
# ---------------------------------------------------------------------------

def _rebuild_monthly_summary(*, db: Session) -> int:
    today = date.today()
    month_start = today.replace(day=1)
    valid_org_ids = db.query(Organization.id).subquery()

    rows = (
        db.query(
            DailyOrgSummary.org_id,
            DailyOrgSummary.project_id,
            DailyOrgSummary.tool_name,
            func.sum(DailyOrgSummary.total_events).label("total_events"),
            func.sum(DailyOrgSummary.total_cost).label("total_cost"),
            func.sum(DailyOrgSummary.total_tokens).label("total_tokens"),
            func.sum(DailyOrgSummary.total_prompt_tokens).label("total_prompt_tokens"),
            func.sum(DailyOrgSummary.total_completion_tokens).label("total_completion_tokens"),
            func.avg(DailyOrgSummary.avg_latency_ms).label("avg_latency_ms"),
            func.sum(DailyOrgSummary.success_count).label("success_count"),
            func.sum(DailyOrgSummary.failure_count).label("failure_count"),
        )
        .filter(
            DailyOrgSummary.date >= month_start,
            DailyOrgSummary.date <= today,
            DailyOrgSummary.org_id.isnot(None),
            DailyOrgSummary.org_id.in_(valid_org_ids),
        )
        .group_by(
            DailyOrgSummary.org_id,
            DailyOrgSummary.project_id,
            DailyOrgSummary.tool_name,
        )
        .all()
    )

    db.query(MonthlyOrgSummary).filter(
        MonthlyOrgSummary.month == month_start
    ).delete(synchronize_session=False)

    inserted = 0
    for row in rows:
        org_id = (row.org_id or "").strip()
        if not org_id:
            continue
        db.add(MonthlyOrgSummary(
            org_id=org_id,
            project_id=row.project_id,
            tool_name=(row.tool_name or "").strip() or "unknown",
            month=month_start,
            total_events=row.total_events or 0,
            total_cost=row.total_cost or Decimal("0"),
            llm_cost=row.total_cost or Decimal("0"),
            infra_cost=Decimal("0"),
            external_cost=Decimal("0"),
            total_tokens=row.total_tokens or 0,
            total_prompt_tokens=row.total_prompt_tokens or 0,
            total_completion_tokens=row.total_completion_tokens or 0,
            avg_latency_ms=int(row.avg_latency_ms or 0),
            success_count=row.success_count or 0,
            failure_count=row.failure_count or 0,
            anomaly_count=0,
            misuse_count=0,
        ))
        inserted += 1

    db.flush()
    return inserted
