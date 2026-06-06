"""Workers router — on-demand manual triggers for scheduled jobs.

The same jobs run automatically via APScheduler (app.scheduler).
These endpoints let operators trigger them manually from the UI or API.
"""
import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.services.alert_engine import AlertEngine
from app.services.langfuse_bridge import status as _langfuse_status
from app.workers.tasks import _detect_anomalies, _rebuild_daily_summary, _rebuild_monthly_summary

router = APIRouter(prefix="/workers", tags=["workers"])


@router.get("/langfuse/status")
def get_langfuse_status():
    """Diagnostic — reports whether the additive Langfuse mirror is active."""
    return _langfuse_status()


@router.post("/daily-aggregation/sync")
def trigger_daily_aggregation_sync(db: Session = Depends(get_db)):
    rows = _rebuild_daily_summary(db, datetime.date.today())
    db.commit()
    return {"status": "completed", "result": {"rows_processed": rows}}


@router.post("/monthly-aggregation/sync")
def trigger_monthly_aggregation_sync(db: Session = Depends(get_db)):
    rows = _rebuild_monthly_summary(db)
    db.commit()
    return {"status": "completed", "result": {"rows_processed": rows}}


@router.post("/anomaly-detection/sync")
def trigger_anomaly_detection_sync(db: Session = Depends(get_db)):
    created = _detect_anomalies(db)
    db.commit()
    return {"status": "completed", "result": {"anomalies_created": created}}


@router.post("/alert-scan/sync")
def trigger_alert_scan_sync(db: Session = Depends(get_db)):
    created = AlertEngine().create_daily_anomaly_alerts(db)
    db.commit()
    return {"status": "completed", "result": {"alerts_created": created}}


@router.post("/connector-poll/sync")
def trigger_connector_poll_sync(db: Session = Depends(get_db)):
    from app.workers.tasks import _run_connector_poll
    result = _run_connector_poll(db)
    db.commit()
    return {"status": "completed", "result": result}
