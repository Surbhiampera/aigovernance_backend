from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import Alert, DataSecurityLog, UsageAnomaly

router = APIRouter(prefix="/alerts-security", tags=["alerts-security"])


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
@router.get("/summary")
def security_summary(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(
        func.count(DataSecurityLog.id).label("total_events"),
        func.sum(DataSecurityLog.pii_detected.cast("int")).label("total_with_pii"),
        func.sum(DataSecurityLog.misuse_pattern_detected.cast("int")).label("misuse_events"),
        func.sum(DataSecurityLog.data_out_violation.cast("int")).label("data_out_events"),
        func.avg(DataSecurityLog.risk_score).label("average_risk_score"),
        func.max(DataSecurityLog.risk_score).label("highest_risk_score"),
    )
    if org_id:
        q = q.filter(DataSecurityLog.org_id == org_id)
    if project_id:
        q = q.filter(DataSecurityLog.project_id == project_id)
    if start_date:
        q = q.filter(func.date(DataSecurityLog.created_at) >= start_date)

    row = q.first()
    return {
        "total_events":        row.total_events or 0,
        "total_with_pii":      int(row.total_with_pii or 0),
        "misuse_events":       int(row.misuse_events or 0),
        "data_out_events":     int(row.data_out_events or 0),
        "average_risk_score":  float(row.average_risk_score or 0),
        "highest_risk_score":  float(row.highest_risk_score or 0),
    }


# ---------------------------------------------------------------------------
# Logs
# ---------------------------------------------------------------------------
@router.get("/logs")
def security_logs(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    pii_detected: Optional[bool] = Query(None),
    misuse_detected: Optional[bool] = Query(None),
    start_date: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list:
    q = db.query(DataSecurityLog)
    if org_id:
        q = q.filter(DataSecurityLog.org_id == org_id)
    if project_id:
        q = q.filter(DataSecurityLog.project_id == project_id)
    if pii_detected is not None:
        q = q.filter(DataSecurityLog.pii_detected == pii_detected)
    if misuse_detected is not None:
        q = q.filter(DataSecurityLog.misuse_pattern_detected == misuse_detected)
    if start_date:
        q = q.filter(func.date(DataSecurityLog.created_at) >= start_date)

    rows = q.order_by(DataSecurityLog.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "id":                     r.id,
            "event_id":               r.event_id,
            "org_id":                 r.org_id,
            "project_id":             r.project_id,
            "pii_detected":           r.pii_detected,
            "pii_type":               r.pii_type,
            "data_out_violation":     r.data_out_violation,
            "misuse_pattern_detected": r.misuse_pattern_detected,
            "abnormal_usage_spike":   r.abnormal_usage_spike,
            "masking_applied":        r.masking_applied,
            "risk_score":             float(r.risk_score or 0),
            "data_in_mb":             float(r.data_in_mb or 0),
            "data_out_mb":            float(r.data_out_mb or 0),
            "created_at":             r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Anomalies
# ---------------------------------------------------------------------------
@router.get("/anomalies")
def security_anomalies(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list:
    q = db.query(UsageAnomaly)
    if org_id:
        q = q.filter(UsageAnomaly.org_id == org_id)
    if project_id:
        q = q.filter(UsageAnomaly.project_id == project_id)
    if status:
        q = q.filter(UsageAnomaly.status == status)
    if start_date:
        q = q.filter(func.date(UsageAnomaly.created_at) >= start_date)

    rows = q.order_by(UsageAnomaly.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "id":             r.id,
            "org_id":         r.org_id,
            "project_id":     r.project_id,
            "tool_name":      r.tool_name,
            "event_id":       r.event_id,
            "anomaly_type":   r.anomaly_type,
            "severity":       r.severity,
            "anomaly_score":  float(r.anomaly_score or 0),
            "baseline_value": float(r.baseline_value or 0),
            "observed_value": float(r.observed_value or 0),
            "message":        r.message,
            "status":         r.status,
            "created_at":     r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Alerts (proxy to existing Alert model)
# ---------------------------------------------------------------------------
@router.get("/alerts")
def security_alerts(
    status: Optional[str] = Query(None),
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    start_date: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> list:
    q = db.query(Alert)
    if org_id:
        q = q.filter(Alert.org_id == org_id)
    if project_id:
        q = q.filter(Alert.project_id == project_id)
    if status:
        q = q.filter(Alert.status == status)
    if start_date:
        q = q.filter(func.date(Alert.created_at) >= start_date)

    rows = q.order_by(Alert.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "id":           r.id,
            "org_id":       r.org_id,
            "project_id":   r.project_id,
            "alert_type":   r.alert_type,
            "severity":     r.severity,
            "message":      r.message,
            "status":       r.status,
            "created_at":   r.created_at.isoformat() if r.created_at else None,
            "resolved_at":  r.resolved_at.isoformat() if getattr(r, "resolved_at", None) else None,
        }
        for r in rows
    ]


@router.patch("/alerts/{alert_id}/resolve")
def resolve_alert(
    alert_id: int,
    db: Session = Depends(get_db),
) -> dict:
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.status = "resolved"
    if hasattr(alert, "resolved_at"):
        alert.resolved_at = datetime.utcnow()
    db.commit()
    return {"id": alert_id, "status": "resolved"}
