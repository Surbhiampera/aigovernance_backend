from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Integer, case, cast, func
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import AiRequest, Alert, DataSecurityLog, UsageAnomaly

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
    try:
        q = db.query(
            func.count(DataSecurityLog.id).label("total_events"),
            func.sum(cast(DataSecurityLog.pii_detected, Integer)).label("total_with_pii"),
            func.sum(cast(DataSecurityLog.misuse_pattern_detected, Integer)).label("misuse_events"),
            func.sum(cast(DataSecurityLog.data_out_violation, Integer)).label("data_out_events"),
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

        # Proxy-side PII severity isn't backed by a numeric risk score, so map it
        # the same way the rest of the dashboard does: high/medium/low -> 0.9/0.5/0.2.
        risk_expr = case(
            (AiRequest.pii_severity == "high", 0.9),
            (AiRequest.pii_severity == "medium", 0.5),
            (AiRequest.pii_severity == "low", 0.2),
            else_=0.0,
        )

        # Also count/score PII events from proxy requests (AiRequest path) —
        # DataSecurityLog has no producer yet, so this is the only real signal.
        req_q = db.query(
            func.count(AiRequest.id).label("count"),
            func.sum(risk_expr).label("risk_sum"),
            func.max(risk_expr).label("risk_max"),
        ).filter(AiRequest.pii_detected == True)
        if org_id:
            req_q = req_q.filter(AiRequest.org_id == org_id)
        if project_id:
            req_q = req_q.filter(AiRequest.project_id == project_id)
        if start_date:
            req_q = req_q.filter(func.date(AiRequest.created_at) >= start_date)
        req_row = req_q.first()
        proxy_pii_count = req_row.count or 0
        proxy_risk_sum = float(req_row.risk_sum or 0)
        proxy_risk_max = float(req_row.risk_max or 0)

        ds_count = row.total_events or 0
        ds_risk_sum = float(row.average_risk_score or 0) * ds_count
        ds_risk_max = float(row.highest_risk_score or 0)

        combined_count = ds_count + proxy_pii_count
        combined_avg = (ds_risk_sum + proxy_risk_sum) / combined_count if combined_count else 0.0
        combined_peak = max(ds_risk_max, proxy_risk_max)

        return {
            "total_events":        (row.total_events or 0) + proxy_pii_count,
            "total_with_pii":      int(row.total_with_pii or 0) + proxy_pii_count,
            "misuse_events":       int(row.misuse_events or 0),
            "data_out_events":     int(row.data_out_events or 0),
            "average_risk_score":  round(combined_avg, 2),
            "highest_risk_score":  round(combined_peak, 2),
        }
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"security_summary query failed: {exc}") from exc


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
    try:
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

        # Fetch at most limit rows from each source to avoid unbounded memory use.
        dsl_rows = q.order_by(DataSecurityLog.created_at.desc()).limit(limit).all()

        combined = [
            {
                "id":                      r.id,
                "event_id":                r.event_id,
                "org_id":                  r.org_id,
                "project_id":              r.project_id,
                "pii_detected":            r.pii_detected,
                "pii_type":                r.pii_type,
                "data_out_violation":      r.data_out_violation,
                "misuse_pattern_detected": r.misuse_pattern_detected,
                "abnormal_usage_spike":    r.abnormal_usage_spike,
                "masking_applied":         r.masking_applied,
                "risk_score":              float(r.risk_score or 0),
                "data_in_mb":              float(r.data_in_mb or 0),
                "data_out_mb":             float(r.data_out_mb or 0),
                "created_at":              r.created_at.isoformat() if r.created_at else None,
            }
            for r in dsl_rows
        ]

        # Also pull PII events from proxy requests (pii_detected stored on AiRequest).
        # Skip when the caller explicitly filters for non-PII or misuse-only events.
        if pii_detected is not False and misuse_detected is not True:
            req_q = db.query(AiRequest).filter(AiRequest.pii_detected == True)
            if org_id:
                req_q = req_q.filter(AiRequest.org_id == org_id)
            if project_id:
                req_q = req_q.filter(AiRequest.project_id == project_id)
            if start_date:
                req_q = req_q.filter(func.date(AiRequest.created_at) >= start_date)
            _risk_map = {"high": 0.9, "medium": 0.5, "low": 0.2}
            for r in req_q.order_by(AiRequest.created_at.desc()).limit(limit).all():
                pii_types = r.pii_types or []
                combined.append({
                    "id":                      None,
                    "event_id":                r.request_id,
                    "org_id":                  r.org_id,
                    "project_id":              r.project_id,
                    "pii_detected":            True,
                    "pii_type":                pii_types[0] if pii_types else None,
                    "data_out_violation":      False,
                    "misuse_pattern_detected": False,
                    "abnormal_usage_spike":    False,
                    "masking_applied":         bool(r.pii_masked),
                    "risk_score":              _risk_map.get(r.pii_severity or "", 0.0),
                    "data_in_mb":              0.0,
                    "data_out_mb":             0.0,
                    "created_at":              r.created_at.isoformat() if r.created_at else None,
                })

        combined.sort(key=lambda x: x["created_at"] or "", reverse=True)
        return combined[offset: offset + limit]
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"security_logs query failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Open anomaly count (dashboard stat)
# ---------------------------------------------------------------------------
@router.get("/anomalies/open-count")
def open_anomaly_count(
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(func.count(UsageAnomaly.id)).filter(UsageAnomaly.status == "open")
    if org_id:
        q = q.filter(UsageAnomaly.org_id == org_id)
    if project_id:
        q = q.filter(UsageAnomaly.project_id == project_id)
    return {"open_anomalies": q.scalar() or 0}


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
    try:
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
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"security_anomalies query failed: {exc}") from exc


# ---------------------------------------------------------------------------
# Resolve an anomaly
# ---------------------------------------------------------------------------
@router.patch("/anomalies/{anomaly_id}/resolve")
def resolve_anomaly(
    anomaly_id: int,
    db: Session = Depends(get_db),
) -> dict:
    from fastapi import HTTPException
    anomaly = db.query(UsageAnomaly).filter(UsageAnomaly.id == anomaly_id).first()
    if not anomaly:
        raise HTTPException(status_code=404, detail="Anomaly not found")
    anomaly.status = "resolved"
    db.commit()
    return {"id": anomaly_id, "status": "resolved"}


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
