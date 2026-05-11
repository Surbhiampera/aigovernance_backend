from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import Alert, DataSecurityLog, Organization, Project, TelemetryEvent, UsageAnomaly
from app.routers.alerts import _enrich_alerts
from app.schemas import AlertResponse, DataSecurityLogResponse, UsageAnomalyResponse

router = APIRouter(prefix="/alerts-security", tags=["alerts & security"])


# ── Alerts ──────────────────────────────────────────────

@router.get("/alerts", response_model=list[AlertResponse])
def list_alerts(
    status: Optional[str] = Query("active"),
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    start_date: Optional[date] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(Alert)
    if status:
        query = query.filter(Alert.status == status)
    if org_id:
        query = query.filter(Alert.org_id == org_id)
    if project_id:
        query = query.filter(Alert.project_id == project_id)
    if start_date is not None:
        query = query.filter(Alert.created_at >= datetime.combine(start_date, time.min))
    rows = query.order_by(Alert.created_at.desc()).all()
    return _enrich_alerts(db, rows)


@router.patch("/alerts/{alert_id}/resolve", response_model=AlertResponse)
def resolve_alert(alert_id: int, db: Session = Depends(get_db)):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.status = "resolved"
    db.commit()
    db.refresh(alert)
    return _enrich_alerts(db, [alert])[0]


def _enrich_anomalies(db: Session, anomalies: list[UsageAnomaly]) -> list[dict]:
    """Hydrate UsageAnomaly rows with org_name / project_name so the
    consumer can trace each anomaly back to its exact organization/project,
    matching the Super Admin Log linkage.
    """
    if not anomalies:
        return []
    org_ids = {a.org_id for a in anomalies if a.org_id}
    project_ids = {a.project_id for a in anomalies if a.project_id}
    org_name_map = {
        o.id: o.org_name
        for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()
    } if org_ids else {}
    project_name_map = {
        p.id: p.project_name
        for p in db.query(Project).filter(Project.id.in_(project_ids)).all()
    } if project_ids else {}
    out: list[dict] = []
    for a in anomalies:
        out.append({
            "id": a.id,
            "org_id": a.org_id,
            "org_name": org_name_map.get(a.org_id, a.org_id),
            "project_id": a.project_id,
            "project_name": project_name_map.get(a.project_id, a.project_id),
            "tool_name": a.tool_name,
            "event_id": a.event_id,
            "anomaly_type": a.anomaly_type,
            "severity": a.severity,
            "anomaly_score": a.anomaly_score,
            "baseline_value": a.baseline_value,
            "observed_value": a.observed_value,
            "message": a.message,
            "status": a.status,
            "created_at": a.created_at,
        })
    return out


# ── Security ────────────────────────────────────────────

@router.get("/logs")
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
    logs = query.order_by(DataSecurityLog.created_at.desc()).limit(100).all()

    # Batch-fetch telemetry context to avoid N+1 queries
    event_ids = [l.event_id for l in logs if l.event_id]
    event_map = {
        e.event_id: e
        for e in db.query(TelemetryEvent).filter(TelemetryEvent.event_id.in_(event_ids)).all()
    } if event_ids else {}

    org_ids = list({e.org_id for e in event_map.values() if e.org_id})
    project_ids = list({e.project_id for e in event_map.values() if e.project_id})
    org_name_map = {
        o.id: o.org_name
        for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()
    } if org_ids else {}
    project_name_map = {
        p.id: p.project_name
        for p in db.query(Project).filter(Project.id.in_(project_ids)).all()
    } if project_ids else {}

    result = []
    for log in logs:
        event = event_map.get(log.event_id)
        result.append({
            "id": log.id,
            "event_id": log.event_id,
            "created_at": log.created_at,
            "pii_detected": log.pii_detected,
            "pii_type": log.pii_type,
            "data_out_violation": log.data_out_violation,
            "misuse_pattern_detected": log.misuse_pattern_detected,
            "abnormal_usage_spike": log.abnormal_usage_spike,
            "masking_applied": log.masking_applied,
            "risk_score": float(log.risk_score or 0),
            "data_in_mb": float(log.data_in_mb or 0),
            "data_out_mb": float(log.data_out_mb or 0),
            "org_id": event.org_id if event else None,
            "org_name": org_name_map.get(event.org_id, event.org_id) if event else None,
            "project_id": event.project_id if event else None,
            "project_name": project_name_map.get(event.project_id, event.project_id) if event else None,
            "model_name": event.model_name if event else None,
            "provider": event.provider if event else None,
        })
    return result


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
    avg_risk = avg_risk_q.scalar() or 0
    highest_risk = max_risk_q.scalar() or 0

    anomaly_q = db.query(func.count(UsageAnomaly.id)).filter(UsageAnomaly.status == "open")
    alert_q = db.query(func.count(Alert.id)).filter(Alert.status == "active")
    if org_id:
        anomaly_q = anomaly_q.filter(UsageAnomaly.org_id == org_id)
        alert_q = alert_q.filter(Alert.org_id == org_id)
    if project_id:
        anomaly_q = anomaly_q.filter(UsageAnomaly.project_id == project_id)
        alert_q = alert_q.filter(Alert.project_id == project_id)
    if cutoff_dt is not None:
        anomaly_q = anomaly_q.filter(UsageAnomaly.created_at >= cutoff_dt)
        alert_q = alert_q.filter(Alert.created_at >= cutoff_dt)

    return {
        "total_events": total_events,
        "total_with_pii": total_with_pii,
        "misuse_events": misuse_events,
        "data_out_events": data_out_events,
        "average_risk_score": Decimal(str(avg_risk)).quantize(Decimal("0.01")),
        "highest_risk_score": Decimal(str(highest_risk)).quantize(Decimal("0.01")),
        "open_anomalies": anomaly_q.scalar() or 0,
        "active_alerts": alert_q.scalar() or 0,
    }
