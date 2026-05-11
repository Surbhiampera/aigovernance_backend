from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import Alert, DailyOrgSummary, GovernanceRule, TelemetryEvent, ToolConnector, UsageAnomaly
from app.routers.telemetry import _build_event_response
from app.schemas import (
    AlertResponse,
    DailySummaryResponse,
    GovernanceOverviewResponse,
    MonthlySummaryResponse,
    TodaySummaryResponse,
    UsageAnomalyResponse,
)

router = APIRouter(prefix="/summary", tags=["summary"])


@router.get("/today", response_model=TodaySummaryResponse)
def get_today_summary(db: Session = Depends(get_db)):
    today = date.today()
    rows = db.query(DailyOrgSummary).filter(DailyOrgSummary.date == today).all()
    total_cost = sum((Decimal(str(r.total_cost or 0)) for r in rows), Decimal("0"))
    total_events = sum((r.total_events or 0 for r in rows), 0)
    return TodaySummaryResponse(
        total_cost=total_cost,
        total_events=total_events,
        tools=[DailySummaryResponse.model_validate(r) for r in rows],
    )


@router.get("/daily", response_model=list[DailySummaryResponse])
def get_daily_summary(
    start: Optional[date] = Query(None),
    end: Optional[date] = Query(None),
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(DailyOrgSummary)
    if start:
        query = query.filter(DailyOrgSummary.date >= start)
    if end:
        query = query.filter(DailyOrgSummary.date <= end)
    if org_id:
        query = query.filter(DailyOrgSummary.org_id == org_id)
    if project_id:
        query = query.filter(DailyOrgSummary.project_id == project_id)
    return query.order_by(DailyOrgSummary.date.asc(), DailyOrgSummary.tool_name.asc()).all()


@router.get("/monthly", response_model=list[MonthlySummaryResponse])
def get_monthly_summary(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    from app.models import MonthlyOrgSummary

    query = db.query(MonthlyOrgSummary)
    if org_id:
        query = query.filter(MonthlyOrgSummary.org_id == org_id)
    if project_id:
        query = query.filter(MonthlyOrgSummary.project_id == project_id)
    return query.order_by(MonthlyOrgSummary.month.desc(), MonthlyOrgSummary.tool_name.asc()).all()


@router.get("/trends")
def get_usage_trends(
    org_id: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
):
    cutoff = date.today() - timedelta(days=days - 1)
    query = (
        db.query(
            DailyOrgSummary.date,
            func.sum(DailyOrgSummary.total_events).label("total_events"),
            func.sum(DailyOrgSummary.total_cost).label("total_cost"),
            func.sum(DailyOrgSummary.total_tokens).label("total_tokens"),
            func.avg(DailyOrgSummary.avg_latency_ms).label("avg_latency_ms"),
            func.sum(DailyOrgSummary.success_count).label("success_count"),
            func.sum(DailyOrgSummary.failure_count).label("failure_count"),
            func.avg(DailyOrgSummary.avg_risk_score).label("avg_risk_score"),
            func.sum(DailyOrgSummary.anomaly_count).label("anomaly_count"),
        )
        .filter(DailyOrgSummary.date >= cutoff)
        .group_by(DailyOrgSummary.date)
        .order_by(DailyOrgSummary.date.asc())
    )
    if org_id:
        query = query.filter(DailyOrgSummary.org_id == org_id)

    rows = query.all()
    return [
        {
            "date": str(r.date),
            "total_events": r.total_events or 0,
            "total_cost": float(r.total_cost or 0),
            "total_tokens": r.total_tokens or 0,
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 2),
            "success_count": r.success_count or 0,
            "failure_count": r.failure_count or 0,
            "avg_risk_score": round(float(r.avg_risk_score or 0), 2),
            "anomaly_count": r.anomaly_count or 0,
        }
        for r in rows
    ]


@router.get("/overview", response_model=GovernanceOverviewResponse)
def get_governance_overview(
    org_id: Optional[str] = Query(None),
    days: int = Query(14, ge=1, le=365),
    range: Optional[str] = Query(
        None,
        description="Optional time filter: 'today', '7d', '30d', '90d', 'all'. "
        "When omitted or 'all' the overview reflects ALL telemetry data.",
    ),
    db: Session = Depends(get_db),
):
    today = date.today()

    # ---- Comprehensive Overview ----
    # Default behaviour now shows the full system activity (not only "today").
    # An optional `range` filter narrows the window for the headline metrics.
    range_key = (range or "all").lower()
    if range_key == "today":
        cutoff_date: Optional[date] = today
    elif range_key in {"7d", "week"}:
        cutoff_date = today - timedelta(days=6)
    elif range_key in {"30d", "month"}:
        cutoff_date = today - timedelta(days=29)
    elif range_key in {"90d", "quarter"}:
        cutoff_date = today - timedelta(days=89)
    else:
        cutoff_date = None  # all-time

    # Datetime version of the cutoff for filtering timestamp columns
    cutoff_dt: Optional[datetime] = (
        datetime.combine(cutoff_date, time.min) if cutoff_date is not None else None
    )

    today_query = db.query(DailyOrgSummary)
    if cutoff_date is not None:
        today_query = today_query.filter(DailyOrgSummary.date >= cutoff_date)
    if org_id:
        today_query = today_query.filter(DailyOrgSummary.org_id == org_id)
    today_rows = today_query.all()

    total_cost = sum((Decimal(str(row.total_cost or 0)) for row in today_rows), Decimal("0"))
    total_events = sum((row.total_events or 0 for row in today_rows), 0)
    total_tokens = sum((row.total_tokens or 0 for row in today_rows), 0)
    total_success = sum((row.success_count or 0 for row in today_rows), 0)
    total_failure = sum((row.failure_count or 0 for row in today_rows), 0)
    avg_latency = (
        Decimal(str(sum((row.avg_latency_ms or 0 for row in today_rows), 0))) / Decimal(str(len(today_rows)))
        if today_rows
        else Decimal("0")
    )
    avg_risk = (
        Decimal(str(sum((Decimal(str(row.avg_risk_score or 0)) for row in today_rows), Decimal("0")))) / Decimal(str(len(today_rows)))
        if today_rows
        else Decimal("0")
    )

    recent_alerts_query = db.query(Alert).order_by(Alert.created_at.desc())
    recent_anomalies_query = db.query(UsageAnomaly).order_by(UsageAnomaly.created_at.desc())
    recent_events_query = db.query(TelemetryEvent).order_by(TelemetryEvent.created_at.desc())
    alerts_count_query = db.query(func.count(Alert.id)).filter(Alert.status == "active")
    anomalies_count_query = db.query(func.count(UsageAnomaly.id)).filter(UsageAnomaly.status == "open")
    connectors_query = db.query(func.count(ToolConnector.id)).filter(ToolConnector.status == "active")
    rules_query = db.query(func.count(GovernanceRule.id)).filter(GovernanceRule.is_active.is_(True))

    if cutoff_dt is not None:
        recent_alerts_query = recent_alerts_query.filter(Alert.created_at >= cutoff_dt)
        recent_anomalies_query = recent_anomalies_query.filter(UsageAnomaly.created_at >= cutoff_dt)
        recent_events_query = recent_events_query.filter(TelemetryEvent.created_at >= cutoff_dt)
        alerts_count_query = alerts_count_query.filter(Alert.created_at >= cutoff_dt)
        anomalies_count_query = anomalies_count_query.filter(UsageAnomaly.created_at >= cutoff_dt)

    if org_id:
        recent_alerts_query = recent_alerts_query.filter(Alert.org_id == org_id)
        recent_anomalies_query = recent_anomalies_query.filter(UsageAnomaly.org_id == org_id)
        recent_events_query = recent_events_query.filter(TelemetryEvent.org_id == org_id)
        alerts_count_query = alerts_count_query.filter(Alert.org_id == org_id)
        anomalies_count_query = anomalies_count_query.filter(UsageAnomaly.org_id == org_id)

    sev_filters = [Alert.status == "active"]
    if cutoff_dt is not None:
        sev_filters.append(Alert.created_at >= cutoff_dt)
    if org_id:
        sev_filters.append(Alert.org_id == org_id)
    severity_rows = (
        db.query(Alert.severity, func.count(Alert.id))
        .filter(*sev_filters)
        .group_by(Alert.severity)
        .all()
    )

    cost_by_type = {
        "llm": sum((Decimal(str(row.llm_cost or 0)) for row in today_rows), Decimal("0")),
        "infra": sum((Decimal(str(row.infra_cost or 0)) for row in today_rows), Decimal("0")),
        "external": sum((Decimal(str(row.external_cost or 0)) for row in today_rows), Decimal("0")),
    }

    health = _build_health_metrics(db, org_id, cutoff_dt)

    recent_alerts = recent_alerts_query.limit(6).all()
    recent_anomalies = recent_anomalies_query.limit(6).all()
    recent_events = recent_events_query.limit(8).all()

    highest_risk = db.query(func.coalesce(func.max(TelemetryEvent.risk_score), 0))
    if cutoff_dt is not None:
        highest_risk = highest_risk.filter(TelemetryEvent.created_at >= cutoff_dt)
    if org_id:
        highest_risk = highest_risk.filter(TelemetryEvent.org_id == org_id)

    success_rate = Decimal("100")
    if total_success + total_failure > 0:
        success_rate = (Decimal(str(total_success)) / Decimal(str(total_success + total_failure))) * Decimal("100")

    return GovernanceOverviewResponse(
        total_cost_today=total_cost,
        total_events_today=total_events,
        total_tokens_today=total_tokens,
        avg_latency_today=avg_latency.quantize(Decimal("0.01")) if avg_latency else Decimal("0"),
        success_rate_today=success_rate.quantize(Decimal("0.01")),
        active_alerts=alerts_count_query.scalar() or 0,
        anomalies_open=anomalies_count_query.scalar() or 0,
        connectors_active=connectors_query.scalar() or 0,
        rules_active=rules_query.scalar() or 0,
        avg_risk_score=avg_risk.quantize(Decimal("0.01")) if avg_risk else Decimal("0"),
        highest_risk_score=Decimal(str(highest_risk.scalar() or 0)).quantize(Decimal("0.01")),
        alerts_by_severity={severity or "unknown": count for severity, count in severity_rows},
        cost_by_type={key: value.quantize(Decimal("0.000001")) for key, value in cost_by_type.items()},
        health=health,
        tool_rollup=[DailySummaryResponse.model_validate(row) for row in today_rows],
        recent_alerts=[AlertResponse.model_validate(row) for row in recent_alerts],
        recent_anomalies=[UsageAnomalyResponse.model_validate(row) for row in recent_anomalies],
        recent_events=[_build_event_response(db, row) for row in recent_events],
    )


def _build_health_metrics(db: Session, org_id: Optional[str], cutoff_dt: Optional[datetime]) -> dict[str, Decimal]:
    query = db.query(
        func.count(TelemetryEvent.id).label("events"),
        func.sum(case((TelemetryEvent.status.in_(["success", "completed"]), 1), else_=0)).label("success"),
        func.avg(TelemetryEvent.latency_ms).label("latency"),
        func.avg(TelemetryEvent.anomaly_score).label("anomaly"),
    )
    if cutoff_dt is not None:
        query = query.filter(TelemetryEvent.created_at >= cutoff_dt)
    if org_id:
        query = query.filter(TelemetryEvent.org_id == org_id)
    row = query.first()
    total_events = Decimal(str(row.events or 0))
    success_events = Decimal(str(row.success or 0))
    success_rate = Decimal("100") if total_events == 0 else (success_events / total_events) * Decimal("100")
    return {
        "success_rate": success_rate.quantize(Decimal("0.01")),
        "failure_rate": (Decimal("100") - success_rate).quantize(Decimal("0.01")),
        "avg_latency_ms": Decimal(str(row.latency or 0)).quantize(Decimal("0.01")),
        "anomaly_score": Decimal(str(row.anomaly or 0)).quantize(Decimal("0.01")),
    }
