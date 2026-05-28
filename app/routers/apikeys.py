from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import (
    Alert, ApiKey, CostBreakdown, DataSecurityLog, ExecutionPipeline,
    TelemetryEvent, TraceModelUsage, TraceToolUsage,
)
from app.schemas import ApiKeyCreate, ApiKeyResponse
from decorator.models import RequestResponseLog

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


@router.get("/", response_model=list[ApiKeyResponse])
def list_api_keys(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(ApiKey)
    if org_id:
        query = query.filter(ApiKey.org_id == org_id)
    if project_id:
        query = query.filter(ApiKey.project_id == project_id)
    return query.all()


@router.post("/", response_model=ApiKeyResponse)
def create_api_key(data: ApiKeyCreate, db: Session = Depends(get_db)):
    key = ApiKey(
        id=data.id,
        org_id=data.org_id,
        project_id=data.project_id,
        key_name=data.key_name,
        provider=data.provider,
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


@router.delete("/{key_id}")
def delete_api_key(key_id: str, db: Session = Depends(get_db)):
    key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not key:
        raise HTTPException(status_code=404, detail="API key not found")

    try:
        event_ids_subq = (
            db.query(TelemetryEvent.event_id).filter(TelemetryEvent.api_key_id == key_id).subquery()
        )
        telemetry_ids_subq = (
            db.query(TelemetryEvent.id).filter(TelemetryEvent.api_key_id == key_id).subquery()
        )

        db.query(DataSecurityLog).filter(DataSecurityLog.event_id.in_(event_ids_subq)).delete(synchronize_session=False)
        db.query(Alert).filter(Alert.telemetry_id.in_(telemetry_ids_subq)).delete(synchronize_session=False)
        db.query(CostBreakdown).filter(CostBreakdown.event_id.in_(event_ids_subq)).delete(synchronize_session=False)
        db.query(ExecutionPipeline).filter(ExecutionPipeline.event_id.in_(event_ids_subq)).delete(synchronize_session=False)
        db.query(TraceModelUsage).filter(TraceModelUsage.event_id.in_(event_ids_subq)).delete(synchronize_session=False)
        db.query(TraceToolUsage).filter(TraceToolUsage.event_id.in_(event_ids_subq)).delete(synchronize_session=False)
        db.query(RequestResponseLog).filter(RequestResponseLog.event_id.in_(event_ids_subq)).delete(synchronize_session=False)
        db.query(TelemetryEvent).filter(TelemetryEvent.api_key_id == key_id).delete(synchronize_session=False)

        db.delete(key)
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    return {"detail": "API key deleted"}
