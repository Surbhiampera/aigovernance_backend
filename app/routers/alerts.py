"""Alerts router — budget and quota threshold alerts from proxy request data.

Alerts are created by the scheduler (workers/tasks.py) when:
  - Project cost exceeds a budget threshold percentage
  - Request volume exceeds a configured quota

No dependency on TelemetryEvent. All traceability is via org_id / project_id.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import Alert, Organization, Project

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _serialize(*, alert: Alert, org_name: Optional[str], project_name: Optional[str]) -> dict:
    return {
        "id": alert.id,
        "org_id": alert.org_id,
        "org_name": org_name or alert.org_id,
        "project_id": alert.project_id,
        "project_name": project_name or alert.project_id,
        "alert_type": alert.alert_type,
        "severity": alert.severity,
        "message": alert.message,
        "threshold_value": float(alert.threshold_value) if alert.threshold_value is not None else None,
        "actual_value": float(alert.actual_value) if alert.actual_value is not None else None,
        "status": alert.status,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
    }


def _enrich(*, db: Session, alerts: list[Alert]) -> list[dict]:
    if not alerts:
        return []

    org_ids = {a.org_id for a in alerts if a.org_id}
    project_ids = {a.project_id for a in alerts if a.project_id}

    org_names = {
        o.id: o.org_name
        for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()
    } if org_ids else {}

    project_names = {
        p.id: p.project_name
        for p in db.query(Project).filter(Project.id.in_(project_ids)).all()
    } if project_ids else {}

    return [
        _serialize(
            alert=a,
            org_name=org_names.get(a.org_id),
            project_name=project_names.get(a.project_id),
        )
        for a in alerts
    ]


# ---------------------------------------------------------------------------
# List alerts
# ---------------------------------------------------------------------------

@router.get("/")
def list_alerts(
    *,
    status: Optional[str] = Query("active"),
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    alert_type: Optional[str] = Query(None),
    severity: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    q = db.query(Alert)
    if status:
        q = q.filter(Alert.status == status)
    if org_id:
        q = q.filter(Alert.org_id == org_id)
    if project_id:
        q = q.filter(Alert.project_id == project_id)
    if alert_type:
        q = q.filter(Alert.alert_type == alert_type)
    if severity:
        q = q.filter(Alert.severity == severity)

    total = q.count()
    rows = q.order_by(Alert.created_at.desc()).offset(offset).limit(limit).all()

    return {"total": total, "offset": offset, "items": _enrich(db=db, alerts=rows)}


# ---------------------------------------------------------------------------
# Resolve alert
# ---------------------------------------------------------------------------

@router.patch("/{alert_id}/resolve")
def resolve_alert(
    *,
    alert_id: int,
    db: Session = Depends(get_db),
) -> dict:
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found.")
    alert.status = "resolved"
    db.commit()
    db.refresh(alert)
    return _enrich(db=db, alerts=[alert])[0]


# ---------------------------------------------------------------------------
# Dismiss alert
# ---------------------------------------------------------------------------

@router.patch("/{alert_id}/dismiss")
def dismiss_alert(
    *,
    alert_id: int,
    db: Session = Depends(get_db),
) -> dict:
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found.")
    alert.status = "dismissed"
    db.commit()
    db.refresh(alert)
    return _enrich(db=db, alerts=[alert])[0]


# ---------------------------------------------------------------------------
# Alert counts by severity (for dashboard badge)
# ---------------------------------------------------------------------------

@router.get("/counts")
def alert_counts(
    *,
    org_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
) -> dict:
    from sqlalchemy import func

    q = db.query(Alert.severity, func.count(Alert.id)).filter(Alert.status == "active")
    if org_id:
        q = q.filter(Alert.org_id == org_id)
    rows = q.group_by(Alert.severity).all()

    counts = {severity: count for severity, count in rows}
    return {
        "critical": counts.get("critical", 0),
        "high": counts.get("high", 0),
        "medium": counts.get("medium", 0),
        "low": counts.get("low", 0),
        "total": sum(counts.values()),
    }
