from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import Alert, Organization, Project, TelemetryEvent
from app.schemas import AlertResponse

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _enrich_alerts(db: Session, alerts: list[Alert]) -> list[dict]:
    """Hydrate alerts with org_name / project_name / model_name so every
    alert is traceable back to its organization, project, tool and model
    — same linkage the Super Admin Log module surfaces.
    """
    if not alerts:
        return []

    org_ids = {a.org_id for a in alerts if a.org_id}
    project_ids = {a.project_id for a in alerts if a.project_id}
    telemetry_ids = {a.telemetry_id for a in alerts if a.telemetry_id}

    org_name_map = {
        o.id: o.org_name
        for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()
    } if org_ids else {}
    project_name_map = {
        p.id: p.project_name
        for p in db.query(Project).filter(Project.id.in_(project_ids)).all()
    } if project_ids else {}
    telemetry_map = {
        t.id: t
        for t in db.query(TelemetryEvent).filter(TelemetryEvent.id.in_(telemetry_ids)).all()
    } if telemetry_ids else {}

    out: list[dict] = []
    for a in alerts:
        evt = telemetry_map.get(a.telemetry_id) if a.telemetry_id else None
        model_name = (evt.model_name if evt else None) or a.tool_name
        out.append({
            "id": a.id,
            "org_id": a.org_id,
            "org_name": org_name_map.get(a.org_id, a.org_id),
            "project_id": a.project_id,
            "project_name": project_name_map.get(a.project_id, a.project_id),
            "tool_name": a.tool_name or model_name,
            "model_name": model_name,
            "rule_id": a.rule_id,
            "alert_type": a.alert_type,
            "severity": a.severity,
            "message": a.message,
            "threshold_value": a.threshold_value,
            "actual_value": a.actual_value,
            "status": a.status,
            "telemetry_id": a.telemetry_id,
            "created_at": a.created_at,
        })
    return out


@router.get("/", response_model=list[AlertResponse])
def list_alerts(
    status: Optional[str] = Query("active"),
    org_id: Optional[str] = Query(None),
    project_id: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    query = db.query(Alert)
    if status:
        query = query.filter(Alert.status == status)
    if org_id:
        query = query.filter(Alert.org_id == org_id)
    if project_id:
        query = query.filter(Alert.project_id == project_id)
    rows = query.order_by(Alert.created_at.desc()).all()
    return _enrich_alerts(db, rows)


@router.patch("/{alert_id}/resolve", response_model=AlertResponse)
def resolve_alert(alert_id: int, db: Session = Depends(get_db)):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.status = "resolved"
    db.commit()
    db.refresh(alert)
    return _enrich_alerts(db, [alert])[0]
