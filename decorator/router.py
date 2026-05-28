"""
FastAPI router for the Decorator Framework.

Endpoints
─────────
POST /tools/inventory/upsert          — SDK upsert (GovernanceDecorator._update_inventory)
GET  /decorator/registrations         — list all decorated functions seen by the platform
GET  /decorator/inventory             — tool-level function catalog with call stats
GET  /decorator/inventory/{tool_name} — per-tool function list
GET  /decorator/usage                 — daily project × model aggregations
GET  /decorator/logs                  — per-call input/output audit trail
GET  /decorator/stats                 — high-level summary counts
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_db, require_api_key
from app.models import TelemetryEvent
from app.routers.telemetry import _ingest_event
from app.schemas import TelemetryEventCreate
from app.services.ai_model_pricing import calculate_token_cost
from decorator.models import (
    DecoratorRegistration,
    ProjectModelUsage,
    RequestResponseLog,
    ToolApiInventory,
)

router = APIRouter(tags=["decorator"])


def _first_present(payload: dict, *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None and value != "":
            return value
    return default


def _first_present_deep(payload: dict, *keys: str, default: Any = None) -> Any:
    """Pick a metric from top-level payload or common nested usage blocks."""
    value = _first_present(payload, *keys)
    if value is not None:
        return value

    for container_key in ("usage", "metrics", "raw_usage_json", "metadata"):
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            value = _first_present(nested, *keys)
            if value is not None:
                return value

            # Some app routes return {"response": {"usage": {...}}}.
            response = nested.get("response")
            if isinstance(response, dict):
                response_usage = response.get("usage") or response.get("metrics") or response
                if isinstance(response_usage, dict):
                    value = _first_present(response_usage, *keys)
                    if value is not None:
                        return value

    response = payload.get("response")
    if isinstance(response, dict):
        response_usage = response.get("usage") or response.get("metrics") or response
        if isinstance(response_usage, dict):
            value = _first_present(response_usage, *keys)
            if value is not None:
                return value

    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────────────────────────
# SDK → platform upsert
# Called by GovernanceDecorator._update_inventory() after every function call.
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/tools/inventory/upsert", summary="SDK: upsert tool function inventory")
def upsert_tool_inventory(
    payload: dict,
    db: Session = Depends(get_db),
    _: Any = Depends(require_api_key),
):
    """
    Upsert an entry in tool_api_inventory and decorator_registrations.
    Called automatically by the governance SDK — external callers should not
    need to hit this endpoint manually.
    """
    org_id        = payload.get("org_id")
    tool_name     = payload.get("tool_name")
    function_name = payload.get("function_name")

    if not all([org_id, tool_name, function_name]):
        raise HTTPException(status_code=422, detail="org_id, tool_name, function_name are required")

    status     = payload.get("status", "success")
    latency_ms = int(payload.get("latency_ms", 0))

    # ── tool_api_inventory upsert ──────────────────────────────────────────
    inv = (
        db.query(ToolApiInventory)
        .filter_by(org_id=org_id, project_id=payload.get("project_id"), tool_name=tool_name, function_name=function_name)
        .first()
    )
    if inv is None:
        inv = ToolApiInventory(
            org_id=org_id,
            project_id=payload.get("project_id"),
            tool_name=tool_name,
            function_name=function_name,
            module_path=payload.get("module_path"),
            decorator_type=payload.get("decorator_type", "trace"),
            total_calls=1,
            success_calls=1 if status == "success" else 0,
            error_calls=0 if status == "success" else 1,
            avg_latency_ms=latency_ms,
        )
        db.add(inv)
    else:
        inv.last_seen      = func.now()
        inv.total_calls    = (inv.total_calls or 0) + 1
        if status == "success":
            inv.success_calls = (inv.success_calls or 0) + 1
        else:
            inv.error_calls = (inv.error_calls or 0) + 1
        # rolling average latency
        n = inv.total_calls or 1
        inv.avg_latency_ms = int(((inv.avg_latency_ms or 0) * (n - 1) + (latency_ms or 0)) / n)

    # ── decorator_registrations upsert ─────────────────────────────────────
    reg = (
        db.query(DecoratorRegistration)
        .filter_by(org_id=org_id, tool_name=tool_name, function_name=function_name)
        .first()
    )
    if reg is None:
        reg = DecoratorRegistration(
            org_id=org_id,
            project_id=payload.get("project_id"),
            tool_name=tool_name,
            function_name=function_name,
            module_path=payload.get("module_path"),
            decorator_type=payload.get("decorator_type", "trace"),
            sdk_version=payload.get("sdk_version"),
            python_version=payload.get("python_version"),
            execution_env=payload.get("execution_env", "production"),
            call_count=1,
        )
        db.add(reg)
    else:
        reg.last_seen  = func.now()
        reg.call_count = (reg.call_count or 0) + 1

    db.commit()
    return {"status": "ok", "function": function_name, "tool": tool_name}


# ─────────────────────────────────────────────────────────────────────────────
# Decorator Registry
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/decorator/registrations", summary="List all registered decorated functions")
def list_registrations(
    org_id:         Optional[str] = Query(None),
    project_id:     Optional[str] = Query(None),
    tool_name:      Optional[str] = Query(None),
    decorator_type: Optional[str] = Query(None),
    limit:          int           = Query(200, le=1000),
    offset:         int           = Query(0),
    db: Session = Depends(get_db),
):
    q = db.query(DecoratorRegistration)
    if org_id:         q = q.filter(DecoratorRegistration.org_id == org_id)
    if project_id:     q = q.filter(DecoratorRegistration.project_id == project_id)
    if tool_name:      q = q.filter(DecoratorRegistration.tool_name == tool_name)
    if decorator_type: q = q.filter(DecoratorRegistration.decorator_type == decorator_type)
    total = q.count()
    rows  = q.order_by(DecoratorRegistration.last_seen.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [
            {
                "id":             r.id,
                "org_id":         r.org_id,
                "project_id":     r.project_id,
                "tool_name":      r.tool_name,
                "function_name":  r.function_name,
                "module_path":    r.module_path,
                "decorator_type": r.decorator_type,
                "execution_env":  r.execution_env,
                "sdk_version":    r.sdk_version,
                "python_version": r.python_version,
                "first_seen":     r.first_seen,
                "last_seen":      r.last_seen,
                "call_count":     r.call_count,
            }
            for r in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tool API Inventory
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/decorator/inventory", summary="Tool API inventory with call stats")
def list_inventory(
    org_id:     Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    tool_name:  Optional[str] = Query(None),
    limit:      int           = Query(200, le=1000),
    offset:     int           = Query(0),
    db: Session = Depends(get_db),
):
    q = db.query(ToolApiInventory)
    if org_id:     q = q.filter(ToolApiInventory.org_id == org_id)
    if project_id: q = q.filter(ToolApiInventory.project_id == project_id)
    if tool_name:  q = q.filter(ToolApiInventory.tool_name == tool_name)
    total = q.count()
    rows  = q.order_by(ToolApiInventory.total_calls.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [
            {
                "id":             r.id,
                "org_id":         r.org_id,
                "project_id":     r.project_id,
                "tool_name":      r.tool_name,
                "function_name":  r.function_name,
                "module_path":    r.module_path,
                "decorator_type": r.decorator_type,
                "first_seen":     r.first_seen,
                "last_seen":      r.last_seen,
                "total_calls":    r.total_calls,
                "success_calls":  r.success_calls,
                "error_calls":    r.error_calls,
                "error_rate":     round(
                    (r.error_calls or 0) / max(r.total_calls or 1, 1) * 100, 2
                ),
                "avg_latency_ms": r.avg_latency_ms,
            }
            for r in rows
        ],
    }


@router.get("/decorator/inventory/{tool_name}", summary="Inventory for a specific tool")
def get_tool_inventory(
    tool_name:  str,
    org_id:     Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    q = db.query(ToolApiInventory).filter(ToolApiInventory.tool_name == tool_name)
    if org_id: q = q.filter(ToolApiInventory.org_id == org_id)
    rows = q.order_by(ToolApiInventory.function_name.asc()).all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No inventory found for tool '{tool_name}'")
    return {
        "tool_name": tool_name,
        "function_count": len(rows),
        "functions": [
            {
                "function_name":  r.function_name,
                "module_path":    r.module_path,
                "decorator_type": r.decorator_type,
                "total_calls":    r.total_calls,
                "success_calls":  r.success_calls,
                "error_calls":    r.error_calls,
                "avg_latency_ms": r.avg_latency_ms,
                "first_seen":     r.first_seen,
                "last_seen":      r.last_seen,
            }
            for r in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Project × Model Usage
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/decorator/usage", summary="Daily project-model usage aggregations")
def list_project_model_usage(
    org_id:     Optional[str]  = Query(None),
    project_id: Optional[str]  = Query(None),
    model_name: Optional[str]  = Query(None),
    start_date: Optional[date] = Query(None),
    end_date:   Optional[date] = Query(None),
    limit:      int            = Query(90, le=500),
    offset:     int            = Query(0),
    db: Session = Depends(get_db),
):
    q = db.query(ProjectModelUsage)
    if org_id:     q = q.filter(ProjectModelUsage.org_id == org_id)
    if project_id: q = q.filter(ProjectModelUsage.project_id == project_id)
    if model_name: q = q.filter(ProjectModelUsage.model_name == model_name)
    if start_date: q = q.filter(ProjectModelUsage.date >= start_date)
    if end_date:   q = q.filter(ProjectModelUsage.date <= end_date)
    total = q.count()
    rows  = q.order_by(ProjectModelUsage.date.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [
            {
                "id":                      r.id,
                "org_id":                  r.org_id,
                "project_id":              r.project_id,
                "model_name":              r.model_name,
                "provider":                r.provider,
                "date":                    str(r.date),
                "call_count":              r.call_count,
                "input_tokens":            r.total_prompt_tokens or 0,
                "output_tokens":           r.total_completion_tokens or 0,
                "total_prompt_tokens":     r.total_prompt_tokens or 0,
                "total_completion_tokens": r.total_completion_tokens or 0,
                "total_tokens":            r.total_tokens or 0,
                **calculate_token_cost(r.model_name or "", r.total_prompt_tokens or 0, r.total_completion_tokens or 0),
                "avg_latency_ms":          r.avg_latency_ms,
                "success_count":           r.success_count,
                "error_count":             r.error_count,
            }
            for r in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Request / Response Audit Logs
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/decorator/logs", summary="Per-call input/output audit trail")
def list_request_response_logs(
    function_name: Optional[str]  = Query(None),
    tool_name:     Optional[str]  = Query(None),
    trace_id:      Optional[str]  = Query(None),
    user_id:       Optional[str]  = Query(None),
    pii_detected:  Optional[bool] = Query(None),
    event_id:      Optional[str]  = Query(None),
    org_id:        Optional[str]  = Query(None),
    project_id:    Optional[str]  = Query(None),
    limit:         int            = Query(100, le=500),
    offset:        int            = Query(0),
    db: Session = Depends(get_db),
):
    q = (
        db.query(
            RequestResponseLog,
            TelemetryEvent.org_id.label("t_org_id"),
            TelemetryEvent.project_id.label("t_project_id"),
            TelemetryEvent.tool_name.label("t_tool_name"),
        )
        .outerjoin(TelemetryEvent, RequestResponseLog.event_id == TelemetryEvent.event_id)
    )
    if function_name:            q = q.filter(RequestResponseLog.function_name == function_name)
    if tool_name:                q = q.filter(TelemetryEvent.tool_name == tool_name)
    if trace_id:                 q = q.filter(RequestResponseLog.trace_id == trace_id)
    if user_id:                  q = q.filter(RequestResponseLog.user_id == user_id)
    if pii_detected is not None: q = q.filter(RequestResponseLog.pii_detected == pii_detected)
    if event_id:                 q = q.filter(RequestResponseLog.event_id == event_id)
    if org_id:                   q = q.filter(TelemetryEvent.org_id == org_id)
    if project_id:               q = q.filter(TelemetryEvent.project_id == project_id)
    total = q.count()
    rows  = q.order_by(RequestResponseLog.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [
            {
                "id":                 r.id,
                "event_id":           r.event_id,
                "trace_id":           r.trace_id,
                "user_id":            r.user_id,
                "org_id":             t_org_id,
                "project_id":         t_project_id,
                "tool_name":          t_tool_name or r.route,
                "function_name":      r.function_name,
                "route":              r.route,
                "model_name":         r.model_name,
                "provider":           r.provider,
                "prompt_tokens":      r.prompt_tokens,
                "completion_tokens":  r.completion_tokens,
                "total_tokens":       r.total_tokens,
                "latency_ms":         r.latency_ms,
                "latency_seconds":    round(r.latency_ms / 1000, 4) if r.latency_ms is not None else None,
                "estimated_cost_usd": float(r.estimated_cost_usd) if r.estimated_cost_usd is not None else None,
                "input_preview":      r.input_preview,
                "output_preview":     r.output_preview,
                "input_size_bytes":   r.input_size_bytes,
                "output_size_bytes":  r.output_size_bytes,
                "pii_detected":       r.pii_detected,
                "pii_fields":         r.pii_fields,
                "created_at":         r.created_at,
            }
            for r, t_org_id, t_project_id, t_tool_name in rows
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Summary Stats
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/decorator/stats", summary="High-level decorator framework counts")
def decorator_stats(
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    def count(model, **filters):
        q = db.query(func.count()).select_from(model)
        for k, v in filters.items():
            q = q.filter(getattr(model, k) == v)
        return q.scalar() or 0

    kw = {"org_id": org_id} if org_id else {}
    return {
        "registered_functions": count(DecoratorRegistration, **kw),
        "inventory_functions":  count(ToolApiInventory,       **kw),
        "usage_records":        count(ProjectModelUsage,       **kw),
        "audit_log_entries":    count(RequestResponseLog),
    }


# ─────────────────────────────────────────────────────────────────────────────
# governance_logger ingest
# Single endpoint called by app.decorators.telemetry.governance_logger after
# every decorated route/function invocation.  Writes to all four decorator
# tables in one transaction so the SDK needs only one HTTP call.
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/decorator/ingest", summary="Receive telemetry from governance_logger decorator")
def ingest_decorator_telemetry(
    payload: dict,
    db: Session = Depends(get_db),
    _: Any = Depends(require_api_key),
):
    # ── unpack — no hardcoded fallbacks; null stays null ───────────────────
    org_id        = payload.get("org_id") or payload.get("organization") or None
    project_id    = payload.get("project_id") or payload.get("project_name") or None
    trace_id      = payload.get("trace_id") or None
    user_id       = payload.get("user_id") or None
    tool_name     = _first_present(payload, "tool_name", "route", "endpoint") or None
    function_name = _first_present(payload, "function_name") or payload.get("route") or None
    route         = payload.get("route") or None
    module_path   = payload.get("module_path") or None
    model_name    = _first_present_deep(payload, "model_name", "model") or None
    provider      = payload.get("provider") or None
    decorator_type= payload.get("decorator_type") or None
    status        = payload.get("status") or None
    metadata      = payload.get("metadata") or {}
    execution_env = metadata.get("execution_env") or payload.get("execution_env") or None
    logger_version= payload.get("logger_version") or None
    http_method   = payload.get("http_method") or metadata.get("http_method")
    http_path     = payload.get("http_path") or metadata.get("http_path")
    contains_pii  = bool(payload.get("contains_pii", False))
    pii_type      = payload.get("pii_type") or None
    input_preview = payload.get("input_preview")
    output_preview= payload.get("output_preview")
    input_size_b  = int(float(payload.get("input_data_size_mb") or 0) * 1024 * 1024)
    output_size_b = int(float(payload.get("output_data_size_mb") or 0) * 1024 * 1024)
    tags          = payload.get("tags") or []

    # Tokens — read exactly what the decorator logged; None if not provided
    _raw_prompt     = _first_present_deep(payload, "input_tokens", "prompt_tokens", "total_input_tokens")
    _raw_completion = _first_present_deep(payload, "output_tokens", "completion_tokens", "total_output_tokens")
    _raw_total      = _first_present_deep(payload, "total_tokens")
    prompt_tokens     = _as_int(_raw_prompt)     if _raw_prompt     is not None else None
    completion_tokens = _as_int(_raw_completion) if _raw_completion is not None else None
    if _raw_total is not None:
        total_tokens = _as_int(_raw_total)
    elif prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    else:
        total_tokens = None

    # Latency — prefer latency_ms, fall back to latency_seconds × 1000
    _raw_latency_ms  = _first_present_deep(payload, "latency_ms")
    _raw_latency_sec = _first_present_deep(payload, "latency_seconds")
    if _raw_latency_ms is not None:
        latency_ms = _as_int(_raw_latency_ms)
    elif _raw_latency_sec is not None:
        latency_ms = int(_as_float(_raw_latency_sec) * 1000)
    else:
        latency_ms = None

    # Cost — read exactly what the decorator logged; None if not provided
    _raw_cost = _first_present_deep(payload, "estimated_cost_usd", "estimated_cost", "total_cost", "llm_cost")
    estimated_cost = _as_float(_raw_cost) if _raw_cost is not None else None

    # ── 1. tool_api_inventory ──────────────────────────────────────────────
    inv = (
        db.query(ToolApiInventory)
        .filter_by(org_id=org_id, project_id=project_id, tool_name=tool_name, function_name=function_name)
        .first()
    )
    if inv is None:
        inv = ToolApiInventory(
            org_id=org_id,
            project_id=project_id,
            tool_name=tool_name,
            function_name=function_name,
            module_path=module_path,
            decorator_type=decorator_type,
            total_calls=1,
            success_calls=1 if status == "success" else 0,
            error_calls=0 if status == "success" else 1,
            avg_latency_ms=latency_ms,
        )
        db.add(inv)
    else:
        inv.last_seen   = func.now()
        inv.total_calls = (inv.total_calls or 0) + 1
        if status == "success":
            inv.success_calls = (inv.success_calls or 0) + 1
        else:
            inv.error_calls = (inv.error_calls or 0) + 1
        n = inv.total_calls or 1
        inv.avg_latency_ms = int(((inv.avg_latency_ms or 0) * (n - 1) + (latency_ms or 0)) / n)

    # ── 2. decorator_registrations ─────────────────────────────────────────
    reg = (
        db.query(DecoratorRegistration)
        .filter_by(org_id=org_id, tool_name=tool_name, function_name=function_name)
        .first()
    )
    if reg is None:
        reg = DecoratorRegistration(
            org_id=org_id,
            project_id=project_id,
            tool_name=tool_name,
            function_name=function_name,
            module_path=module_path,
            decorator_type=decorator_type,
            sdk_version=logger_version,
            python_version=metadata.get("python_version"),
            execution_env=execution_env,
            call_count=1,
        )
        db.add(reg)
    else:
        reg.last_seen  = func.now()
        reg.call_count = (reg.call_count or 0) + 1

    # ── 3. project_model_usage  (only when a model is known) ──────────────
    if model_name:
        today = date.today()
        usage = (
            db.query(ProjectModelUsage)
            .filter_by(org_id=org_id, project_id=project_id, model_name=model_name, date=today)
            .first()
        )
        if usage is None:
            usage = ProjectModelUsage(
                org_id=org_id,
                project_id=project_id,
                model_name=model_name,
                provider=provider,
                date=today,
                call_count=1,
                total_prompt_tokens=prompt_tokens,
                total_completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                total_cost=estimated_cost,
                avg_latency_ms=latency_ms,
                success_count=1 if status == "success" else 0,
                error_count=0 if status == "success" else 1,
            )
            db.add(usage)
        else:
            usage.call_count = (usage.call_count or 0) + 1
            if prompt_tokens is not None:
                usage.total_prompt_tokens = (usage.total_prompt_tokens or 0) + prompt_tokens
            if completion_tokens is not None:
                usage.total_completion_tokens = (usage.total_completion_tokens or 0) + completion_tokens
            if total_tokens is not None:
                usage.total_tokens = (usage.total_tokens or 0) + total_tokens
            if estimated_cost is not None:
                usage.total_cost = float(usage.total_cost or 0) + estimated_cost
            if latency_ms is not None:
                n = usage.call_count or 1
                usage.avg_latency_ms = int(((usage.avg_latency_ms or 0) * (n - 1) + latency_ms) / n)
            if status == "success":
                usage.success_count = (usage.success_count or 0) + 1
            else:
                usage.error_count = (usage.error_count or 0) + 1

    # ── 4. request_response_logs — store exactly what the decorator sent ───
    http_context = f"{http_method} {http_path}" if (http_method and http_path) else None
    log = RequestResponseLog(
        trace_id=trace_id,
        user_id=user_id,
        function_name=function_name,
        route=route or tool_name,
        model_name=model_name,
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        latency_ms=latency_ms,
        estimated_cost_usd=estimated_cost,
        input_preview=input_preview,
        output_preview=output_preview,
        input_size_bytes=input_size_b,
        output_size_bytes=output_size_b,
        input_keys=http_context,
        output_keys=model_name,
        pii_detected=contains_pii,
        pii_fields=pii_type if contains_pii else None,
    )
    db.add(log)

    # ── 5. full governance pipeline ────────────────────────────────────────
    # Cost is NOT pre-computed by the logger — the CostEngine calculates it
    # from model_pricing DB table so any model/tool is supported automatically.
    try:
        input_mb  = Decimal(str(float(payload.get("input_data_size_mb")  or 0)))
        output_mb = Decimal(str(float(payload.get("output_data_size_mb") or 0)))

        tec = TelemetryEventCreate(
            event_id=f"dec-{uuid.uuid4().hex}",
            org_id=org_id or "default",
            project_id=project_id,
            trace_id=trace_id,
            user_id=user_id,
            tool_name=tool_name or route or "unknown",
            provider=provider,
            model_name=model_name,
            service_type=payload.get("service_type") or route or decorator_type,
            execution_type=decorator_type,
            status=status or "success",
            latency_ms=latency_ms or 0,
            prompt_tokens=prompt_tokens or 0,
            completion_tokens=completion_tokens or 0,
            contains_pii=contains_pii,
            pii_type=pii_type,
            input_data_size_mb=input_mb,
            output_data_size_mb=output_mb,
            input_preview=input_preview,
            output_preview=output_preview,
            precomputed_llm_cost=None,   # always let CostEngine calculate from model_pricing
            metadata_json={
                "function_name": function_name,
                "route": route,
                "module_path": module_path,
                "decorator_type": decorator_type,
                "execution_env": execution_env,
                "trace_id": trace_id,
                **{k: v for k, v in metadata.items()
                   if isinstance(v, (str, int, float, bool, type(None)))},
            },
            tags=tags,
        )
        telemetry = _ingest_event(db, tec)
        log.event_id = telemetry.event_id
        # Write the backend-computed cost back to the log row so the UI
        # shows the real cost (from model_pricing), not a client estimate.
        log.estimated_cost_usd = telemetry.total_cost or telemetry.llm_cost or None
        # Also update the daily project_model_usage row with the real cost
        if model_name and usage is not None:
            real_cost = float(telemetry.total_cost or 0)
            if real_cost and estimated_cost is not None:
                cost_delta = real_cost - (estimated_cost or 0)
                usage.total_cost = float(usage.total_cost or 0) + cost_delta
    except Exception:
        pass  # never let governance pipeline failure break decorator ingest

    db.commit()
    return {
        "status":         "ok",
        "function":       function_name,
        "tool":           tool_name,
        "org_id":         org_id,
        "project_id":     project_id,
        "model_name":     model_name,
        "total_tokens":   total_tokens,
        "estimated_cost": estimated_cost,
        "latency_ms":     latency_ms,
    }
