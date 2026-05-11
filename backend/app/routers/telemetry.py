from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import Alert, Budget, CostBreakdown, DataSecurityLog, DailyOrgSummary, ExecutionPipeline, Organization, Project, RateLimit, TelemetryEvent, ToolConnector, ToolRegistry, UsageAnomaly
from app.schemas import (
    BatchTelemetryIngest,
    CostBreakdownResponse,
    TelemetryEventCreate,
    TelemetryEventResponse,
    TelemetryEventUpdate,
    TraceDetailResponse,
)
from app.services.alert_engine import AlertEngine
from app.services.cost_engine import CostEngine
from app.services.langfuse_bridge import mirror_event as _langfuse_mirror_event
from app.services.security_engine import SecurityEngine

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

cost_engine = CostEngine()
security_engine = SecurityEngine()
alert_engine = AlertEngine()


def _json_safe(value: Any) -> Any:
    """Recursively convert any value to JSON-serializable types.

    Handles datetime/date → ISO string, Decimal → float, and nested
    dicts/lists.  Called before storing any JSON column so PostgreSQL
    never receives a non-serializable Python object.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


@router.post("/event", response_model=TelemetryEventResponse)
def create_event(event_data: TelemetryEventCreate, db: Session = Depends(get_db)):
    event = _ingest_event(db, event_data)
    db.commit()
    return _build_event_response(db, event)


@router.post("/events/batch")
def ingest_events_batch(batch: BatchTelemetryIngest, db: Session = Depends(get_db)):
    ingested = []
    for event_data in batch.events:
        event = _ingest_event(db, event_data)
        ingested.append(event.event_id)
    db.commit()
    return {"status": "completed", "ingested_count": len(ingested), "event_ids": ingested}


@router.get("/logs", response_model=list[TelemetryEventResponse])
def list_telemetry_logs(
    org_id: Optional[str] = Query(None),
    tool_name: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    limit: int = Query(50, le=500),
    db: Session = Depends(get_db),
):
    query = db.query(TelemetryEvent)
    if org_id:
        query = query.filter(TelemetryEvent.org_id == org_id)
    if tool_name:
        query = query.filter(TelemetryEvent.model_name == tool_name)
    if provider:
        query = query.filter(TelemetryEvent.provider == provider)
    if status:
        query = query.filter(TelemetryEvent.status == status)
    if start_date:
        query = query.filter(TelemetryEvent.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.filter(TelemetryEvent.created_at <= datetime.combine(end_date, datetime.max.time()))

    rows = query.order_by(TelemetryEvent.created_at.desc()).limit(limit).all()
    return [_build_event_response(db, row) for row in rows]


@router.get("/admin/logs")
def super_admin_logs(
    org_id: Optional[str] = Query(None),
    tool_name: Optional[str] = Query(None),
    provider: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    end_date: Optional[date] = Query(None),
    limit: int = Query(200, le=1000),
    db: Session = Depends(get_db),
):
    """Super-admin centralised log access across every integrated AI tool.

    Returns redacted log records (no raw payloads, no code) for monitoring,
    auditing and compliance.
    """
    query = db.query(TelemetryEvent)
    if org_id:
        query = query.filter(TelemetryEvent.org_id == org_id)
    if tool_name:
        query = query.filter(TelemetryEvent.model_name == tool_name)
    if provider:
        query = query.filter(TelemetryEvent.provider == provider)
    if status:
        query = query.filter(TelemetryEvent.status == status)
    if start_date:
        query = query.filter(TelemetryEvent.created_at >= datetime.combine(start_date, datetime.min.time()))
    if end_date:
        query = query.filter(TelemetryEvent.created_at <= datetime.combine(end_date, datetime.max.time()))

    rows = query.order_by(TelemetryEvent.created_at.desc()).limit(limit).all()
    event_ids = [r.event_id for r in rows]

    # Batch-fetch security logs and org/project names for full PII context
    security_map = {
        sl.event_id: sl
        for sl in db.query(DataSecurityLog).filter(DataSecurityLog.event_id.in_(event_ids)).all()
    } if event_ids else {}

    org_ids = list({r.org_id for r in rows if r.org_id})
    project_ids = list({r.project_id for r in rows if r.project_id})
    org_name_map = {o.id: o.org_name for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()} if org_ids else {}
    project_name_map = {p.id: p.project_name for p in db.query(Project).filter(Project.id.in_(project_ids)).all()} if project_ids else {}

    return [
        {
            "event_id": row.event_id,
            "created_at": row.created_at,
            "org_id": row.org_id,
            "org_name": org_name_map.get(row.org_id, row.org_id),
            "project_id": row.project_id,
            "project_name": project_name_map.get(row.project_id, row.project_id),
            "user_id": row.user_id,
            "provider": row.provider,
            "tool_name": row.model_name,
            "service_type": row.service_type,
            "status": row.status,
            "latency_ms": row.latency_ms,
            "total_tokens": row.total_tokens,
            "total_cost": float(row.total_cost or 0),
            "risk_score": float(row.risk_score or 0),
            "misuse_detected": bool(row.misuse_detected),
            "abnormal_usage_spike": bool(row.abnormal_usage_spike),
            "pii_detected": bool(security_map[row.event_id].pii_detected) if row.event_id in security_map else False,
            "pii_type": security_map[row.event_id].pii_type if row.event_id in security_map else None,
        }
        for row in rows
    ]


@router.get("/admin/aggregate")
def super_admin_aggregate(
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Aggregated token usage, cost, and budget remaining per org/tool.

    Sourced entirely from tracing data — single source of truth for the
    Super Admin Log Module's centralized cost and usage computation.
    """
    query = (
        db.query(
            TelemetryEvent.org_id,
            TelemetryEvent.model_name.label("tool_name"),
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.prompt_tokens).label("prompt_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("completion_tokens"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
            func.avg(TelemetryEvent.risk_score).label("avg_risk_score"),
        )
        .group_by(TelemetryEvent.org_id, TelemetryEvent.model_name)
        .order_by(TelemetryEvent.org_id, TelemetryEvent.model_name)
    )
    if org_id:
        query = query.filter(TelemetryEvent.org_id == org_id)

    rows = query.all()

    # First budget per org (project-level budgets excluded for org-wide view)
    budgets: dict = {}
    for b in db.query(Budget).filter(Budget.project_id.is_(None)).all():
        if b.org_id not in budgets:
            budgets[b.org_id] = b

    result = []
    for row in rows:
        budget = budgets.get(row.org_id)
        total_cost = round(float(row.total_cost or 0), 4)
        budget_limit = round(float(budget.limit_amount), 2) if budget else None
        result.append({
            "org_id": row.org_id,
            "tool_name": row.tool_name or "-",
            "total_events": row.total_events or 0,
            "total_tokens": int(row.total_tokens or 0),
            "prompt_tokens": int(row.prompt_tokens or 0),
            "completion_tokens": int(row.completion_tokens or 0),
            "total_cost": total_cost,
            "avg_risk_score": round(float(row.avg_risk_score or 0), 2),
            "budget_limit": budget_limit,
            "remaining_budget": round(budget_limit - total_cost, 2) if budget_limit is not None else None,
        })
    return result


@router.get("/admin/registered-tools")
def super_admin_registered_tools(
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Centralised view of every tool registered through the Control Module
    (via the Connector). Always surfaces the tool catalog — even when no
    telemetry events have been ingested yet — and joins live usage stats
    from `telemetry_events` plus connector metadata so the Super Admin
    can audit the full integrated AI surface area, not only injected events.
    """
    # Per-tool usage stats from telemetry (optionally scoped by org)
    usage_query = db.query(
        TelemetryEvent.model_name.label("tool_name"),
        TelemetryEvent.org_id.label("org_id"),
        func.count(TelemetryEvent.id).label("total_events"),
        func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
        func.sum(TelemetryEvent.total_cost).label("total_cost"),
        func.max(TelemetryEvent.created_at).label("last_event_at"),
    ).group_by(TelemetryEvent.model_name, TelemetryEvent.org_id)
    if org_id:
        usage_query = usage_query.filter(TelemetryEvent.org_id == org_id)
    usage_by_tool: dict = {}
    for row in usage_query.all():
        if not row.tool_name:
            continue
        usage_by_tool.setdefault(row.tool_name, []).append({
            "org_id": row.org_id,
            "total_events": int(row.total_events or 0),
            "total_tokens": int(row.total_tokens or 0),
            "total_cost": float(row.total_cost or 0),
            "last_event_at": row.last_event_at,
        })

    # All registered tools (from the Control Module catalog)
    tools = db.query(ToolRegistry).order_by(ToolRegistry.tool_name.asc()).all()

    # All connectors that integrate those tools
    connector_query = db.query(ToolConnector)
    if org_id:
        connector_query = connector_query.filter(
            (ToolConnector.org_id == org_id) | (ToolConnector.org_id.is_(None))
        )
    connectors_by_tool: dict = {}
    for c in connector_query.all():
        connectors_by_tool.setdefault(c.tool_name, []).append({
            "connector_name": c.connector_name,
            "provider": c.provider,
            "ingestion_mode": c.ingestion_mode,
            "status": c.status,
            "endpoint_url": c.endpoint_url,
            "org_id": c.org_id,
            "project_id": c.project_id,
            "last_ingested_at": c.last_ingested_at,
        })

    results = []
    for tool in tools:
        usages = usage_by_tool.get(tool.tool_name, [])
        connectors = connectors_by_tool.get(tool.tool_name, [])
        total_events = sum(u["total_events"] for u in usages)
        total_cost = round(sum(u["total_cost"] for u in usages), 6)
        total_tokens = sum(u["total_tokens"] for u in usages)
        last_event_at = max((u["last_event_at"] for u in usages if u["last_event_at"]), default=None)
        results.append({
            "tool_name": tool.tool_name,
            "tool_type": tool.tool_type,
            "vendor": tool.vendor,
            "cost_model": tool.cost_model,
            "base_cost": float(tool.base_cost or 0),
            "registered_at": tool.created_at,
            "connector_count": len(connectors),
            "connectors": connectors,
            "total_events": total_events,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "last_event_at": last_event_at,
            "is_ingesting": any((c["status"] or "").lower() == "active" for c in connectors),
        })

    # Also surface "shadow" tools — tools that have generated telemetry
    # events but were never formally registered in the catalog.
    registered_names = {t.tool_name for t in tools}
    for tool_name, usages in usage_by_tool.items():
        if tool_name in registered_names:
            continue
        connectors = connectors_by_tool.get(tool_name, [])
        total_events = sum(u["total_events"] for u in usages)
        total_cost = round(sum(u["total_cost"] for u in usages), 6)
        total_tokens = sum(u["total_tokens"] for u in usages)
        last_event_at = max((u["last_event_at"] for u in usages if u["last_event_at"]), default=None)
        results.append({
            "tool_name": tool_name,
            "tool_type": None,
            "vendor": None,
            "cost_model": None,
            "base_cost": 0.0,
            "registered_at": None,
            "connector_count": len(connectors),
            "connectors": connectors,
            "total_events": total_events,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
            "last_event_at": last_event_at,
            "is_ingesting": False,
            "unregistered": True,
        })

    results.sort(key=lambda r: (-(r["total_cost"] or 0), r["tool_name"] or ""))
    return results


@router.get("/admin/insights")
def super_admin_insights(
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """
    Real-time governance insights for the Super Admin dashboard.

    Returns:
      - tool_costs: total cost per tool aggregated across all projects and orgs
      - model_usage: token consumption per model with token limits and remaining capacity
      - notifications: in-app alerts for limit breaches, cost thresholds, and anomalies
    """
    today = date.today()
    now = datetime.utcnow()
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}

    # ── Tool cost totals ──────────────────────────────────────────────────────
    tool_cost_q = (
        db.query(
            TelemetryEvent.model_name.label("tool_name"),
            TelemetryEvent.provider,
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.prompt_tokens).label("prompt_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("completion_tokens"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.llm_cost).label("llm_cost"),
            func.sum(TelemetryEvent.infra_cost).label("infra_cost"),
            func.sum(TelemetryEvent.external_cost).label("external_cost"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
        )
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.model_name, TelemetryEvent.provider)
        .order_by(func.sum(TelemetryEvent.total_cost).desc())
    )
    if org_id:
        tool_cost_q = tool_cost_q.filter(TelemetryEvent.org_id == org_id)

    tool_costs = [
        {
            "tool_name": r.tool_name,
            "provider": r.provider or "—",
            "total_events": int(r.total_events or 0),
            "prompt_tokens": int(r.prompt_tokens or 0),
            "completion_tokens": int(r.completion_tokens or 0),
            "total_tokens": int(r.total_tokens or 0),
            "llm_cost": round(float(r.llm_cost or 0), 4),
            "infra_cost": round(float(r.infra_cost or 0), 4),
            "external_cost": round(float(r.external_cost or 0), 4),
            "total_cost": round(float(r.total_cost or 0), 4),
        }
        for r in tool_cost_q.all()
    ]

    # ── Model usage vs token limits ───────────────────────────────────────────
    rate_limits = {rl.org_id: rl for rl in db.query(RateLimit).all()}

    model_usage_q = (
        db.query(
            TelemetryEvent.model_name,
            TelemetryEvent.provider,
            TelemetryEvent.org_id,
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.prompt_tokens).label("prompt_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("completion_tokens"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
        )
        .filter(TelemetryEvent.model_name.isnot(None), TelemetryEvent.model_name != "")
        .group_by(TelemetryEvent.model_name, TelemetryEvent.provider, TelemetryEvent.org_id)
        .order_by(func.sum(TelemetryEvent.total_tokens).desc())
    )
    if org_id:
        model_usage_q = model_usage_q.filter(TelemetryEvent.org_id == org_id)

    model_usage = []
    for r in model_usage_q.all():
        rl = rate_limits.get(r.org_id)
        token_limit = int(rl.max_tokens_per_day or 0) if (rl and rl.max_tokens_per_day) else None
        total_tokens = int(r.total_tokens or 0)
        usage_pct = round(total_tokens / token_limit * 100, 1) if token_limit else None
        remaining = token_limit - total_tokens if token_limit is not None else None

        if token_limit:
            if remaining <= 0:
                token_status = "exhausted"
            elif usage_pct >= 90:
                token_status = "critical"
            elif usage_pct >= 75:
                token_status = "warning"
            else:
                token_status = "ok"
        else:
            token_status = "no_limit"

        model_usage.append({
            "model_name": r.model_name,
            "provider": r.provider or "—",
            "org_id": r.org_id,
            "total_events": int(r.total_events or 0),
            "prompt_tokens": int(r.prompt_tokens or 0),
            "completion_tokens": int(r.completion_tokens or 0),
            "total_tokens": total_tokens,
            "token_limit": token_limit,
            "remaining_tokens": remaining,
            "usage_pct": usage_pct,
            "token_status": token_status,
        })

    # ── In-app notifications ─────────────────────────────────────────────────
    notifications = []

    # Resolve org/project display names so every notification carries the same
    # full traceability set (org, project, tool, model) as the Super Admin Log.
    notif_org_ids = {m["org_id"] for m in model_usage if m.get("org_id")}
    org_name_lookup = {
        o.id: o.org_name
        for o in db.query(Organization).filter(Organization.id.in_(notif_org_ids)).all()
    } if notif_org_ids else {}

    # Token limit notifications
    for m in model_usage:
        org_name = org_name_lookup.get(m["org_id"], m["org_id"])
        if m["token_status"] == "exhausted":
            notifications.append({
                "type": "token_limit_exhausted",
                "severity": "critical",
                "message": (
                    f"Token limit exhausted for model '{m['model_name']}' in org '{m['org_id']}' — "
                    f"{m['total_tokens']:,} of {m['token_limit']:,} tokens used (100%+)"
                ),
                "org_id": m["org_id"],
                "org_name": org_name,
                "project_id": None,
                "project_name": None,
                "tool_name": m["model_name"],
                "model_name": m["model_name"],
                "created_at": now.isoformat(),
            })
        elif m["token_status"] == "critical":
            notifications.append({
                "type": "token_limit_approaching",
                "severity": "high",
                "message": (
                    f"Token limit {m['usage_pct']}% consumed for model '{m['model_name']}' "
                    f"in org '{m['org_id']}' — {m['remaining_tokens']:,} tokens remaining"
                ),
                "org_id": m["org_id"],
                "org_name": org_name,
                "project_id": None,
                "project_name": None,
                "tool_name": m["model_name"],
                "model_name": m["model_name"],
                "created_at": now.isoformat(),
            })
        elif m["token_status"] == "warning":
            notifications.append({
                "type": "token_limit_approaching",
                "severity": "medium",
                "message": (
                    f"Token usage at {m['usage_pct']}% for model '{m['model_name']}' "
                    f"in org '{m['org_id']}' — {m['remaining_tokens']:,} tokens remaining"
                ),
                "org_id": m["org_id"],
                "org_name": org_name,
                "project_id": None,
                "project_name": None,
                "tool_name": m["model_name"],
                "model_name": m["model_name"],
                "created_at": now.isoformat(),
            })

    # Cost threshold notifications from budgets
    budget_q = db.query(Budget).filter(Budget.project_id.is_(None))
    if org_id:
        budget_q = budget_q.filter(Budget.org_id == org_id)

    org_cost_q = db.query(
        TelemetryEvent.org_id,
        func.sum(TelemetryEvent.total_cost).label("total_cost"),
    ).group_by(TelemetryEvent.org_id)
    if org_id:
        org_cost_q = org_cost_q.filter(TelemetryEvent.org_id == org_id)
    org_cost_map = {r.org_id: float(r.total_cost or 0) for r in org_cost_q.all()}

    budgets_list = budget_q.all()
    budget_org_ids = {b.org_id for b in budgets_list if b.org_id}
    budget_org_names = {
        o.id: o.org_name
        for o in db.query(Organization).filter(Organization.id.in_(budget_org_ids)).all()
    } if budget_org_ids else {}
    for b in budgets_list:
        limit = float(b.limit_amount or 0)
        if limit <= 0:
            continue
        spent = org_cost_map.get(b.org_id, 0.0)
        pct = round(spent / limit * 100, 1)
        threshold_pct = int(b.alert_threshold_percent or 80)
        remaining_budget = limit - spent
        b_org_name = budget_org_names.get(b.org_id, b.org_id)

        if spent >= limit:
            notifications.append({
                "type": "cost_threshold_exceeded",
                "severity": "critical",
                "message": (
                    f"Budget EXCEEDED for org '{b.org_id}' — "
                    f"${spent:.2f} of ${limit:.2f} spent ({pct}%)"
                ),
                "org_id": b.org_id,
                "org_name": b_org_name,
                "project_id": b.project_id,
                "project_name": b.project_id,
                "tool_name": None,
                "model_name": None,
                "created_at": now.isoformat(),
            })
        elif pct >= threshold_pct:
            notifications.append({
                "type": "cost_threshold_approaching",
                "severity": "high",
                "message": (
                    f"Budget {pct}% consumed for org '{b.org_id}' — "
                    f"${remaining_budget:.2f} remaining of ${limit:.2f}"
                ),
                "org_id": b.org_id,
                "org_name": b_org_name,
                "project_id": b.project_id,
                "project_name": b.project_id,
                "tool_name": None,
                "model_name": None,
                "created_at": now.isoformat(),
            })

    # Abnormal usage anomaly notifications — fully traced to org/project/tool
    anomaly_q = db.query(UsageAnomaly).filter(UsageAnomaly.status == "open")
    if org_id:
        anomaly_q = anomaly_q.filter(UsageAnomaly.org_id == org_id)
    anomaly_rows = anomaly_q.order_by(UsageAnomaly.created_at.desc()).limit(20).all()
    anomaly_orgs = {a.org_id for a in anomaly_rows if a.org_id}
    anomaly_projects = {a.project_id for a in anomaly_rows if a.project_id}
    anomaly_org_names = {
        o.id: o.org_name
        for o in db.query(Organization).filter(Organization.id.in_(anomaly_orgs)).all()
    } if anomaly_orgs else {}
    anomaly_project_names = {
        p.id: p.project_name
        for p in db.query(Project).filter(Project.id.in_(anomaly_projects)).all()
    } if anomaly_projects else {}
    for a in anomaly_rows:
        notifications.append({
            "type": "abnormal_usage",
            "severity": a.severity or "medium",
            "message": a.message or f"Abnormal usage pattern detected in org '{a.org_id}' — tool: {a.tool_name}",
            "org_id": a.org_id,
            "org_name": anomaly_org_names.get(a.org_id, a.org_id),
            "project_id": a.project_id,
            "project_name": anomaly_project_names.get(a.project_id, a.project_id),
            "tool_name": a.tool_name,
            "model_name": a.tool_name,
            "created_at": a.created_at.isoformat() if a.created_at else now.isoformat(),
        })

    # Active high/critical alerts — joined to TelemetryEvent for model_name
    alert_q = db.query(Alert).filter(
        Alert.status == "active",
        Alert.severity.in_(["critical", "high"]),
    )
    if org_id:
        alert_q = alert_q.filter(Alert.org_id == org_id)
    alert_rows = alert_q.order_by(Alert.created_at.desc()).limit(20).all()
    alert_org_ids = {a.org_id for a in alert_rows if a.org_id}
    alert_project_ids = {a.project_id for a in alert_rows if a.project_id}
    alert_telemetry_ids = {a.telemetry_id for a in alert_rows if a.telemetry_id}
    alert_org_names = {
        o.id: o.org_name
        for o in db.query(Organization).filter(Organization.id.in_(alert_org_ids)).all()
    } if alert_org_ids else {}
    alert_project_names = {
        p.id: p.project_name
        for p in db.query(Project).filter(Project.id.in_(alert_project_ids)).all()
    } if alert_project_ids else {}
    alert_event_map = {
        e.id: e
        for e in db.query(TelemetryEvent).filter(TelemetryEvent.id.in_(alert_telemetry_ids)).all()
    } if alert_telemetry_ids else {}
    for al in alert_rows:
        evt = alert_event_map.get(al.telemetry_id) if al.telemetry_id else None
        model_name = (evt.model_name if evt else None) or al.tool_name
        notifications.append({
            "type": al.alert_type or "governance_alert",
            "severity": al.severity or "high",
            "message": al.message or f"Governance alert triggered for org '{al.org_id}'",
            "org_id": al.org_id,
            "org_name": alert_org_names.get(al.org_id, al.org_id),
            "project_id": al.project_id,
            "project_name": alert_project_names.get(al.project_id, al.project_id),
            "tool_name": al.tool_name or model_name,
            "model_name": model_name,
            "created_at": al.created_at.isoformat() if al.created_at else now.isoformat(),
        })

    notifications.sort(key=lambda n: sev_order.get(n["severity"], 99))

    return {
        "tool_costs": tool_costs,
        "model_usage": model_usage,
        "notifications": notifications,
        "notification_count": len(notifications),
        "critical_count": sum(1 for n in notifications if n["severity"] == "critical"),
        "high_count": sum(1 for n in notifications if n["severity"] == "high"),
        "medium_count": sum(1 for n in notifications if n["severity"] == "medium"),
    }


@router.get("/admin/pii-detail/{event_id}")
def admin_pii_detail(event_id: str, db: Session = Depends(get_db)):
    """Full contextual detail for a PII detection event — used by the admin modal."""
    event = db.query(TelemetryEvent).filter(TelemetryEvent.event_id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    security = db.query(DataSecurityLog).filter(DataSecurityLog.event_id == event_id).first()

    org = db.query(Organization).filter(Organization.id == event.org_id).first() if event.org_id else None
    project = db.query(Project).filter(Project.id == event.project_id).first() if event.project_id else None

    rl = db.query(RateLimit).filter(RateLimit.org_id == event.org_id).first()
    token_limit = int(rl.max_tokens_per_day or 0) if (rl and rl.max_tokens_per_day) else None
    total_tokens = int(event.total_tokens or 0)
    usage_pct = round(total_tokens / token_limit * 100, 1) if token_limit else None
    remaining_tokens = token_limit - total_tokens if token_limit is not None else None

    # Related anomalies: prefer direct event_id match, fall back to org-level
    related_anomalies = []
    anomaly_q = db.query(UsageAnomaly).filter(UsageAnomaly.org_id == event.org_id)
    direct = anomaly_q.filter(UsageAnomaly.event_id == event_id).all()
    fallback = anomaly_q.filter(UsageAnomaly.event_id.is_(None)).limit(3).all() if not direct else []
    for a in (direct or fallback):
        related_anomalies.append({
            "anomaly_type": a.anomaly_type,
            "severity": a.severity,
            "message": a.message,
            "anomaly_score": float(a.anomaly_score or 0),
            "baseline_value": float(a.baseline_value or 0),
            "observed_value": float(a.observed_value or 0),
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })

    risk = float((security.risk_score if security else None) or event.risk_score or 0)
    if risk >= 80:
        risk_label = "Critical — immediate review required"
    elif risk >= 60:
        risk_label = "High — elevated risk, monitor closely"
    elif risk >= 30:
        risk_label = "Medium — moderate risk"
    else:
        risk_label = "Low — within acceptable range"

    root_causes = []
    if security:
        if security.pii_detected:
            root_causes.append(f"Sensitive data pattern detected: {security.pii_type or 'unclassified type'}")
        if security.misuse_pattern_detected:
            root_causes.append("Misuse pattern identified in prompt or completion content")
        if security.data_out_violation:
            root_causes.append(f"Data output volume exceeded threshold — {float(security.data_out_mb or 0):.2f} MB out")
        if security.abnormal_usage_spike:
            root_causes.append("Abnormal usage spike detected vs. recent baseline")
    if usage_pct is not None and usage_pct >= 75:
        if usage_pct >= 100:
            root_causes.append(f"Token limit exhausted: {usage_pct:.1f}% of daily limit consumed")
        elif usage_pct >= 90:
            root_causes.append(f"Token limit critical: {usage_pct:.1f}% of daily limit consumed")
        else:
            root_causes.append(f"Token usage elevated: {usage_pct:.1f}% of daily limit consumed")
    if float(event.anomaly_score or 0) >= 1.5:
        root_causes.append(
            f"Anomaly score {float(event.anomaly_score):.2f}x — significantly above normal baseline"
        )
    if not root_causes:
        root_causes.append("No specific high-risk indicators beyond PII pattern match")

    return {
        "event_id": event.event_id,
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "org_id": event.org_id,
        "org_name": org.org_name if org else event.org_id,
        "project_id": event.project_id,
        "project_name": project.project_name if project else event.project_id,
        "project_environment": project.environment if project else None,
        "model_name": event.model_name,
        "tool_name": event.model_name,
        "provider": event.provider,
        "service_type": event.service_type,
        "status": event.status,
        "prompt_tokens": int(event.prompt_tokens or 0),
        "completion_tokens": int(event.completion_tokens or 0),
        "total_tokens": total_tokens,
        "total_cost": float(event.total_cost or 0),
        "latency_ms": int(event.latency_ms or 0),
        "data_in_mb": float(event.input_data_size_mb or 0),
        "data_out_mb": float(event.output_data_size_mb or 0),
        "token_limit": token_limit,
        "remaining_tokens": remaining_tokens,
        "usage_pct": usage_pct,
        "pii_detected": bool(security.pii_detected) if security else False,
        "pii_type": security.pii_type if security else None,
        "risk_score": risk,
        "risk_label": risk_label,
        "data_out_violation": bool(security.data_out_violation) if security else False,
        "misuse_pattern_detected": bool(security.misuse_pattern_detected) if security else False,
        "abnormal_usage_spike": bool((security.abnormal_usage_spike if security else False) or event.abnormal_usage_spike),
        "masking_applied": bool(security.masking_applied) if security else False,
        "anomaly_score": float(event.anomaly_score or 0),
        "root_causes": root_causes,
        "related_anomalies": related_anomalies,
    }


@router.get("/traces/{event_id}", response_model=TraceDetailResponse)
def get_event_trace(event_id: str, db: Session = Depends(get_db)):
    event = db.query(TelemetryEvent).filter(TelemetryEvent.event_id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    security = db.query(DataSecurityLog).filter(DataSecurityLog.event_id == event_id).first()
    return TraceDetailResponse(event=_build_event_response(db, event), security=security)


@router.delete("/event/{event_id}")
def delete_event(event_id: str, db: Session = Depends(get_db)):
    event = db.query(TelemetryEvent).filter(TelemetryEvent.event_id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    db.query(CostBreakdown).filter(CostBreakdown.event_id == event_id).delete()
    db.query(ExecutionPipeline).filter(ExecutionPipeline.event_id == event_id).delete()
    db.query(DataSecurityLog).filter(DataSecurityLog.event_id == event_id).delete()
    db.query(Alert).filter(Alert.telemetry_id == event.id).delete()
    db.query(UsageAnomaly).filter(UsageAnomaly.event_id == event_id).delete()
    db.delete(event)
    db.commit()
    return {"status": "deleted", "event_id": event_id}


@router.put("/event/{event_id}", response_model=TelemetryEventResponse)
def update_event(event_id: str, update_data: TelemetryEventUpdate, db: Session = Depends(get_db)):
    event = db.query(TelemetryEvent).filter(TelemetryEvent.event_id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    update_fields = update_data.model_dump(exclude_none=True)
    for field, value in update_fields.items():
        setattr(event, field, value)

    if "prompt_tokens" in update_fields or "completion_tokens" in update_fields:
        event.total_tokens = event.prompt_tokens + event.completion_tokens

    db.commit()
    db.refresh(event)
    return _build_event_response(db, event)


@router.post("/track", response_model=TelemetryEventResponse)
def track_event(event_data: TelemetryEventCreate, db: Session = Depends(get_db)):
    event = _ingest_event(db, event_data)
    db.commit()
    return _build_event_response(db, event)


def _ingest_event(db: Session, event_data: TelemetryEventCreate) -> TelemetryEvent:
    existing = db.query(TelemetryEvent).filter(TelemetryEvent.event_id == event_data.event_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="event_id already exists")

    # --- org_id handling fix ---
    # Never persist an empty org_id. Fall back to "default" so the telemetry
    # event is still ingested for super-admin/cross-tool monitoring.
    if not (event_data.org_id and str(event_data.org_id).strip()):
        event_data.org_id = "default"

    started_at = event_data.started_at or datetime.utcnow()
    completed_at = event_data.completed_at or (started_at + timedelta(milliseconds=event_data.latency_ms))
    total_tokens = event_data.prompt_tokens + event_data.completion_tokens

    cost_summary = cost_engine.calculate(event_data, db)
    security_result = security_engine.analyze(event_data)
    anomaly_score, abnormal_usage_spike = _detect_event_spike(db, event_data)

    telemetry = TelemetryEvent(
        event_id=event_data.event_id,
        request_id=event_data.request_id,
        trace_id=event_data.trace_id or event_data.event_id,
        org_id=event_data.org_id,
        project_id=event_data.project_id,
        user_id=event_data.user_id,
        api_key_id=event_data.api_key_id,
        provider=event_data.provider,
        model_name=event_data.model_name or event_data.tool_name,
        service_type=event_data.service_type,
        component_name=event_data.component_name,
        execution_type=event_data.execution_type,
        status=event_data.status,
        input_data_size_mb=event_data.input_data_size_mb,
        output_data_size_mb=event_data.output_data_size_mb,
        prompt_tokens=event_data.prompt_tokens,
        completion_tokens=event_data.completion_tokens,
        total_tokens=total_tokens,
        llm_cost=cost_summary.llm_cost,
        infra_cost=cost_summary.infra_cost,
        external_cost=cost_summary.external_cost,
        total_cost=cost_summary.total_cost,
        risk_score=security_result["risk_score"],
        anomaly_score=anomaly_score,
        misuse_detected=security_result["misuse_pattern_detected"],
        abnormal_usage_spike=abnormal_usage_spike,
        started_at=started_at,
        completed_at=completed_at,
        latency_ms=event_data.latency_ms,
        tags=_json_safe(event_data.tags),
        metadata_json=_json_safe(event_data.metadata_json),
        input_preview=event_data.input_preview,
        output_preview=event_data.output_preview,
        raw_usage_json=_json_safe(event_data.raw_usage_json) if event_data.raw_usage_json else {
            "prompt_tokens": event_data.prompt_tokens,
            "completion_tokens": event_data.completion_tokens,
            "total_tokens": total_tokens,
            "provider": event_data.provider,
            "model_name": event_data.model_name,
            "latency_ms": event_data.latency_ms,
        },
    )
    db.add(telemetry)
    db.flush()

    _save_cost_breakdown(db, event_data, cost_summary)
    _save_pipeline_stages(db, event_data)
    _save_security_log(db, event_data, security_result, abnormal_usage_spike)
    _upsert_daily_summary(db, telemetry)
    alert_engine.evaluate(db, event_data, cost_summary, security_result, anomaly_score, abnormal_usage_spike, telemetry_id=telemetry.id)
    db.flush()

    # Additive: mirror to Langfuse for waterfall UI / prompt browser / scores.
    # No-op when LANGFUSE_ENABLED!=true or the SDK isn't installed.  Errors
    # are swallowed inside the bridge so ingestion is never affected.
    _langfuse_mirror_event(
        event_data,
        llm_cost=float(cost_summary.llm_cost),
        total_cost=float(cost_summary.total_cost),
        risk_score=float(security_result["risk_score"]),
        pii_detected=bool(security_result["pii_detected"]),
    )

    return telemetry


def _save_cost_breakdown(db: Session, event_data: TelemetryEventCreate, cost_summary) -> None:
    total_tokens = Decimal(str(event_data.prompt_tokens + event_data.completion_tokens))
    if total_tokens > 0:
        db.add(
            CostBreakdown(
                event_id=event_data.event_id,
                cost_type="llm",
                component_name=event_data.model_name or event_data.component_name or event_data.tool_name,
                unit_cost=(cost_summary.llm_cost / total_tokens).quantize(Decimal("0.000001")),
                quantity=total_tokens,
                total_cost=cost_summary.llm_cost,
            )
        )
    if cost_summary.infra_cost > 0:
        db.add(
            CostBreakdown(
                event_id=event_data.event_id,
                cost_type="infra",
                component_name="compute",
                unit_cost=Decimal("0.000080"),
                quantity=Decimal(str(max(event_data.latency_ms, 1))),
                total_cost=cost_summary.infra_cost,
            )
        )
    for item in event_data.external_tools:
        db.add(
            CostBreakdown(
                event_id=event_data.event_id,
                cost_type="external",
                component_name=item.name,
                unit_cost=Decimal(str(item.cost)),
                quantity=Decimal("1"),
                total_cost=Decimal(str(item.cost)),
            )
        )


def _save_pipeline_stages(db: Session, event_data: TelemetryEventCreate) -> None:
    for index, stage in enumerate(event_data.stages):
        db.add(
            ExecutionPipeline(
                event_id=event_data.event_id,
                stage_order=stage.stage_order if stage.stage_order is not None else index,
                stage_name=stage.stage_name,
                system_name=stage.system_name,
                status=stage.status,
                stage_latency_ms=stage.stage_latency_ms,
                retry_count=stage.retry_count,
                details=_json_safe(stage.details),
            )
        )


def _save_security_log(
    db: Session,
    event_data: TelemetryEventCreate,
    security_result: dict,
    abnormal_usage_spike: bool,
) -> None:
    db.add(
        DataSecurityLog(
            event_id=event_data.event_id,
            org_id=event_data.org_id,
            project_id=event_data.project_id,
            pii_detected=security_result["pii_detected"],
            pii_type=security_result["pii_type"],
            data_out_violation=security_result["data_out_violation"],
            misuse_pattern_detected=security_result["misuse_pattern_detected"],
            abnormal_usage_spike=abnormal_usage_spike,
            masking_applied=security_result["masking_applied"],
            risk_score=security_result["risk_score"],
            data_in_mb=event_data.input_data_size_mb,
            data_out_mb=event_data.output_data_size_mb,
        )
    )


def _upsert_daily_summary(db: Session, event: TelemetryEvent) -> None:
    summary_date = (event.completed_at or event.created_at or datetime.utcnow()).date()
    summary = (
        db.query(DailyOrgSummary)
        .filter(
            DailyOrgSummary.org_id == event.org_id,
            DailyOrgSummary.project_id == event.project_id,
            DailyOrgSummary.tool_name == (event.model_name or ""),
            DailyOrgSummary.date == summary_date,
        )
        .first()
    )

    success_count = 1 if (event.status or "").lower() in {"success", "completed"} else 0
    failure_count = 0 if success_count else 1
    anomaly_count = 1 if event.abnormal_usage_spike else 0
    misuse_count = 1 if event.misuse_detected else 0

    if not summary:
        summary = DailyOrgSummary(
            org_id=event.org_id,
            project_id=event.project_id,
            tool_name=event.model_name or "",
            date=summary_date,
            total_events=1,
            total_cost=event.total_cost,
            llm_cost=event.llm_cost,
            infra_cost=event.infra_cost,
            external_cost=event.external_cost,
            total_prompt_tokens=event.prompt_tokens,
            total_completion_tokens=event.completion_tokens,
            total_tokens=event.total_tokens,
            avg_latency_ms=event.latency_ms,
            success_count=success_count,
            failure_count=failure_count,
            anomaly_count=anomaly_count,
            misuse_count=misuse_count,
            total_input_mb=event.input_data_size_mb,
            total_output_mb=event.output_data_size_mb,
            avg_risk_score=event.risk_score,
        )
        db.add(summary)
        return

    previous_events = summary.total_events or 0
    summary.total_events = previous_events + 1
    summary.total_cost = Decimal(str(summary.total_cost or 0)) + Decimal(str(event.total_cost or 0))
    summary.llm_cost = Decimal(str(summary.llm_cost or 0)) + Decimal(str(event.llm_cost or 0))
    summary.infra_cost = Decimal(str(summary.infra_cost or 0)) + Decimal(str(event.infra_cost or 0))
    summary.external_cost = Decimal(str(summary.external_cost or 0)) + Decimal(str(event.external_cost or 0))
    summary.total_prompt_tokens = (summary.total_prompt_tokens or 0) + (event.prompt_tokens or 0)
    summary.total_completion_tokens = (summary.total_completion_tokens or 0) + (event.completion_tokens or 0)
    summary.total_tokens = (summary.total_tokens or 0) + (event.total_tokens or 0)
    summary.avg_latency_ms = int(
        (((summary.avg_latency_ms or 0) * previous_events) + (event.latency_ms or 0)) / (previous_events + 1)
    )
    summary.success_count = (summary.success_count or 0) + success_count
    summary.failure_count = (summary.failure_count or 0) + failure_count
    summary.anomaly_count = (summary.anomaly_count or 0) + anomaly_count
    summary.misuse_count = (summary.misuse_count or 0) + misuse_count
    summary.total_input_mb = Decimal(str(summary.total_input_mb or 0)) + Decimal(str(event.input_data_size_mb or 0))
    summary.total_output_mb = Decimal(str(summary.total_output_mb or 0)) + Decimal(str(event.output_data_size_mb or 0))
    summary.avg_risk_score = Decimal(
        str((((Decimal(str(summary.avg_risk_score or 0)) * previous_events) + Decimal(str(event.risk_score or 0))) / (previous_events + 1)))
    ).quantize(Decimal("0.01"))


def _detect_event_spike(db: Session, event_data: TelemetryEventCreate) -> tuple[Decimal, bool]:
    today = date.today()
    recent_counts = (
        db.query(func.date(TelemetryEvent.created_at), func.count(TelemetryEvent.id))
        .filter(
            TelemetryEvent.org_id == event_data.org_id,
            TelemetryEvent.model_name == (event_data.model_name or event_data.tool_name),
            TelemetryEvent.created_at >= datetime.combine(today - timedelta(days=7), datetime.min.time()),
            TelemetryEvent.created_at < datetime.combine(today, datetime.min.time()),
        )
        .group_by(func.date(TelemetryEvent.created_at))
        .all()
    )
    baseline = Decimal(str(sum(row[1] for row in recent_counts) / len(recent_counts))) if recent_counts else Decimal("1")
    today_count = (
        db.query(func.count(TelemetryEvent.id))
        .filter(
            TelemetryEvent.org_id == event_data.org_id,
            TelemetryEvent.model_name == (event_data.model_name or event_data.tool_name),
            func.date(TelemetryEvent.created_at) == today,
        )
        .scalar()
        or 0
    )
    observed = Decimal(str(today_count + 1))
    anomaly_ratio = observed / baseline if baseline > 0 else Decimal("1")
    abnormal_usage_spike = anomaly_ratio >= Decimal("1.5")
    return anomaly_ratio.quantize(Decimal("0.01")), abnormal_usage_spike


def _build_event_response(db: Session, event: TelemetryEvent) -> TelemetryEventResponse:
    breakdown = (
        db.query(CostBreakdown)
        .filter(CostBreakdown.event_id == event.event_id)
        .order_by(CostBreakdown.id.asc())
        .all()
    )
    stages = (
        db.query(ExecutionPipeline)
        .filter(ExecutionPipeline.event_id == event.event_id)
        .order_by(ExecutionPipeline.stage_order.asc(), ExecutionPipeline.id.asc())
        .all()
    )
    return TelemetryEventResponse(
        event_id=event.event_id,
        request_id=event.request_id,
        trace_id=event.trace_id,
        org_id=event.org_id,
        project_id=event.project_id,
        user_id=event.user_id,
        api_key_id=event.api_key_id,
        tool_name=event.model_name or "",
        provider=event.provider,
        model_name=event.model_name,
        service_type=event.service_type,
        component_name=event.component_name,
        execution_type=event.execution_type,
        status=event.status,
        input_data_size_mb=Decimal(str(event.input_data_size_mb or 0)),
        output_data_size_mb=Decimal(str(event.output_data_size_mb or 0)),
        prompt_tokens=event.prompt_tokens or 0,
        completion_tokens=event.completion_tokens or 0,
        total_tokens=event.total_tokens or 0,
        llm_cost=Decimal(str(event.llm_cost or 0)),
        infra_cost=Decimal(str(event.infra_cost or 0)),
        external_cost=Decimal(str(event.external_cost or 0)),
        total_cost=Decimal(str(event.total_cost or 0)),
        risk_score=Decimal(str(event.risk_score or 0)),
        anomaly_score=Decimal(str(event.anomaly_score or 0)),
        misuse_detected=bool(event.misuse_detected),
        abnormal_usage_spike=bool(event.abnormal_usage_spike),
        started_at=event.started_at,
        completed_at=event.completed_at,
        latency_ms=event.latency_ms or 0,
        tags=event.tags or [],
        metadata_json=event.metadata_json or {},
        raw_usage_json=event.raw_usage_json,
        created_at=event.created_at,
        cost_breakdown=[CostBreakdownResponse.model_validate(item) for item in breakdown],
        stages=stages,
    )
