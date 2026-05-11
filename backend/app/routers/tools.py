from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import ConnectorSyncLog, TelemetryEvent, ToolConnector, ToolRegistry
from app.schemas import (
    ConnectorSyncLogResponse,
    ToolConnectorCreate,
    ToolConnectorResponse,
    ToolConnectorUpdate,
    ToolRegistryCreate,
    ToolRegistryResponse,
    ToolUsageResponse,
)

router = APIRouter(prefix="/tools", tags=["tools"])


@router.get("/", response_model=list[ToolRegistryResponse])
def list_tools(db: Session = Depends(get_db)):
    return db.query(ToolRegistry).order_by(ToolRegistry.tool_name.asc()).all()


@router.post("/register", response_model=ToolRegistryResponse)
def register_tool(tool_data: ToolRegistryCreate, db: Session = Depends(get_db)):
    tool = db.query(ToolRegistry).filter(ToolRegistry.tool_name == tool_data.tool_name).first()
    if tool:
        tool.tool_type = tool_data.tool_type
        tool.vendor = tool_data.vendor
        tool.cost_model = tool_data.cost_model
        tool.base_cost = tool_data.base_cost
    else:
        tool = ToolRegistry(
            tool_name=tool_data.tool_name,
            tool_type=tool_data.tool_type,
            vendor=tool_data.vendor,
            cost_model=tool_data.cost_model,
            base_cost=tool_data.base_cost,
        )
        db.add(tool)
    db.commit()
    db.refresh(tool)
    return tool


@router.get("/connectors", response_model=list[ToolConnectorResponse])
def list_connectors(db: Session = Depends(get_db)):
    return db.query(ToolConnector).order_by(ToolConnector.created_at.desc()).all()


@router.post("/connectors", response_model=ToolConnectorResponse)
def create_connector(connector_data: ToolConnectorCreate, db: Session = Depends(get_db)):
    connector = db.query(ToolConnector).filter(ToolConnector.connector_name == connector_data.connector_name).first()
    if connector:
        connector.tool_name = connector_data.tool_name
        connector.provider = connector_data.provider
        connector.endpoint_url = connector_data.endpoint_url
        connector.auth_type = connector_data.auth_type
        connector.ingestion_mode = connector_data.ingestion_mode
        connector.status = connector_data.status
        connector.org_id = connector_data.org_id
        connector.project_id = connector_data.project_id
        connector.sync_enabled = connector_data.sync_enabled
        connector.pull_interval_minutes = connector_data.pull_interval_minutes
        if connector_data.api_key is not None:
            connector.api_key = connector_data.api_key
    else:
        connector = ToolConnector(**connector_data.model_dump())
        db.add(connector)
    db.commit()
    db.refresh(connector)
    return connector


@router.patch("/connectors/{connector_id}", response_model=ToolConnectorResponse)
def update_connector(connector_id: int, update_data: ToolConnectorUpdate, db: Session = Depends(get_db)):
    connector = db.query(ToolConnector).filter(ToolConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    for field, value in update_data.model_dump(exclude_none=True).items():
        setattr(connector, field, value)
    db.commit()
    db.refresh(connector)
    return connector


@router.delete("/connectors/{connector_id}")
def delete_connector(connector_id: int, db: Session = Depends(get_db)):
    connector = db.query(ToolConnector).filter(ToolConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    db.query(ConnectorSyncLog).filter(ConnectorSyncLog.connector_id == connector_id).delete(synchronize_session=False)
    db.delete(connector)
    db.commit()
    return {"deleted": connector_id}


@router.get("/connectors/sync-logs", response_model=list[ConnectorSyncLogResponse])
def list_sync_logs(
    connector_id: int | None = Query(None),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
):
    q = db.query(ConnectorSyncLog).order_by(ConnectorSyncLog.created_at.desc())
    if connector_id is not None:
        q = q.filter(ConnectorSyncLog.connector_id == connector_id)
    return q.limit(limit).all()


@router.post("/connectors/{connector_id}/trigger-sync")
def trigger_connector_sync(connector_id: int, db: Session = Depends(get_db)):
    connector = db.query(ToolConnector).filter(ToolConnector.id == connector_id).first()
    if not connector:
        raise HTTPException(status_code=404, detail="Connector not found")
    from app.workers.tasks import _pull_connector
    import time, uuid
    from datetime import datetime
    from decimal import Decimal
    from app.models import ConnectorSyncLog as CSL, TelemetryEvent as TE
    from app.services.cost_engine import CostEngine
    from app.services.security_engine import SecurityEngine
    from app.services.alert_engine import AlertEngine

    t0 = time.time()
    raw_events, error = _pull_connector(connector)
    duration_ms = int((time.time() - t0) * 1000)

    events_ingested = 0
    for raw in raw_events:
        try:
            event_id = raw.get("event_id") or str(uuid.uuid4())
            if db.query(TE).filter(TE.event_id == event_id).first():
                continue
            row = TE(
                event_id=event_id,
                org_id=connector.org_id or raw.get("org_id", "default"),
                project_id=connector.project_id or raw.get("project_id"),
                tool_name=raw.get("tool_name", connector.tool_name),
                provider=raw.get("provider", connector.provider),
                model_name=raw.get("model_name"),
                status=raw.get("status", "success"),
                prompt_tokens=int(raw.get("prompt_tokens", 0)),
                completion_tokens=int(raw.get("completion_tokens", 0)),
                total_tokens=int(raw.get("total_tokens", 0)),
                latency_ms=int(raw.get("latency_ms", 0)),
                input_data_size_mb=Decimal(str(raw.get("input_data_size_mb", 0))),
                output_data_size_mb=Decimal(str(raw.get("output_data_size_mb", 0))),
                metadata_json=raw.get("metadata_json", {}),
            )
            db.add(row)
            db.flush()
            CostEngine().calculate(db, row)
            SecurityEngine().analyze(db, row, raw.get("contains_pii", False), raw.get("pii_type"))
            AlertEngine().evaluate(db, row)
            events_ingested += 1
        except Exception:
            pass

    sync_status = "error" if error and not events_ingested else ("no_data" if not events_ingested else "success")
    db.add(CSL(
        connector_id=connector.id,
        connector_name=connector.connector_name,
        sync_status=sync_status,
        events_pulled=events_ingested,
        error_message=error,
        duration_ms=duration_ms,
    ))
    connector.last_ingested_at = datetime.utcnow()
    connector.last_sync_status = sync_status
    connector.last_sync_error = error
    connector.total_events_pulled = (connector.total_events_pulled or 0) + events_ingested
    db.commit()
    return {"status": sync_status, "events_pulled": events_ingested, "duration_ms": duration_ms, "error": error}


@router.get("/usage", response_model=list[ToolUsageResponse])
def get_tool_usage(db: Session = Depends(get_db)):
    rows = (
        db.query(
            TelemetryEvent.model_name,
            func.max(ToolRegistry.vendor).label("vendor"),
            func.count(TelemetryEvent.id).label("total_events"),
            func.sum(TelemetryEvent.total_cost).label("total_cost"),
            func.sum(TelemetryEvent.total_tokens).label("total_tokens"),
            func.sum(TelemetryEvent.prompt_tokens).label("total_prompt_tokens"),
            func.sum(TelemetryEvent.completion_tokens).label("total_completion_tokens"),
            func.avg(TelemetryEvent.latency_ms).label("avg_latency_ms"),
            func.sum(case((TelemetryEvent.status.in_(["success", "completed"]), 1), else_=0)).label("success_count"),
        )
        .outerjoin(ToolRegistry, ToolRegistry.tool_name == TelemetryEvent.model_name)
        .group_by(TelemetryEvent.model_name)
        .order_by(func.sum(TelemetryEvent.total_cost).desc())
        .all()
    )

    results = []
    for row in rows:
        total_events = row.total_events or 0
        success_rate = Decimal("100") if total_events == 0 else (Decimal(str(row.success_count or 0)) / Decimal(str(total_events))) * Decimal("100")
        results.append(
            ToolUsageResponse(
                tool_name=row.model_name or "",
                vendor=row.vendor,
                total_events=total_events,
                total_cost=Decimal(str(row.total_cost or 0)),
                total_tokens=Decimal(str(row.total_tokens or 0)),
                total_prompt_tokens=Decimal(str(row.total_prompt_tokens or 0)),
                total_completion_tokens=Decimal(str(row.total_completion_tokens or 0)),
                avg_latency_ms=Decimal(str(row.avg_latency_ms or 0)).quantize(Decimal("0.01")),
                success_rate=success_rate.quantize(Decimal("0.01")),
            )
        )
    return results
