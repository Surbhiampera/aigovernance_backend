from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import Alert, Budget, DailyOrgSummary, MonthlyOrgSummary, TelemetryEvent, ToolRegistry

router = APIRouter(prefix="/costs", tags=["costs"])


def _scope(query, model, org_id, project_id=None):
    if org_id:
        query = query.filter(model.org_id == org_id)
    if project_id and hasattr(model, "project_id"):
        query = query.filter(model.project_id == project_id)
    return query


@router.get("/by-model")
def cost_by_model(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(
            TelemetryEvent.model_name,
            TelemetryEvent.provider,
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.prompt_tokens).label("prompt_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("completion_tokens"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
            func.avg(TelemetryEvent.latency_ms).label("avg_latency_ms"),
            func.sum(case((TelemetryEvent.status.in_(["success", "completed"]), 1), else_=0)).label("success_count"),
        )
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.model_name, TelemetryEvent.provider)
        .order_by(func.sum(TelemetryEvent.total_cost).desc())
    )
    if org_id:
        rows = rows.filter(TelemetryEvent.org_id == org_id)
    if project_id:
        rows = rows.filter(TelemetryEvent.project_id == project_id)

    results = []
    for r in rows.all():
        total = r.total_events or 0
        success_rate = Decimal("100") if total == 0 else (Decimal(str(r.success_count or 0)) / Decimal(str(total))) * Decimal("100")
        results.append({
            "model_name": r.model_name,
            "provider": r.provider or "—",
            "total_events": total,
            "prompt_tokens": r.prompt_tokens or 0,
            "completion_tokens": r.completion_tokens or 0,
            "total_tokens": r.total_tokens or 0,
            "total_cost": float(r.total_cost or 0),
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 1),
            "success_rate": round(float(success_rate), 1),
        })
    return results


@router.get("/by-project")
def cost_by_project(
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(
            TelemetryEvent.project_id,
            TelemetryEvent.org_id,
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.llm_cost).label("llm_cost"),
            func.sum(TelemetryEvent.infra_cost).label("infra_cost"),
            func.sum(TelemetryEvent.external_cost).label("external_cost"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
            func.avg(TelemetryEvent.latency_ms).label("avg_latency_ms"),
            func.count(TelemetryEvent.model_name.distinct()).label("tool_count"),
        )
        .filter(TelemetryEvent.project_id.isnot(None), TelemetryEvent.project_id != "")
        .group_by(TelemetryEvent.project_id, TelemetryEvent.org_id)
        .order_by(func.sum(TelemetryEvent.total_cost).desc())
    )
    if org_id:
        rows = rows.filter(TelemetryEvent.org_id == org_id)

    return [
        {
            "project_id": r.project_id,
            "org_id": r.org_id,
            "total_events": r.total_events or 0,
            "total_tokens": r.total_tokens or 0,
            "llm_cost": float(r.llm_cost or 0),
            "infra_cost": float(r.infra_cost or 0),
            "external_cost": float(r.external_cost or 0),
            "total_cost": float(r.total_cost or 0),
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 1),
            "tool_count": int(r.tool_count or 0),
        }
        for r in rows.all()
    ]


@router.get("/project-breakdown")
def project_cost_breakdown(
    project_id: str = Query(...),
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Full cost breakdown for a single project: total aggregates + per-tool split
    with each tool's % share of the project total cost."""
    filters = [TelemetryEvent.project_id == project_id]
    if org_id:
        filters.append(TelemetryEvent.org_id == org_id)

    totals = (
        db.query(
            func.sum(TelemetryEvent.llm_cost).label("llm_cost"),
            func.sum(TelemetryEvent.infra_cost).label("infra_cost"),
            func.sum(TelemetryEvent.external_cost).label("external_cost"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.avg(TelemetryEvent.latency_ms).label("avg_latency_ms"),
        )
        .filter(*filters)
        .first()
    )

    total_cost = float(totals.total_cost or 0) if totals else 0.0
    llm = float(totals.llm_cost or 0) if totals else 0.0
    infra = float(totals.infra_cost or 0) if totals else 0.0
    external = float(totals.external_cost or 0) if totals else 0.0
    pct = lambda v: round((v / total_cost * 100), 1) if total_cost > 0 else 0.0

    tool_rows = (
        db.query(
            TelemetryEvent.model_name.label("tool_name"),
            func.max(ToolRegistry.vendor).label("vendor"),
            func.max(ToolRegistry.cost_model).label("cost_model"),
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.llm_cost).label("llm_cost"),
            func.sum(TelemetryEvent.infra_cost).label("infra_cost"),
            func.sum(TelemetryEvent.external_cost).label("external_cost"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
        )
        .outerjoin(ToolRegistry, ToolRegistry.tool_name == TelemetryEvent.model_name)
        .filter(*filters)
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.model_name)
        .order_by(func.sum(TelemetryEvent.total_cost).desc())
        .all()
    )

    tools = [
        {
            "tool_name": r.tool_name,
            "vendor": r.vendor or "—",
            "cost_model": r.cost_model or "per_token",
            "total_events": r.total_events or 0,
            "total_tokens": int(r.total_tokens or 0),
            "llm_cost": float(r.llm_cost or 0),
            "infra_cost": float(r.infra_cost or 0),
            "external_cost": float(r.external_cost or 0),
            "total_cost": float(r.total_cost or 0),
            "cost_share_pct": pct(float(r.total_cost or 0)),
        }
        for r in tool_rows
    ]

    return {
        "project_id": project_id,
        "org_id": org_id,
        "total_cost": round(total_cost, 6),
        "llm_cost": round(llm, 6),
        "infra_cost": round(infra, 6),
        "external_cost": round(external, 6),
        "llm_pct": pct(llm),
        "infra_pct": pct(infra),
        "external_pct": pct(external),
        "total_events": int(totals.total_events or 0) if totals else 0,
        "total_tokens": int(totals.total_tokens or 0) if totals else 0,
        "avg_latency_ms": round(float(totals.avg_latency_ms or 0), 1) if totals else 0.0,
        "tool_count": len(tools),
        "tools": tools,
    }


@router.get("/daily")
def cost_daily(
    days: int = Query(14, ge=1, le=90),
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    cutoff = date.today() - timedelta(days=days - 1)
    query = (
        db.query(
            DailyOrgSummary.date,
            DailyOrgSummary.tool_name,
            func.sum(DailyOrgSummary.total_cost).label("total_cost"),
            func.sum(DailyOrgSummary.total_tokens).label("total_tokens"),
            func.sum(DailyOrgSummary.total_events).label("total_events"),
        )
        .filter(DailyOrgSummary.date >= cutoff)
        .group_by(DailyOrgSummary.date, DailyOrgSummary.tool_name)
        .order_by(DailyOrgSummary.date.desc())
    )
    if org_id:
        query = query.filter(DailyOrgSummary.org_id == org_id)
    if project_id:
        query = query.filter(DailyOrgSummary.project_id == project_id)

    return [
        {
            "date": str(r.date),
            "tool_name": r.tool_name,
            "total_cost": float(r.total_cost or 0),
            "total_tokens": r.total_tokens or 0,
            "total_events": r.total_events or 0,
        }
        for r in query.all()
    ]


@router.get("/monthly")
def cost_monthly(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = (
        db.query(
            MonthlyOrgSummary.month,
            MonthlyOrgSummary.tool_name,
            func.sum(MonthlyOrgSummary.total_cost).label("total_cost"),
            func.sum(MonthlyOrgSummary.total_tokens).label("total_tokens"),
            func.sum(MonthlyOrgSummary.total_events).label("total_events"),
        )
        .group_by(MonthlyOrgSummary.month, MonthlyOrgSummary.tool_name)
        .order_by(MonthlyOrgSummary.month.desc())
    )
    if org_id:
        query = query.filter(MonthlyOrgSummary.org_id == org_id)
    if project_id:
        query = query.filter(MonthlyOrgSummary.project_id == project_id)

    return [
        {
            "month": str(r.month),
            "tool_name": r.tool_name,
            "total_cost": float(r.total_cost or 0),
            "total_tokens": r.total_tokens or 0,
            "total_events": r.total_events or 0,
        }
        for r in query.all()
    ]


@router.get("/by-org")
def cost_by_org(db: Session = Depends(get_db)):
    rows = (
        db.query(
            TelemetryEvent.org_id,
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
            func.avg(TelemetryEvent.latency_ms).label("avg_latency_ms"),
        )
        .group_by(TelemetryEvent.org_id)
        .order_by(func.sum(TelemetryEvent.total_cost).desc())
        .all()
    )
    return [
        {
            "org_id": r.org_id,
            "total_events": r.total_events or 0,
            "total_tokens": r.total_tokens or 0,
            "total_cost": float(r.total_cost or 0),
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 1),
        }
        for r in rows
    ]


@router.get("/totals")
def cost_totals(db: Session = Depends(get_db)):
    today = date.today()
    first_of_month = today.replace(day=1)

    daily_row = (
        db.query(
            func.sum(DailyOrgSummary.total_cost).label("cost"),
            func.sum(DailyOrgSummary.total_tokens).label("tokens"),
            func.sum(DailyOrgSummary.total_events).label("events"),
        )
        .filter(DailyOrgSummary.date == today)
        .first()
    )

    monthly_row = (
        db.query(
            func.sum(DailyOrgSummary.total_cost).label("cost"),
            func.sum(DailyOrgSummary.total_tokens).label("tokens"),
            func.sum(DailyOrgSummary.total_events).label("events"),
        )
        .filter(DailyOrgSummary.date >= first_of_month)
        .first()
    )

    all_time = (
        db.query(
            func.sum(TelemetryEvent.total_cost).label("cost"),
            func.sum(TelemetryEvent.total_tokens).label("tokens"),
            func.count(TelemetryEvent.id).label("events"),
        )
        .first()
    )

    return {
        "today": {
            "cost": float(daily_row.cost or 0) if daily_row else 0,
            "tokens": int(daily_row.tokens or 0) if daily_row else 0,
            "events": int(daily_row.events or 0) if daily_row else 0,
        },
        "this_month": {
            "cost": float(monthly_row.cost or 0) if monthly_row else 0,
            "tokens": int(monthly_row.tokens or 0) if monthly_row else 0,
            "events": int(monthly_row.events or 0) if monthly_row else 0,
        },
        "all_time": {
            "cost": float(all_time.cost or 0) if all_time else 0,
            "tokens": int(all_time.tokens or 0) if all_time else 0,
            "events": int(all_time.events or 0) if all_time else 0,
        },
    }


# -------------------------------------------------------------------------
# Additional cost dimensions — surfaced so users can interpret cost from
# every relevant angle (tool, provider, execution type, service type) and
# see the underlying LLM/Infra/External split that produced each total.
# All endpoints use the same simple formula:  total = llm + infra + external
# -------------------------------------------------------------------------


@router.get("/by-tool")
def cost_by_tool(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Cost grouped by registered tool (joined to the Control Module's
    ToolRegistry so vendor and pricing model are visible). Tools without
    telemetry yet show as zero — making the full integrated surface area
    transparent.
    """
    rows = (
        db.query(
            TelemetryEvent.model_name.label("tool_name"),
            func.max(ToolRegistry.vendor).label("vendor"),
            func.max(ToolRegistry.cost_model).label("cost_model"),
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.llm_cost).label("llm_cost"),
            func.sum(TelemetryEvent.infra_cost).label("infra_cost"),
            func.sum(TelemetryEvent.external_cost).label("external_cost"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
        )
        .outerjoin(ToolRegistry, ToolRegistry.tool_name == TelemetryEvent.model_name)
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.model_name)
        .order_by(func.sum(TelemetryEvent.total_cost).desc())
    )
    rows = _scope(rows, TelemetryEvent, org_id, project_id)
    return [
        {
            "tool_name": r.tool_name,
            "vendor": r.vendor or "—",
            "cost_model": r.cost_model or "per_token",
            "total_events": r.total_events or 0,
            "total_tokens": int(r.total_tokens or 0),
            "llm_cost": float(r.llm_cost or 0),
            "infra_cost": float(r.infra_cost or 0),
            "external_cost": float(r.external_cost or 0),
            "total_cost": float(r.total_cost or 0),
        }
        for r in rows.all()
    ]


@router.get("/by-provider")
def cost_by_provider(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Cost grouped by vendor/provider (OpenAI, Anthropic, internal, …)."""
    rows = (
        db.query(
            TelemetryEvent.provider,
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.llm_cost).label("llm_cost"),
            func.sum(TelemetryEvent.infra_cost).label("infra_cost"),
            func.sum(TelemetryEvent.external_cost).label("external_cost"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
        )
        .group_by(TelemetryEvent.provider)
        .order_by(func.sum(TelemetryEvent.total_cost).desc())
    )
    rows = _scope(rows, TelemetryEvent, org_id, project_id)
    return [
        {
            "provider": r.provider or "—",
            "total_events": r.total_events or 0,
            "total_tokens": int(r.total_tokens or 0),
            "llm_cost": float(r.llm_cost or 0),
            "infra_cost": float(r.infra_cost or 0),
            "external_cost": float(r.external_cost or 0),
            "total_cost": float(r.total_cost or 0),
        }
        for r in rows.all()
    ]


@router.get("/by-execution-type")
def cost_by_execution_type(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Cost grouped by execution_type (sync, async, batch, stream, …)."""
    rows = (
        db.query(
            TelemetryEvent.execution_type,
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
            func.avg(TelemetryEvent.latency_ms).label("avg_latency_ms"),
        )
        .group_by(TelemetryEvent.execution_type)
        .order_by(func.sum(TelemetryEvent.total_cost).desc())
    )
    rows = _scope(rows, TelemetryEvent, org_id, project_id)
    return [
        {
            "execution_type": r.execution_type or "—",
            "total_events": r.total_events or 0,
            "total_tokens": int(r.total_tokens or 0),
            "total_cost": float(r.total_cost or 0),
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 1),
        }
        for r in rows.all()
    ]


@router.get("/by-service-type")
def cost_by_service_type(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Cost grouped by service_type (chat, completion, embedding, …)."""
    rows = (
        db.query(
            TelemetryEvent.service_type,
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
        )
        .group_by(TelemetryEvent.service_type)
        .order_by(func.sum(TelemetryEvent.total_cost).desc())
    )
    rows = _scope(rows, TelemetryEvent, org_id, project_id)
    return [
        {
            "service_type": r.service_type or "—",
            "total_events": r.total_events or 0,
            "total_tokens": int(r.total_tokens or 0),
            "total_cost": float(r.total_cost or 0),
        }
        for r in rows.all()
    ]


@router.get("/breakdown")
def cost_breakdown_summary(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Aggregated cost split (LLM / Infra / External) plus the simple
    formulas used to derive each component. Designed to be fully
    transparent — no hidden multipliers, no opaque adjustments.
    """
    q = db.query(
        func.sum(TelemetryEvent.llm_cost).label("llm_cost"),
        func.sum(TelemetryEvent.infra_cost).label("infra_cost"),
        func.sum(TelemetryEvent.external_cost).label("external_cost"),
        func.sum(TelemetryEvent.total_cost).label("total_cost"),
        func.count(TelemetryEvent.id).label("total_events"),
        func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
    )
    q = _scope(q, TelemetryEvent, org_id, project_id)
    row = q.first()

    llm = float(row.llm_cost or 0) if row else 0.0
    infra = float(row.infra_cost or 0) if row else 0.0
    external = float(row.external_cost or 0) if row else 0.0
    total = float(row.total_cost or 0) if row else 0.0
    events = int(row.total_events or 0) if row else 0
    tokens = int(row.total_tokens or 0) if row else 0
    pct = lambda v: round((v / total * 100), 2) if total > 0 else 0.0

    return {
        "components": [
            {
                "name": "llm_cost",
                "amount": round(llm, 6),
                "percent": pct(llm),
                "formula": "(prompt_tokens × input_rate + completion_tokens × output_rate) ÷ 1000",
            },
            {
                "name": "infra_cost",
                "amount": round(infra, 6),
                "percent": pct(infra),
                "formula": "latency_ms × $0.00008",
            },
            {
                "name": "external_cost",
                "amount": round(external, 6),
                "percent": pct(external),
                "formula": "Σ external_tools[i].cost (passed through, no mark-up)",
            },
        ],
        "total_cost": round(total, 6),
        "total_events": events,
        "total_tokens": tokens,
        "avg_cost_per_event": round(total / events, 6) if events else 0.0,
        "avg_cost_per_1k_tokens": round((total / tokens) * 1000, 6) if tokens else 0.0,
        "formula": "total_cost = llm_cost + infra_cost + external_cost",
    }


@router.get("/per-tool-daily")
def cost_per_tool_daily(
    days: int = Query(14, ge=1, le=90),
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Per-tool, per-day cost rows with input/output token split and email volume."""
    from datetime import datetime as _dt
    cutoff = date.today() - timedelta(days=days - 1)

    query = (
        db.query(
            DailyOrgSummary.date,
            DailyOrgSummary.tool_name,
            func.sum(DailyOrgSummary.total_cost).label("total_cost"),
            func.sum(DailyOrgSummary.total_prompt_tokens).label("input_tokens"),
            func.sum(DailyOrgSummary.total_completion_tokens).label("output_tokens"),
            func.sum(DailyOrgSummary.total_tokens).label("total_tokens"),
            func.sum(DailyOrgSummary.total_events).label("total_events"),
        )
        .filter(DailyOrgSummary.date >= cutoff)
        .group_by(DailyOrgSummary.date, DailyOrgSummary.tool_name)
        .order_by(DailyOrgSummary.date.desc(), func.sum(DailyOrgSummary.total_cost).desc())
    )
    if org_id:
        query = query.filter(DailyOrgSummary.org_id == org_id)
    if project_id:
        query = query.filter(DailyOrgSummary.project_id == project_id)

    # Email volume: events whose service_type or model_name contains "email"
    email_q = (
        db.query(
            func.date(TelemetryEvent.created_at).label("date"),
            TelemetryEvent.model_name.label("tool_name"),
            func.count(TelemetryEvent.id).label("email_count"),
        )
        .filter(TelemetryEvent.created_at >= _dt.combine(cutoff, _dt.min.time()))
        .filter(
            func.lower(func.coalesce(TelemetryEvent.service_type, "")).contains("email")
            | func.lower(func.coalesce(TelemetryEvent.model_name, "")).contains("email")
        )
        .group_by(func.date(TelemetryEvent.created_at), TelemetryEvent.model_name)
    )
    if org_id:
        email_q = email_q.filter(TelemetryEvent.org_id == org_id)
    if project_id:
        email_q = email_q.filter(TelemetryEvent.project_id == project_id)
    email_map = {(str(r.date), r.tool_name): int(r.email_count) for r in email_q.all()}

    return [
        {
            "date": str(r.date),
            "tool_name": r.tool_name,
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "total_cost": float(r.total_cost or 0),
            "total_events": int(r.total_events or 0),
            "email_volume": email_map.get((str(r.date), r.tool_name), 0),
        }
        for r in query.all()
    ]


@router.get("/spend-cap-status")
def spend_cap_status(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Live spend vs cap for every configured budget — used by the Spend Cap dashboard."""
    import calendar as _cal
    today = date.today()
    month_start = today.replace(day=1)
    days_elapsed = max((today - month_start).days + 1, 1)
    days_in_month = _cal.monthrange(today.year, today.month)[1]
    days_remaining = days_in_month - today.day

    budget_q = db.query(Budget)
    if org_id:
        budget_q = budget_q.filter(Budget.org_id == org_id)
    if project_id:
        budget_q = budget_q.filter(Budget.project_id == project_id)

    # Active alerts keyed by org_id
    alert_q = db.query(Alert).filter(Alert.status == "active")
    if org_id:
        alert_q = alert_q.filter(Alert.org_id == org_id)
    alerts_by_org: dict = {}
    for a in alert_q.all():
        alerts_by_org.setdefault(a.org_id, []).append(
            {"alert_type": a.alert_type, "severity": a.severity, "message": a.message}
        )

    results = []
    for b in budget_q.all():
        limit = float(b.limit_amount or 0)
        if limit <= 0:
            continue

        spent_q = db.query(func.coalesce(func.sum(DailyOrgSummary.total_cost), 0)).filter(
            DailyOrgSummary.org_id == b.org_id
        )
        if b.project_id:
            spent_q = spent_q.filter(DailyOrgSummary.project_id == b.project_id)
        if b.budget_type == "daily":
            spent_q = spent_q.filter(DailyOrgSummary.date == today)
        else:
            spent_q = spent_q.filter(DailyOrgSummary.date >= month_start)

        spent = float(spent_q.scalar() or 0)
        pct_used = round(spent / limit * 100, 1) if limit > 0 else 0.0
        threshold_pct = int(b.alert_threshold_percent or 80)

        forecast = None
        if b.budget_type != "daily" and days_elapsed > 0:
            velocity = spent / days_elapsed
            forecast = round(spent + velocity * days_remaining, 4)

        if pct_used >= 100:
            status = "exceeded"
        elif pct_used >= 90:
            status = "critical"
        elif pct_used >= threshold_pct:
            status = "warning"
        else:
            status = "ok"

        today_tokens = int(
            db.query(func.coalesce(func.sum(DailyOrgSummary.total_tokens), 0))
            .filter(DailyOrgSummary.org_id == b.org_id, DailyOrgSummary.date == today)
            .scalar()
            or 0
        )

        results.append(
            {
                "budget_id": b.id,
                "org_id": b.org_id,
                "project_id": b.project_id,
                "budget_type": b.budget_type or "monthly",
                "limit_amount": limit,
                "spent": round(spent, 4),
                "remaining": round(max(limit - spent, 0), 4),
                "pct_used": pct_used,
                "threshold_pct": threshold_pct,
                "forecast": forecast,
                "status": status,
                "today_tokens": today_tokens,
                "active_alerts": alerts_by_org.get(b.org_id, []),
            }
        )
    return results
