from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import DataSecurityLog, UsageAnomaly
from app.routers.alerts_security import _enrich_anomalies
from app.schemas import DataSecurityLogResponse, UsageAnomalyResponse

router = APIRouter(prefix="/security", tags=["security"])


@router.get("/logs", response_model=list[DataSecurityLogResponse])
def list_security_logs(
    pii_detected: Optional[bool] = Query(None),
    misuse_detected: Optional[bool] = Query(None),
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(DataSecurityLog)
    if pii_detected is not None:
        query = query.filter(DataSecurityLog.pii_detected == pii_detected)
    if misuse_detected is not None:
        query = query.filter(DataSecurityLog.misuse_pattern_detected == misuse_detected)
    if org_id:
        query = query.filter(DataSecurityLog.org_id == org_id)
    if project_id:
        query = query.filter(DataSecurityLog.project_id == project_id)
    if start_date is not None:
        query = query.filter(DataSecurityLog.created_at >= datetime.combine(start_date, time.min))
    return query.order_by(DataSecurityLog.created_at.desc()).limit(100).all()


@router.get("/anomalies", response_model=list[UsageAnomalyResponse])
def list_usage_anomalies(
    status: Optional[str] = Query("open"),
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(UsageAnomaly)
    if status:
        query = query.filter(UsageAnomaly.status == status)
    if org_id:
        query = query.filter(UsageAnomaly.org_id == org_id)
    if project_id:
        query = query.filter(UsageAnomaly.project_id == project_id)
    if start_date is not None:
        query = query.filter(UsageAnomaly.created_at >= datetime.combine(start_date, time.min))
    rows = query.order_by(UsageAnomaly.created_at.desc()).limit(100).all()
    return _enrich_anomalies(db, rows)


@router.get("/summary")
def get_security_summary(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    cutoff_dt = datetime.combine(start_date, time.min) if start_date is not None else None

    def _log_q():
        q = db.query(func.count(DataSecurityLog.id))
        if org_id:
            q = q.filter(DataSecurityLog.org_id == org_id)
        if project_id:
            q = q.filter(DataSecurityLog.project_id == project_id)
        if cutoff_dt is not None:
            q = q.filter(DataSecurityLog.created_at >= cutoff_dt)
        return q

    total_events = _log_q().scalar() or 0
    total_with_pii = _log_q().filter(DataSecurityLog.pii_detected.is_(True)).scalar() or 0
    misuse_events = _log_q().filter(DataSecurityLog.misuse_pattern_detected.is_(True)).scalar() or 0
    data_out_events = _log_q().filter(DataSecurityLog.data_out_violation.is_(True)).scalar() or 0

    avg_risk_q = db.query(func.coalesce(func.avg(DataSecurityLog.risk_score), 0))
    max_risk_q = db.query(func.coalesce(func.max(DataSecurityLog.risk_score), 0))
    if org_id:
        avg_risk_q = avg_risk_q.filter(DataSecurityLog.org_id == org_id)
        max_risk_q = max_risk_q.filter(DataSecurityLog.org_id == org_id)
    if project_id:
        avg_risk_q = avg_risk_q.filter(DataSecurityLog.project_id == project_id)
        max_risk_q = max_risk_q.filter(DataSecurityLog.project_id == project_id)
    if cutoff_dt is not None:
        avg_risk_q = avg_risk_q.filter(DataSecurityLog.created_at >= cutoff_dt)
        max_risk_q = max_risk_q.filter(DataSecurityLog.created_at >= cutoff_dt)

    anomaly_q = db.query(func.count(UsageAnomaly.id)).filter(UsageAnomaly.status == "open")
    if org_id:
        anomaly_q = anomaly_q.filter(UsageAnomaly.org_id == org_id)
    if project_id:
        anomaly_q = anomaly_q.filter(UsageAnomaly.project_id == project_id)
    if cutoff_dt is not None:
        anomaly_q = anomaly_q.filter(UsageAnomaly.created_at >= cutoff_dt)

    return {
        "total_events": total_events,
        "total_with_pii": total_with_pii,
        "misuse_events": misuse_events,
        "data_out_events": data_out_events,
        "average_risk_score": Decimal(str(avg_risk_q.scalar() or 0)).quantize(Decimal("0.01")),
        "highest_risk_score": Decimal(str(max_risk_q.scalar() or 0)).quantize(Decimal("0.01")),
        "open_anomalies": anomaly_q.scalar() or 0,
    }
