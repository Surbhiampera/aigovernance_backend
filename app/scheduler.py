"""APScheduler — runs all periodic jobs as background threads inside the FastAPI process."""
from __future__ import annotations

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_scheduler_max_workers

_scheduler: BackgroundScheduler | None = None


# ─────────────────────── job wrappers ───────────────────────

def _job_daily_aggregation() -> None:
    import datetime
    from app.database import SessionLocal
    from app.workers.tasks import _rebuild_daily_summary

    db = SessionLocal()
    try:
        rows = _rebuild_daily_summary(db, datetime.date.today())
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _job_monthly_aggregation() -> None:
    from app.database import SessionLocal
    from app.workers.tasks import _rebuild_monthly_summary

    db = SessionLocal()
    try:
        rows = _rebuild_monthly_summary(db)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _job_anomaly_detection() -> None:
    from app.database import SessionLocal
    from app.workers.tasks import _detect_anomalies

    db = SessionLocal()
    try:
        created = _detect_anomalies(db)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _job_alert_scan() -> None:
    from app.database import SessionLocal
    from app.services.alert_engine import AlertEngine

    db = SessionLocal()
    try:
        created = AlertEngine().create_daily_anomaly_alerts(db)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _job_connector_poll() -> None:
    from app.database import SessionLocal
    from app.workers.tasks import _run_connector_poll

    db = SessionLocal()
    try:
        result = _run_connector_poll(db)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


# ─────────────────────── lifecycle ───────────────────────

def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    job_defaults = {
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 300,
    }
    executors = {"default": ThreadPoolExecutor(max_workers=get_scheduler_max_workers())}
    _scheduler = BackgroundScheduler(
        timezone="UTC",
        executors=executors,
        job_defaults=job_defaults,
    )
    _scheduler.add_job(_job_daily_aggregation, "interval", hours=1, id="daily_agg", replace_existing=True)
    _scheduler.add_job(_job_monthly_aggregation, "interval", hours=24, id="monthly_agg", replace_existing=True)
    _scheduler.add_job(_job_anomaly_detection, "interval", minutes=30, id="anomaly_detection", replace_existing=True)
    _scheduler.add_job(_job_alert_scan, "interval", minutes=30, id="alert_scan", replace_existing=True)
    _scheduler.add_job(_job_connector_poll, "interval", minutes=15, id="connector_poll", replace_existing=True)
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
