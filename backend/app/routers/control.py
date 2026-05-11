"""Control Router — vendor-agnostic ingestion endpoints.

POST /control/ingest              → single structured event from any vendor
POST /control/ingest/batch        → batch of events
POST /control/ingest/trace        → unified multi-model / multi-tool trace event
GET  /control/quota/{org_id}      → token quota + budget status with velocity forecast
GET  /control/project/{project_id}/trace → unified project-level trace
GET  /control/trace/{trace_id}    → full detail for a single unified trace
GET  /control/cost-breakdown      → cost breakdown per tool/model/project/org
"""
from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_api_key
from app.models import (
    Budget,
    DailyOrgSummary,
    Organization,
    Project,
    RateLimit,
    TelemetryEvent,
    TraceModelUsage,
    TraceToolUsage,
)
from app.routers.telemetry import _ingest_event
from app.schemas import ModelUsageItem, ToolUsageItemEnhanced
from app.services.control_ingest import SDKEvent, sdk_ingest_service, unified_trace_processor

router = APIRouter(prefix="/control", tags=["control"])


# ─────────────────────── Pydantic request schemas ───────────────────────

class ToolUsageItem(BaseModel):
    name: str
    cost: Optional[float] = None


class ControlIngestRequest(BaseModel):
    org_id: str
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    provider: str
    model_name: Optional[str] = None
    tool_name: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_per_call: Optional[float] = None
    input_cost_per_1k: Optional[float] = None
    output_cost_per_1k: Optional[float] = None
    tool_usages: list[ToolUsageItem] = Field(default_factory=list)
    latency_ms: int = 0
    status: str = "success"
    trace_id: Optional[str] = None
    service_type: Optional[str] = None
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    stages: list[dict[str, Any]] = Field(default_factory=list)
    contains_pii: bool = False
    pii_type: Optional[str] = None
    data_out_violation: bool = False
    input_data_size_mb: float = 0.0
    output_data_size_mb: float = 0.0
    input_preview: Optional[str] = None
    output_preview: Optional[str] = None
    event_id: Optional[str] = None


class BatchControlIngestRequest(BaseModel):
    events: list[ControlIngestRequest]


class UnifiedTraceRequest(BaseModel):
    """
    Single event capturing multiple models and multiple tools used in one workflow.

    Models and tools are individually priced via DB lookups (model_pricing →
    tool_registry) or caller-supplied overrides. Aggregated totals are stored on
    the parent telemetry_event; per-model and per-tool rows go into
    trace_model_usage / trace_tool_usage for drill-down queries.
    """
    org_id: str
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    trace_id: Optional[str] = None
    workflow_name: Optional[str] = None
    status: str = "success"
    models: list[ModelUsageItem] = Field(default_factory=list)
    tools: list[ToolUsageItemEnhanced] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    contains_pii: bool = False
    pii_type: Optional[str] = None
    data_out_violation: bool = False
    input_data_size_mb: float = 0.0
    output_data_size_mb: float = 0.0
    event_id: Optional[str] = None


# ─────────────────────── helpers ───────────────────────

def _build_sdk_event(item: ControlIngestRequest) -> SDKEvent:
    return SDKEvent(
        org_id=item.org_id,
        project_id=item.project_id,
        user_id=item.user_id,
        provider=item.provider,
        model_name=item.model_name,
        tool_name=item.tool_name,
        input_tokens=item.input_tokens,
        output_tokens=item.output_tokens,
        cost_per_call=item.cost_per_call,
        input_cost_per_1k=item.input_cost_per_1k,
        output_cost_per_1k=item.output_cost_per_1k,
        tool_usages=[t.model_dump() for t in item.tool_usages],
        latency_ms=item.latency_ms,
        status=item.status,
        trace_id=item.trace_id,
        service_type=item.service_type,
        tags=item.tags,
        metadata=item.metadata,
        stages=item.stages,
        contains_pii=item.contains_pii,
        pii_type=item.pii_type,
        data_out_violation=item.data_out_violation,
        input_data_size_mb=item.input_data_size_mb,
        output_data_size_mb=item.output_data_size_mb,
        input_preview=item.input_preview,
        output_preview=item.output_preview,
        event_id=item.event_id,
    )


# ─────────────────────── Endpoints ───────────────────────

@router.post("/ingest")
def control_ingest(
    payload: ControlIngestRequest,
    db: Session = Depends(get_db),
    _: Any = Depends(require_api_key),
):
    """Ingest a single event from any AI vendor via the control plane."""
    sdk_event = _build_sdk_event(payload)
    telemetry_create = sdk_ingest_service.to_telemetry(sdk_event, db)
    event = _ingest_event(db, telemetry_create)
    db.commit()
    return {
        "status": "ingested",
        "event_id": event.event_id,
        "total_cost": float(event.total_cost or 0),
        "llm_cost": float(event.llm_cost or 0),
        "infra_cost": float(event.infra_cost or 0),
        "external_cost": float(event.external_cost or 0),
        "total_tokens": event.total_tokens or 0,
        "input_tokens": event.prompt_tokens or 0,
        "output_tokens": event.completion_tokens or 0,
    }


@router.post("/ingest/batch")
def control_ingest_batch(
    payload: BatchControlIngestRequest,
    db: Session = Depends(get_db),
    _: Any = Depends(require_api_key),
):
    """Ingest a batch of events from any AI vendor via the control plane."""
    results = []
    for item in payload.events:
        sdk_event = _build_sdk_event(item)
        try:
            telemetry_create = sdk_ingest_service.to_telemetry(sdk_event, db)
            event = _ingest_event(db, telemetry_create)
            results.append({
                "event_id": event.event_id,
                "status": "ingested",
                "total_cost": float(event.total_cost or 0),
                "total_tokens": event.total_tokens or 0,
            })
        except HTTPException as exc:
            db.rollback()
            results.append({"event_id": sdk_event.event_id, "status": "error", "detail": exc.detail})
        except Exception as exc:
            db.rollback()
            results.append({"event_id": sdk_event.event_id, "status": "error", "detail": str(exc)})
    db.commit()
    return {
        "total": len(results),
        "ingested": sum(1 for r in results if r["status"] == "ingested"),
        "errors": sum(1 for r in results if r["status"] == "error"),
        "results": results,
    }


@router.post("/ingest/trace")
def control_ingest_unified_trace(
    payload: UnifiedTraceRequest,
    db: Session = Depends(get_db),
    _: Any = Depends(require_api_key),
):
    """
    Ingest a unified trace event capturing multiple models and tools in one call.

    Stores aggregated totals on the parent telemetry event and per-model /
    per-tool rows in trace_model_usage / trace_tool_usage for drill-down queries.
    """
    result = unified_trace_processor.process(payload, db)
    event = _ingest_event(db, result.telemetry_create)
    for record in result.model_records:
        db.add(record)
    for record in result.tool_records:
        db.add(record)
    db.commit()

    return {
        "status": "ingested",
        "event_id": event.event_id,
        "trace_id": event.trace_id,
        "workflow_name": payload.workflow_name,
        "total_tokens": event.total_tokens or 0,
        "total_input_tokens": event.prompt_tokens or 0,
        "total_output_tokens": event.completion_tokens or 0,
        "total_cost": round(float(event.total_cost or 0), 6),
        "total_llm_cost": round(float(event.llm_cost or 0), 6),
        "total_tool_cost": round(float(event.external_cost or 0), 6),
        "model_count": len(result.model_records),
        "tool_count": len(result.tool_records),
    }


@router.get("/trace/{trace_id}")
def get_unified_trace(
    trace_id: str,
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Full detail for a single unified trace: parent event + per-model + per-tool breakdown.
    Works for both unified traces and single-model events.
    """
    q = db.query(TelemetryEvent).filter(TelemetryEvent.trace_id == trace_id)
    if org_id:
        q = q.filter(TelemetryEvent.org_id == org_id)
    event = q.first()
    if not event:
        raise HTTPException(status_code=404, detail="Trace not found")

    model_rows = (
        db.query(TraceModelUsage)
        .filter(TraceModelUsage.trace_id == trace_id)
        .order_by(TraceModelUsage.id)
        .all()
    )
    tool_rows = (
        db.query(TraceToolUsage)
        .filter(TraceToolUsage.trace_id == trace_id)
        .order_by(TraceToolUsage.id)
        .all()
    )

    meta = event.metadata_json or {}
    return {
        "event_id": event.event_id,
        "trace_id": event.trace_id,
        "org_id": event.org_id,
        "project_id": event.project_id,
        "workflow_name": meta.get("workflow_name") or event.component_name,
        "status": event.status,
        "is_unified_trace": bool(meta.get("is_unified_trace", False)),
        "total_input_tokens": event.prompt_tokens or 0,
        "total_output_tokens": event.completion_tokens or 0,
        "total_tokens": event.total_tokens or 0,
        "total_cost": round(float(event.total_cost or 0), 6),
        "total_llm_cost": round(float(event.llm_cost or 0), 6),
        "total_tool_cost": round(float(event.external_cost or 0), 6),
        "infra_cost": round(float(event.infra_cost or 0), 6),
        "total_execution_time_ms": event.latency_ms or 0,
        "risk_score": round(float(event.risk_score or 0), 2),
        "model_count": len(model_rows),
        "tool_count": len(tool_rows),
        "models": [
            {
                "model_name": m.model_name,
                "provider": m.provider,
                "input_tokens": m.input_tokens or 0,
                "output_tokens": m.output_tokens or 0,
                "total_tokens": m.total_tokens or 0,
                "cost": round(float(m.llm_cost or 0), 6),
                "latency_ms": m.latency_ms or 0,
            }
            for m in model_rows
        ],
        "tools": [
            {
                "tool_name": t.tool_name,
                "tool_type": t.tool_type,
                "invocation_count": t.invocation_count or 1,
                "execution_time_ms": t.execution_time_ms or 0,
                "cost": round(float(t.cost or 0), 6),
            }
            for t in tool_rows
        ],
        "tags": event.tags or [],
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


@router.get("/quota/{org_id}")
def get_quota_status(
    org_id: str,
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Token quota and budget status with velocity-based end-of-month forecast.
    All thresholds come from the budgets table — zero hardcoding.
    """
    today = date.today()
    month_start = today.replace(day=1)

    def _filter_scope(q):
        if project_id:
            return q.filter(DailyOrgSummary.project_id == project_id)
        return q

    month_q = _filter_scope(
        db.query(
            func.coalesce(func.sum(DailyOrgSummary.total_cost), 0).label("cost"),
            func.coalesce(func.sum(DailyOrgSummary.total_tokens), 0).label("tokens"),
            func.coalesce(func.sum(DailyOrgSummary.total_events), 0).label("events"),
        ).filter(
            DailyOrgSummary.org_id == org_id,
            DailyOrgSummary.date >= month_start,
            DailyOrgSummary.date <= today,
        )
    )
    month = month_q.first()

    today_q = _filter_scope(
        db.query(
            func.coalesce(func.sum(DailyOrgSummary.total_cost), 0).label("cost"),
            func.coalesce(func.sum(DailyOrgSummary.total_tokens), 0).label("tokens"),
        ).filter(DailyOrgSummary.org_id == org_id, DailyOrgSummary.date == today)
    )
    today_row = today_q.first()

    budget_q = db.query(Budget).filter(Budget.org_id == org_id)
    if project_id:
        budget_q = budget_q.filter(Budget.project_id == project_id)
    else:
        budget_q = budget_q.filter(Budget.project_id.is_(None))
    budget = budget_q.first()

    rate_limit = db.query(RateLimit).filter(RateLimit.org_id == org_id).first()

    month_cost = float(month.cost or 0)
    month_tokens = int(month.tokens or 0)
    today_cost = float(today_row.cost or 0)
    today_tokens = int(today_row.tokens or 0)

    limit_amount = float(budget.limit_amount or 0) if budget else None
    threshold_pct = int(budget.alert_threshold_percent or 80) if budget else 80
    token_quota = int(rate_limit.max_tokens_per_day or 0) if rate_limit else None

    days_elapsed = max((today - month_start).days + 1, 1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    days_remaining = days_in_month - today.day

    daily_velocity_cost = month_cost / days_elapsed
    daily_velocity_tokens = month_tokens / days_elapsed
    forecast_cost = month_cost + daily_velocity_cost * days_remaining
    forecast_tokens = month_tokens + daily_velocity_tokens * days_remaining

    usage_pct = round(month_cost / limit_amount * 100, 1) if limit_amount else None
    forecast_pct = round(forecast_cost / limit_amount * 100, 1) if limit_amount else None
    token_usage_pct = round(today_tokens / token_quota * 100, 1) if token_quota else None

    return {
        "org_id": org_id,
        "project_id": project_id,
        "period": str(month_start),
        "month_cost": round(month_cost, 4),
        "month_tokens": month_tokens,
        "today_cost": round(today_cost, 4),
        "today_tokens": today_tokens,
        "budget_limit": limit_amount,
        "alert_threshold_percent": threshold_pct,
        "usage_percent": usage_pct,
        "cost_remaining": round(limit_amount - month_cost, 4) if limit_amount else None,
        "daily_velocity_cost": round(daily_velocity_cost, 6),
        "daily_velocity_tokens": round(daily_velocity_tokens, 1),
        "forecast_month_cost": round(forecast_cost, 4),
        "forecast_month_tokens": int(forecast_tokens),
        "forecast_usage_percent": forecast_pct,
        "will_exceed_budget": (forecast_cost > limit_amount) if limit_amount else False,
        "days_remaining_in_month": days_remaining,
        "token_quota_daily": token_quota,
        "token_quota_used_today": today_tokens,
        "token_quota_percent": token_usage_pct,
    }


@router.get("/project/{project_id}/trace")
def get_project_trace(
    project_id: str,
    org_id: Optional[str] = Query(None),
    limit: int = Query(200, le=500),
    db: Session = Depends(get_db),
):
    """
    Unified project-level trace — all events, models, and tools in a single view.
    Aggregates input/output tokens and cost per model and per tool.
    """
    query = db.query(TelemetryEvent).filter(TelemetryEvent.project_id == project_id)
    if org_id:
        query = query.filter(TelemetryEvent.org_id == org_id)
    events = query.order_by(TelemetryEvent.created_at.desc()).limit(limit).all()

    model_agg: dict[str, dict] = {}
    tool_agg: dict[str, dict] = {}

    for e in events:
        mkey = e.model_name or "unknown"
        if mkey not in model_agg:
            model_agg[mkey] = {
                "model": mkey, "provider": e.provider, "events": 0,
                "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "total_cost": 0.0, "avg_latency_ms": 0.0,
            }
        ma = model_agg[mkey]
        ma["events"] += 1
        ma["input_tokens"] += e.prompt_tokens or 0
        ma["output_tokens"] += e.completion_tokens or 0
        ma["total_tokens"] += e.total_tokens or 0
        ma["total_cost"] += float(e.total_cost or 0)
        ma["avg_latency_ms"] = (
            (ma["avg_latency_ms"] * (ma["events"] - 1) + (e.latency_ms or 0)) / ma["events"]
        )

    tool_rows = (
        db.query(TraceToolUsage)
        .filter(TraceToolUsage.project_id == project_id)
        .all()
    )
    for t in tool_rows:
        tkey = t.tool_name
        if tkey not in tool_agg:
            tool_agg[tkey] = {
                "tool_name": tkey, "tool_type": t.tool_type,
                "total_invocations": 0, "total_execution_time_ms": 0,
                "total_cost": 0.0, "event_count": 0,
            }
        ta = tool_agg[tkey]
        ta["total_invocations"] += t.invocation_count or 1
        ta["total_execution_time_ms"] += t.execution_time_ms or 0
        ta["total_cost"] += float(t.cost or 0)
        ta["event_count"] += 1

    total_events = len(events)
    total_input = sum(e.prompt_tokens or 0 for e in events)
    total_output = sum(e.completion_tokens or 0 for e in events)
    total_tokens = sum(e.total_tokens or 0 for e in events)
    total_cost = round(sum(float(e.total_cost or 0) for e in events), 6)
    unified_trace_count = sum(
        1 for e in events
        if isinstance(e.metadata_json, dict) and e.metadata_json.get("is_unified_trace")
    )

    return {
        "project_id": project_id,
        "org_id": org_id,
        "total_events": total_events,
        "unified_trace_count": unified_trace_count,
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_tokens": total_tokens,
        "total_cost": total_cost,
        "model_breakdown": [
            {**v, "total_cost": round(v["total_cost"], 6), "avg_latency_ms": round(v["avg_latency_ms"], 1)}
            for v in model_agg.values()
        ],
        "tool_breakdown": [
            {**v, "total_cost": round(v["total_cost"], 6)}
            for v in tool_agg.values()
        ],
        "events": [
            {
                "event_id": e.event_id,
                "trace_id": e.trace_id,
                "model_name": e.model_name,
                "provider": e.provider,
                "workflow_name": (e.metadata_json or {}).get("workflow_name"),
                "is_unified_trace": bool((e.metadata_json or {}).get("is_unified_trace", False)),
                "model_count": (e.metadata_json or {}).get("model_count"),
                "tool_count": (e.metadata_json or {}).get("tool_count"),
                "input_tokens": e.prompt_tokens or 0,
                "output_tokens": e.completion_tokens or 0,
                "total_tokens": e.total_tokens or 0,
                "total_cost": round(float(e.total_cost or 0), 6),
                "llm_cost": round(float(e.llm_cost or 0), 6),
                "status": e.status,
                "latency_ms": e.latency_ms or 0,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in events
        ],
    }


@router.get("/resolve")
def resolve_org_project(
    org_name: Optional[str] = Query(None),
    project_name: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Resolve human-readable org/project names to their stable database IDs.

    Used by the GovernanceSDK at initialisation time so callers can use
    ``org_name`` / ``project_name`` instead of raw UUIDs.

    GET /control/resolve?org_name=Acme+Corp&project_name=chatbot-prod
    → { "org_id": "...", "project_id": "...", "org_name": "...", "project_name": "..." }
    """
    org_id: Optional[str] = None
    project_id: Optional[str] = None
    resolved_org_name: Optional[str] = None
    resolved_project_name: Optional[str] = None

    if org_name:
        org = (
            db.query(Organization)
            .filter(func.lower(Organization.org_name) == org_name.strip().lower())
            .first()
        )
        if org:
            org_id = org.id
            resolved_org_name = org.org_name
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Organization '{org_name}' not found.",
            )

    if project_name and org_id:
        proj = (
            db.query(Project)
            .filter(
                Project.org_id == org_id,
                func.lower(Project.project_name) == project_name.strip().lower(),
            )
            .first()
        )
        if proj:
            project_id = proj.id
            resolved_project_name = proj.project_name
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Project '{project_name}' not found in org '{org_name}'.",
            )

    return {
        "org_id": org_id,
        "project_id": project_id,
        "org_name": resolved_org_name,
        "project_name": resolved_project_name,
    }

@router.get("/notifications/status")
def get_notification_status():
    """Notification channel configuration status — no credentials exposed."""
    import os

    smtp_host = os.getenv("SMTP_HOST", "")
    notification_emails = [e.strip() for e in os.getenv("NOTIFICATION_EMAIL", "").split(",") if e.strip()]
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_from = os.getenv("TWILIO_WHATSAPP_FROM", "")
    twilio_to = [n.strip() for n in os.getenv("TWILIO_WHATSAPP_TO", "").split(",") if n.strip()]
    teams_webhooks = [u.strip() for u in os.getenv("TEAMS_WEBHOOK_URLS", "").split(",") if u.strip()]

    email_ok = bool(smtp_host and notification_emails)
    wa_ok = bool(twilio_sid and twilio_from and twilio_to)
    teams_ok = bool(teams_webhooks)

    return {
        "channels": {
            "dashboard": {
                "enabled": True,
                "status": "active",
                "description": "Real-time alerts shown in the dashboard. Always active.",
            },
            "email": {
                "enabled": email_ok,
                "status": "active" if email_ok else "not_configured",
                "recipients": len(notification_emails),
                "smtp_host_set": bool(smtp_host),
                "description": "SMTP email notifications for high/critical alerts.",
                "config_vars": ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "NOTIFICATION_EMAIL"],
            },
            "whatsapp": {
                "enabled": wa_ok,
                "status": "active" if wa_ok else "not_configured",
                "recipients": len(twilio_to),
                "twilio_configured": bool(twilio_sid),
                "description": "WhatsApp notifications via Twilio for high/critical alerts.",
                "config_vars": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "TWILIO_WHATSAPP_TO"],
            },
            "teams": {
                "enabled": teams_ok,
                "status": "active" if teams_ok else "not_configured",
                "webhooks": len(teams_webhooks),
                "description": "Microsoft Teams notifications via Incoming Webhooks for high/critical alerts.",
                "config_vars": ["TEAMS_WEBHOOK_URLS"],
            },
        },
        "alert_severities_notified": ["critical", "high"],
        "note": "Email, WhatsApp, and Teams fire automatically for 'critical' and 'high' severity only. All severities appear in the dashboard.",
    }


@router.get("/cost-breakdown")
def get_cost_breakdown(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Cost breakdown aggregated per model/tool, scoped by org and/or project."""
    q = db.query(
        TelemetryEvent.org_id,
        TelemetryEvent.project_id,
        TelemetryEvent.model_name.label("model"),
        TelemetryEvent.provider,
        func.count(TelemetryEvent.id).label("events"),
        func.sum(TelemetryEvent.prompt_tokens).label("input_tokens"),
        func.sum(TelemetryEvent.completion_tokens).label("output_tokens"),
        func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
        func.sum(TelemetryEvent.llm_cost).label("llm_cost"),
        func.sum(TelemetryEvent.infra_cost).label("infra_cost"),
        func.sum(TelemetryEvent.external_cost).label("external_cost"),
        func.sum(TelemetryEvent.total_cost).label("total_cost"),
    ).group_by(
        TelemetryEvent.org_id,
        TelemetryEvent.project_id,
        TelemetryEvent.model_name,
        TelemetryEvent.provider,
    )

    if org_id:
        q = q.filter(TelemetryEvent.org_id == org_id)
    if project_id:
        q = q.filter(TelemetryEvent.project_id == project_id)

    rows = q.order_by(func.sum(TelemetryEvent.total_cost).desc()).all()

    return [
        {
            "org_id": r.org_id,
            "project_id": r.project_id,
            "model": r.model or "unknown",
            "provider": r.provider or "unknown",
            "events": int(r.events or 0),
            "input_tokens": int(r.input_tokens or 0),
            "output_tokens": int(r.output_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "llm_cost": round(float(r.llm_cost or 0), 6),
            "infra_cost": round(float(r.infra_cost or 0), 6),
            "external_cost": round(float(r.external_cost or 0), 6),
            "total_cost": round(float(r.total_cost or 0), 6),
        }
        for r in rows
    ]
