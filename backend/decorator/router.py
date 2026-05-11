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
from app.routers.telemetry import _ingest_event
from app.schemas import TelemetryEventCreate
from decorator.models import (
    DecoratorRegistration,
    ProjectModelUsage,
    RequestResponseLog,
    ToolApiInventory,
)

router = APIRouter(tags=["decorator"])


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
        .filter_by(org_id=org_id, tool_name=tool_name, function_name=function_name)
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
        inv.avg_latency_ms = int(((inv.avg_latency_ms or 0) * (n - 1) + latency_ms) / n)

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
                "total_prompt_tokens":     r.total_prompt_tokens,
                "total_completion_tokens": r.total_completion_tokens,
                "total_tokens":            r.total_tokens,
                "total_cost":              float(r.total_cost or 0),
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
    pii_detected:  Optional[bool] = Query(None),
    event_id:      Optional[str]  = Query(None),
    limit:         int            = Query(50, le=500),
    offset:        int            = Query(0),
    db: Session = Depends(get_db),
):
    q = db.query(RequestResponseLog)
    if function_name: q = q.filter(RequestResponseLog.function_name == function_name)
    if pii_detected is not None: q = q.filter(RequestResponseLog.pii_detected == pii_detected)
    if event_id:      q = q.filter(RequestResponseLog.event_id == event_id)
    total = q.count()
    rows  = q.order_by(RequestResponseLog.created_at.desc()).offset(offset).limit(limit).all()
    return {
        "total": total,
        "items": [
            {
                "id":                r.id,
                "event_id":          r.event_id,
                "function_name":     r.function_name,
                "input_preview":     r.input_preview,
                "output_preview":    r.output_preview,
                "input_size_bytes":  r.input_size_bytes,
                "output_size_bytes": r.output_size_bytes,
                "input_keys":        r.input_keys,
                "output_keys":       r.output_keys,
                "pii_detected":      r.pii_detected,
                "pii_fields":        r.pii_fields,
                "created_at":        r.created_at,
            }
            for r in rows
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
    # ── unpack ─────────────────────────────────────────────────────────────
    org_id        = payload.get("org_id") or payload.get("organization") or "unknown"
    project_id    = payload.get("project_id") or payload.get("project_name") or "unknown"
    tool_name     = payload.get("tool_name") or "unknown"
    function_name = payload.get("function_name") or "unknown"
    module_path   = payload.get("module_path")
    model_name    = payload.get("model_name")
    provider      = payload.get("provider") or "unknown"
    decorator_type= payload.get("decorator_type") or "fastapi_route"
    status        = payload.get("status") or "success"
    latency_ms    = int(payload.get("latency_ms") or 0)
    prompt_tokens = int(payload.get("input_tokens") or 0)
    completion_tokens = int(payload.get("output_tokens") or 0)
    total_tokens  = int(payload.get("total_tokens") or 0) or (prompt_tokens + completion_tokens)
    estimated_cost= float(payload.get("estimated_cost") or 0)
    contains_pii  = bool(payload.get("contains_pii", False))
    input_preview = payload.get("input_preview")
    output_preview= payload.get("output_preview")
    input_size_b  = int(float(payload.get("input_data_size_mb") or 0) * 1024 * 1024)
    output_size_b = int(float(payload.get("output_data_size_mb") or 0) * 1024 * 1024)
    metadata      = payload.get("metadata") or {}
    execution_env = metadata.get("execution_env") or payload.get("execution_env") or "production"
    logger_version= payload.get("logger_version") or "unknown"
    http_method   = payload.get("http_method") or metadata.get("http_method")
    http_path     = payload.get("http_path") or metadata.get("http_path")

    # ── 1. tool_api_inventory ──────────────────────────────────────────────
    inv = (
        db.query(ToolApiInventory)
        .filter_by(org_id=org_id, tool_name=tool_name, function_name=function_name)
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
        inv.avg_latency_ms = int(((inv.avg_latency_ms or 0) * (n - 1) + latency_ms) / n)

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
            usage.call_count              = (usage.call_count or 0) + 1
            usage.total_prompt_tokens     = (usage.total_prompt_tokens or 0) + prompt_tokens
            usage.total_completion_tokens = (usage.total_completion_tokens or 0) + completion_tokens
            usage.total_tokens            = (usage.total_tokens or 0) + total_tokens
            usage.total_cost              = float(usage.total_cost or 0) + estimated_cost
            n = usage.call_count or 1
            usage.avg_latency_ms = int(((usage.avg_latency_ms or 0) * (n - 1) + latency_ms) / n)
            if status == "success":
                usage.success_count = (usage.success_count or 0) + 1
            else:
                usage.error_count = (usage.error_count or 0) + 1

    # ── 4. request_response_logs ───────────────────────────────────────────
    http_context = None
    if http_method and http_path:
        http_context = f"{http_method} {http_path}"
    log = RequestResponseLog(
        function_name=function_name,
        input_preview=input_preview,
        output_preview=output_preview,
        input_size_bytes=input_size_b,
        output_size_bytes=output_size_b,
        input_keys=http_context,
        output_keys=model_name,
        pii_detected=contains_pii,
        pii_fields="detected in input/output" if contains_pii else None,
    )
    db.add(log)

    # ── 5. full governance pipeline ────────────────────────────────────────
    # Forward into _ingest_event so every decorator call also writes to
    # telemetry_events, cost_breakdown, data_security_logs, alerts, and
    # daily_org_summary — powering Dashboard, Cost, Alerts & Security, etc.
    try:
        input_mb  = Decimal(str(float(payload.get("input_data_size_mb")  or 0)))
        output_mb = Decimal(str(float(payload.get("output_data_size_mb") or 0)))
        # Use SDK-supplied cost only when no model_name is present (so the
        # CostEngine can't compute from model_pricing table).
        precomputed = Decimal(str(estimated_cost)) if (estimated_cost > 0 and not model_name) else None

        tec = TelemetryEventCreate(
            event_id=f"dec-{uuid.uuid4().hex}",
            org_id=org_id,
            project_id=project_id,
            tool_name=tool_name,
            provider=provider if provider != "unknown" else None,
            model_name=model_name,
            service_type=payload.get("service_type") or decorator_type,
            execution_type=decorator_type,
            status=status,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            contains_pii=contains_pii,
            pii_type=payload.get("pii_type"),
            input_data_size_mb=input_mb,
            output_data_size_mb=output_mb,
            input_preview=input_preview,
            output_preview=output_preview,
            precomputed_llm_cost=precomputed,
            metadata_json={
                "function_name": function_name,
                "module_path": module_path,
                "decorator_type": decorator_type,
                "execution_env": execution_env,
                **{k: v for k, v in metadata.items()
                   if isinstance(v, (str, int, float, bool, type(None)))},
            },
            tags=payload.get("tags") or [],
        )
        telemetry = _ingest_event(db, tec)
        log.event_id = telemetry.event_id  # link audit log → telemetry event
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
