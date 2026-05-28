from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.core.visibility import active_orgs_subq
from app.models import (
    Alert,
    Budget,
    DailyOrgSummary,
    MonthlyOrgSummary,
    Organization,
    Project,
    TelemetryEvent,
    ToolRegistry,
)
from app.services.ai_model_pricing import calculate_token_cost

router = APIRouter(prefix="/costs", tags=["costs"])


def _pricing(model_name: str, input_tokens: int, output_tokens: int) -> dict:
    """Thin wrapper so all endpoints use the same call site."""
    return calculate_token_cost(model_name, input_tokens, output_tokens)


# Telemetry events ingested by the governance_logger decorator are stamped
# with an `event_id` of the form  "dec-<uuid>"  (see decorator/router.py
# `/decorator/ingest`). The cost module must surface ONLY those rows so
# manually-seeded / orphan test data never leaks into the dashboards.
DECORATOR_EVENT_FILTER = TelemetryEvent.event_id.like("dec-%")


def _scope(query, model, org_id, project_id=None, db=None):
    if org_id:
        query = query.filter(model.org_id == org_id)
    elif db is not None:
        query = query.filter(model.org_id.in_(active_orgs_subq(db)))
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
    else:
        rows = rows.filter(TelemetryEvent.org_id.in_(active_orgs_subq(db)))
    if project_id:
        rows = rows.filter(TelemetryEvent.project_id == project_id)

    results = []
    for r in rows.all():
        total = r.total_events or 0
        success_rate = Decimal("100") if total == 0 else (Decimal(str(r.success_count or 0)) / Decimal(str(total))) * Decimal("100")
        computed = _pricing(r.model_name or "", int(r.prompt_tokens or 0), int(r.completion_tokens or 0))
        results.append({
            "model_name": r.model_name,
            "provider": r.provider or "—",
            "total_events": total,
            "input_tokens": int(r.prompt_tokens or 0),
            "output_tokens": int(r.completion_tokens or 0),
            "prompt_tokens": int(r.prompt_tokens or 0),
            "completion_tokens": int(r.completion_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": computed["input_token_cost"],
            "output_token_cost": computed["output_token_cost"],
            "total_token_cost": computed["total_cost"],
            "total_cost": computed["total_cost"],
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 1),
            "success_rate": round(float(success_rate), 1),
        })
    return results


@router.get("/by-project")
def cost_by_project(
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Cost grouped by project, driven by the `projects` master table.
    Every project that exists in the DB is returned (with zeroed metrics
    if it has no telemetry yet); stale project_ids that exist only in
    telemetry but not in the `projects` table are ignored."""
    rows = (
        db.query(
            Project.id.label("project_id"),
            Project.org_id.label("org_id"),
            Project.project_name.label("project_name"),
            Project.environment.label("environment"),
            Organization.org_name.label("org_name"),
            func.count(TelemetryEvent.id).label("total_events"),
            func.coalesce(func.sum(TelemetryEvent.prompt_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(TelemetryEvent.completion_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(TelemetryEvent.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(TelemetryEvent.llm_cost), 0).label("llm_cost"),
            func.coalesce(func.sum(TelemetryEvent.infra_cost), 0).label("infra_cost"),
            func.coalesce(func.sum(TelemetryEvent.external_cost), 0).label("external_cost"),
            func.coalesce(func.sum(TelemetryEvent.total_cost), 0).label("total_cost"),
            func.avg(TelemetryEvent.latency_ms).label("avg_latency_ms"),
            func.count(TelemetryEvent.model_name.distinct()).label("tool_count"),
        )
        .outerjoin(
            TelemetryEvent,
            and_(TelemetryEvent.project_id == Project.id, DECORATOR_EVENT_FILTER),
        )
        .outerjoin(Organization, Organization.id == Project.org_id)
        .group_by(
            Project.id,
            Project.org_id,
            Project.project_name,
            Project.environment,
            Organization.org_name,
        )
        .order_by(func.coalesce(func.sum(TelemetryEvent.total_cost), 0).desc())
    )
    if org_id:
        rows = rows.filter(Project.org_id == org_id)

    # Per-(project, model) token aggregates — used for dynamic cost computation
    model_tokens_q = (
        db.query(
            TelemetryEvent.project_id,
            TelemetryEvent.model_name,
            func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
        )
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.project_id, TelemetryEvent.model_name)
    )
    if org_id:
        model_tokens_q = model_tokens_q.filter(TelemetryEvent.org_id == org_id)

    # project_id → {input_token_cost, output_token_cost, total_cost}
    project_dynamic_costs: dict = {}
    for mt in model_tokens_q.all():
        c = _pricing(mt.model_name, int(mt.input_tokens or 0), int(mt.output_tokens or 0))
        pid = mt.project_id or ""
        agg = project_dynamic_costs.setdefault(pid, {"input_token_cost": 0.0, "output_token_cost": 0.0, "total_cost": 0.0})
        agg["input_token_cost"] = round(agg["input_token_cost"] + c["input_token_cost"], 6)
        agg["output_token_cost"] = round(agg["output_token_cost"] + c["output_token_cost"], 6)
        agg["total_cost"] = round(agg["total_cost"] + c["total_cost"], 6)

    result = []
    for r in rows.all():
        pid = r.project_id or ""
        dc = project_dynamic_costs.get(pid, {"input_token_cost": 0.0, "output_token_cost": 0.0, "total_cost": 0.0})
        result.append({
            "project_id": r.project_id,
            "org_id": r.org_id,
            "org_name": r.org_name,
            "project_name": r.project_name,
            "environment": r.environment,
            "total_events": r.total_events or 0,
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": dc["input_token_cost"],
            "output_token_cost": dc["output_token_cost"],
            "total_token_cost": dc["total_cost"],
            "total_cost": dc["total_cost"],
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 1),
            "tool_count": int(r.tool_count or 0),
        })
    return result


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
            func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
        )
        .outerjoin(ToolRegistry, ToolRegistry.tool_name == TelemetryEvent.model_name)
        .filter(*filters)
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.model_name)
        .all()
    )

    tools = []
    dynamic_total = 0.0
    for r in tool_rows:
        c = _pricing(r.tool_name or "", int(r.input_tokens or 0), int(r.output_tokens or 0))
        dynamic_total += c["total_cost"]
        tools.append({
            "tool_name": r.tool_name,
            "vendor": r.vendor or "—",
            "cost_model": r.cost_model or "per_token",
            "total_events": r.total_events or 0,
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": c["input_token_cost"],
            "output_token_cost": c["output_token_cost"],
            "total_token_cost": c["total_cost"],
            "total_cost": c["total_cost"],
        })

    # Recompute cost_share_pct using dynamic totals
    for t in tools:
        t["cost_share_pct"] = round((t["total_cost"] / dynamic_total * 100), 1) if dynamic_total > 0 else 0.0

    # Sort by computed cost desc
    tools.sort(key=lambda x: x["total_cost"], reverse=True)

    return {
        "project_id": project_id,
        "org_id": org_id,
        "total_cost": round(dynamic_total, 6),
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
            func.sum(DailyOrgSummary.total_prompt_tokens).label("input_tokens"),
            func.sum(DailyOrgSummary.total_completion_tokens).label("output_tokens"),
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

    results = []
    for r in query.all():
        computed = _pricing(r.tool_name, int(r.input_tokens or 0), int(r.output_tokens or 0))
        results.append({
            "date": str(r.date),
            "tool_name": r.tool_name,
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": computed["input_token_cost"],
            "output_token_cost": computed["output_token_cost"],
            "total_token_cost": computed["total_cost"],
            "total_cost": computed["total_cost"],
            "total_events": int(r.total_events or 0),
        })
    return results


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
            func.sum(MonthlyOrgSummary.total_prompt_tokens).label("input_tokens"),
            func.sum(MonthlyOrgSummary.total_completion_tokens).label("output_tokens"),
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

    results = []
    for r in query.all():
        computed = _pricing(r.tool_name, int(r.input_tokens or 0), int(r.output_tokens or 0))
        results.append({
            "month": str(r.month),
            "tool_name": r.tool_name,
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": computed["input_token_cost"],
            "output_token_cost": computed["output_token_cost"],
            "total_token_cost": computed["total_cost"],
            "total_cost": computed["total_cost"],
            "total_events": int(r.total_events or 0),
        })
    return results


@router.get("/by-org")
def cost_by_org(db: Session = Depends(get_db)):
    """Cost grouped by organization, driven by the `organizations` master
    table. Every organization in the DB is returned (with zeroed metrics
    if no telemetry yet); stale org_ids that exist only in telemetry but
    not in the `organizations` table are ignored."""
    rows = (
        db.query(
            Organization.id.label("org_id"),
            Organization.org_name.label("org_name"),
            Organization.plan_type.label("plan_type"),
            func.count(TelemetryEvent.id).label("total_events"),
            func.coalesce(func.sum(TelemetryEvent.total_tokens), 0).label("total_tokens"),
            func.avg(TelemetryEvent.latency_ms).label("avg_latency_ms"),
        )
        .outerjoin(
            TelemetryEvent,
            and_(TelemetryEvent.org_id == Organization.id, DECORATOR_EVENT_FILTER),
        )
        .group_by(Organization.id, Organization.org_name, Organization.plan_type)
        .all()
    )

    # Dynamic costs: sub-query per (org_id, model_name) → compute via pricing service
    model_q = (
        db.query(
            TelemetryEvent.org_id,
            TelemetryEvent.model_name,
            func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
        )
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "", DECORATOR_EVENT_FILTER)
        .group_by(TelemetryEvent.org_id, TelemetryEvent.model_name)
        .all()
    )
    org_costs: dict = {}
    for mt in model_q:
        c = _pricing(mt.model_name, int(mt.input_tokens or 0), int(mt.output_tokens or 0))
        agg = org_costs.setdefault(mt.org_id or "", {"input_token_cost": 0.0, "output_token_cost": 0.0, "total_cost": 0.0})
        agg["input_token_cost"] = round(agg["input_token_cost"] + c["input_token_cost"], 6)
        agg["output_token_cost"] = round(agg["output_token_cost"] + c["output_token_cost"], 6)
        agg["total_cost"] = round(agg["total_cost"] + c["total_cost"], 6)

    result = []
    for r in rows:
        dc = org_costs.get(r.org_id or "", {"input_token_cost": 0.0, "output_token_cost": 0.0, "total_cost": 0.0})
        result.append({
            "org_id": r.org_id,
            "org_name": r.org_name,
            "plan_type": r.plan_type,
            "total_events": r.total_events or 0,
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": dc["input_token_cost"],
            "output_token_cost": dc["output_token_cost"],
            "total_cost": dc["total_cost"],
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 1),
        })
    result.sort(key=lambda x: x["total_cost"], reverse=True)
    return result


@router.get("/totals")
def cost_totals(db: Session = Depends(get_db)):
    today = date.today()
    first_of_month = today.replace(day=1)

    def _daily_totals(date_filter):
        rows = (
            db.query(
                DailyOrgSummary.tool_name,
                func.sum(DailyOrgSummary.total_prompt_tokens).label("input_tokens"),
                func.sum(DailyOrgSummary.total_completion_tokens).label("output_tokens"),
                func.sum(DailyOrgSummary.total_tokens).label("total_tokens"),
                func.sum(DailyOrgSummary.total_events).label("total_events"),
            )
            .filter(date_filter)
            .group_by(DailyOrgSummary.tool_name)
            .all()
        )
        cost = 0.0
        tokens = 0
        events = 0
        for r in rows:
            c = _pricing(r.tool_name or "", int(r.input_tokens or 0), int(r.output_tokens or 0))
            cost = round(cost + c["total_cost"], 6)
            tokens += int(r.total_tokens or 0)
            events += int(r.total_events or 0)
        return {"cost": cost, "tokens": tokens, "events": events}

    def _all_time_totals():
        rows = (
            db.query(
                TelemetryEvent.model_name,
                func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
                func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
                func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
                func.count(TelemetryEvent.id).label("total_events"),
            )
            .group_by(TelemetryEvent.model_name)
            .all()
        )
        cost = 0.0
        tokens = 0
        events = 0
        for r in rows:
            c = _pricing(r.model_name or "", int(r.input_tokens or 0), int(r.output_tokens or 0))
            cost = round(cost + c["total_cost"], 6)
            tokens += int(r.total_tokens or 0)
            events += int(r.total_events or 0)
        return {"cost": cost, "tokens": tokens, "events": events}

    return {
        "today": _daily_totals(DailyOrgSummary.date == today),
        "this_month": _daily_totals(DailyOrgSummary.date >= first_of_month),
        "all_time": _all_time_totals(),
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
            func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
        )
        .outerjoin(ToolRegistry, ToolRegistry.tool_name == TelemetryEvent.model_name)
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.model_name)
    )
    rows = _scope(rows, TelemetryEvent, org_id, project_id, db=db)

    # Build tool → distinct project_ids map from the same scope
    proj_q = (
        db.query(
            TelemetryEvent.model_name.label("tool_name"),
            TelemetryEvent.project_id,
        )
        .filter(
            TelemetryEvent.model_name.isnot(None),
            TelemetryEvent.model_name != "",
            TelemetryEvent.project_id.isnot(None),
            TelemetryEvent.project_id != "",
        )
        .distinct()
    )
    proj_q = _scope(proj_q, TelemetryEvent, org_id, project_id, db=db)
    tool_projects: dict = {}
    for pr in proj_q.all():
        tool_projects.setdefault(pr.tool_name, []).append(pr.project_id)

    results = []
    for r in rows.all():
        computed = _pricing(r.tool_name or "", int(r.input_tokens or 0), int(r.output_tokens or 0))
        results.append({
            "tool_name": r.tool_name,
            "vendor": r.vendor or "—",
            "cost_model": r.cost_model or "per_token",
            "total_events": r.total_events or 0,
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": computed["input_token_cost"],
            "output_token_cost": computed["output_token_cost"],
            "llm_cost": computed["total_cost"],
            "infra_cost": 0.0,
            "external_cost": 0.0,
            "total_token_cost": computed["total_cost"],
            "total_cost": computed["total_cost"],
            "project_ids": sorted(tool_projects.get(r.tool_name, [])),
        })
    results.sort(key=lambda x: x["total_cost"], reverse=True)
    return results


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
            func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
        )
        .group_by(TelemetryEvent.provider)
    )
    rows = _scope(rows, TelemetryEvent, org_id, project_id, db=db)

    # Sub-query: (provider, model_name) → compute per-model costs via pricing service
    model_q = (
        db.query(
            TelemetryEvent.provider,
            TelemetryEvent.model_name,
            func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
        )
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.provider, TelemetryEvent.model_name)
    )
    model_q = _scope(model_q, TelemetryEvent, org_id, project_id, db=db)
    provider_costs: dict = {}
    for mt in model_q.all():
        c = _pricing(mt.model_name, int(mt.input_tokens or 0), int(mt.output_tokens or 0))
        agg = provider_costs.setdefault(mt.provider or "", {"input_token_cost": 0.0, "output_token_cost": 0.0, "total_cost": 0.0})
        agg["input_token_cost"] = round(agg["input_token_cost"] + c["input_token_cost"], 6)
        agg["output_token_cost"] = round(agg["output_token_cost"] + c["output_token_cost"], 6)
        agg["total_cost"] = round(agg["total_cost"] + c["total_cost"], 6)

    results = []
    for r in rows.all():
        dc = provider_costs.get(r.provider or "", {"input_token_cost": 0.0, "output_token_cost": 0.0, "total_cost": 0.0})
        results.append({
            "provider": r.provider or "—",
            "total_events": r.total_events or 0,
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": dc["input_token_cost"],
            "output_token_cost": dc["output_token_cost"],
            "total_cost": dc["total_cost"],
        })
    results.sort(key=lambda x: x["total_cost"], reverse=True)
    return results


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
            func.avg(TelemetryEvent.latency_ms).label("avg_latency_ms"),
        )
        .group_by(TelemetryEvent.execution_type)
    )
    rows = _scope(rows, TelemetryEvent, org_id, project_id, db=db)

    # Sub-query: (execution_type, model_name) → dynamic costs
    model_q = (
        db.query(
            TelemetryEvent.execution_type,
            TelemetryEvent.model_name,
            func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
        )
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.execution_type, TelemetryEvent.model_name)
    )
    model_q = _scope(model_q, TelemetryEvent, org_id, project_id, db=db)
    exec_costs: dict = {}
    for mt in model_q.all():
        c = _pricing(mt.model_name, int(mt.input_tokens or 0), int(mt.output_tokens or 0))
        agg = exec_costs.setdefault(mt.execution_type or "", {"input_token_cost": 0.0, "output_token_cost": 0.0, "total_cost": 0.0})
        agg["input_token_cost"] = round(agg["input_token_cost"] + c["input_token_cost"], 6)
        agg["output_token_cost"] = round(agg["output_token_cost"] + c["output_token_cost"], 6)
        agg["total_cost"] = round(agg["total_cost"] + c["total_cost"], 6)

    results = []
    for r in rows.all():
        dc = exec_costs.get(r.execution_type or "", {"input_token_cost": 0.0, "output_token_cost": 0.0, "total_cost": 0.0})
        results.append({
            "execution_type": r.execution_type or "—",
            "total_events": r.total_events or 0,
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": dc["input_token_cost"],
            "output_token_cost": dc["output_token_cost"],
            "total_cost": dc["total_cost"],
            "avg_latency_ms": round(float(r.avg_latency_ms or 0), 1),
        })
    results.sort(key=lambda x: x["total_cost"], reverse=True)
    return results


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
        )
        .group_by(TelemetryEvent.service_type)
    )
    rows = _scope(rows, TelemetryEvent, org_id, project_id, db=db)

    # Sub-query: (service_type, model_name) → dynamic costs
    model_q = (
        db.query(
            TelemetryEvent.service_type,
            TelemetryEvent.model_name,
            func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
        )
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.service_type, TelemetryEvent.model_name)
    )
    model_q = _scope(model_q, TelemetryEvent, org_id, project_id, db=db)
    svc_costs: dict = {}
    for mt in model_q.all():
        c = _pricing(mt.model_name, int(mt.input_tokens or 0), int(mt.output_tokens or 0))
        agg = svc_costs.setdefault(mt.service_type or "", {"input_token_cost": 0.0, "output_token_cost": 0.0, "total_cost": 0.0})
        agg["input_token_cost"] = round(agg["input_token_cost"] + c["input_token_cost"], 6)
        agg["output_token_cost"] = round(agg["output_token_cost"] + c["output_token_cost"], 6)
        agg["total_cost"] = round(agg["total_cost"] + c["total_cost"], 6)

    results = []
    for r in rows.all():
        dc = svc_costs.get(r.service_type or "", {"input_token_cost": 0.0, "output_token_cost": 0.0, "total_cost": 0.0})
        results.append({
            "service_type": r.service_type or "—",
            "total_events": r.total_events or 0,
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": dc["input_token_cost"],
            "output_token_cost": dc["output_token_cost"],
            "total_cost": dc["total_cost"],
        })
    results.sort(key=lambda x: x["total_cost"], reverse=True)
    return results


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
        func.sum(TelemetryEvent.infra_cost).label("infra_cost"),
        func.sum(TelemetryEvent.external_cost).label("external_cost"),
        func.count(TelemetryEvent.id).label("total_events"),
        func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
    )
    q = _scope(q, TelemetryEvent, org_id, project_id, db=db)
    row = q.first()

    infra = 0.0
    external = float(row.external_cost or 0) if row else 0.0
    events = int(row.total_events or 0) if row else 0
    tokens = int(row.total_tokens or 0) if row else 0

    # LLM cost computed dynamically via the pricing service (never from DB)
    model_q = (
        db.query(
            TelemetryEvent.model_name,
            func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
        )
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.model_name)
    )
    model_q = _scope(model_q, TelemetryEvent, org_id, project_id, db=db)
    llm = 0.0
    for mt in model_q.all():
        c = _pricing(mt.model_name, int(mt.input_tokens or 0), int(mt.output_tokens or 0))
        llm = round(llm + c["total_cost"], 6)

    total = round(llm + infra + external, 6)
    pct = lambda v: round((v / total * 100), 2) if total > 0 else 0.0

    return {
        "components": [
            {
                "name": "llm_cost",
                "amount": round(llm, 6),
                "percent": pct(llm),
                "formula": "(prompt_tokens × input_rate + completion_tokens × output_rate) ÷ 1,000,000",
            },
            {
                "name": "infra_cost",
                "amount": 0.0,
                "percent": 0.0,
                "formula": "not calculated",
            },
            {
                "name": "external_cost",
                "amount": round(external, 6),
                "percent": pct(external),
                "formula": "Σ external_tools[i].cost (passed through, no mark-up)",
            },
        ],
        "total_cost": total,
        "total_events": events,
        "total_tokens": tokens,
        "avg_cost_per_event": round(total / events, 6) if events else 0.0,
        "avg_cost_per_1k_tokens": round((total / tokens) * 1000, 6) if tokens else 0.0,
        "formula": "total_cost = llm_cost + external_cost",
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
            func.sum(DailyOrgSummary.total_prompt_tokens).label("input_tokens"),
            func.sum(DailyOrgSummary.total_completion_tokens).label("output_tokens"),
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

    rows = []
    for r in query.all():
        in_tok = int(r.input_tokens or 0)
        out_tok = int(r.output_tokens or 0)
        computed = _pricing(r.tool_name, in_tok, out_tok)
        rows.append({
            "date": str(r.date),
            "tool_name": r.tool_name,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": int(r.total_tokens or 0),
            "input_token_cost": computed["input_token_cost"],
            "output_token_cost": computed["output_token_cost"],
            "llm_cost": computed["total_cost"],
            "infra_cost": 0.0,
            "external_cost": 0.0,
            "total_token_cost": computed["total_cost"],
            "total_cost": computed["total_cost"],
            "total_events": int(r.total_events or 0),
            "email_volume": email_map.get((str(r.date), r.tool_name), 0),
        })
    # Re-sort by computed total_cost desc after pricing applied
    rows.sort(key=lambda x: (x["date"], -x["total_cost"]))
    return rows


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

        # Recompute spend dynamically via the pricing service — never trust the stored total_cost
        spent_model_q = (
            db.query(
                DailyOrgSummary.tool_name,
                func.coalesce(func.sum(DailyOrgSummary.total_prompt_tokens), 0).label("input_tokens"),
                func.coalesce(func.sum(DailyOrgSummary.total_completion_tokens), 0).label("output_tokens"),
            )
            .filter(DailyOrgSummary.org_id == b.org_id)
            .group_by(DailyOrgSummary.tool_name)
        )
        if b.project_id:
            spent_model_q = spent_model_q.filter(DailyOrgSummary.project_id == b.project_id)
        if b.budget_type == "daily":
            spent_model_q = spent_model_q.filter(DailyOrgSummary.date == today)
        else:
            spent_model_q = spent_model_q.filter(DailyOrgSummary.date >= month_start)

        spent = 0.0
        for sr in spent_model_q.all():
            c = _pricing(sr.tool_name or "", int(sr.input_tokens or 0), int(sr.output_tokens or 0))
            spent = round(spent + c["total_cost"], 6)
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
