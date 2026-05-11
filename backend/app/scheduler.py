"""Scheduler — replaces Celery with APScheduler (no broker / Redis required).

All periodic work runs in background threads managed by APScheduler.
Tasks are pure functions in app.workers.tasks — no Celery decorators.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

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
        logger.info("Daily aggregation complete: %d rows", rows)
    except Exception as exc:
        logger.error("Daily aggregation error: %s", exc)
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
        logger.info("Monthly aggregation complete: %d rows", rows)
    except Exception as exc:
        logger.error("Monthly aggregation error: %s", exc)
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
        logger.info("Anomaly detection complete: %d anomalies created", created)
    except Exception as exc:
        logger.error("Anomaly detection error: %s", exc)
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
        logger.info("Alert scan complete: %d alerts created", created)
    except Exception as exc:
        logger.error("Alert scan error: %s", exc)
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
        logger.info("Connector poll complete: %s", result)
    except Exception as exc:
        logger.error("Connector poll error: %s", exc)
        db.rollback()
    finally:
        db.close()


# ─────────────────────── lifecycle ───────────────────────

def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone="UTC", job_defaults={"misfire_grace_time": 300})
    _scheduler.add_job(_job_daily_aggregation, "interval", hours=1, id="daily_agg", replace_existing=True)
    _scheduler.add_job(_job_monthly_aggregation, "interval", hours=24, id="monthly_agg", replace_existing=True)
    _scheduler.add_job(_job_anomaly_detection, "interval", minutes=30, id="anomaly_detection", replace_existing=True)
    _scheduler.add_job(_job_alert_scan, "interval", minutes=30, id="alert_scan", replace_existing=True)
    _scheduler.add_job(_job_connector_poll, "interval", minutes=15, id="connector_poll", replace_existing=True)
    _scheduler.start()
    logger.info("APScheduler started (daily_agg / monthly_agg / anomaly_detection / alert_scan / connector_poll)")
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped")
