"""APScheduler — runs periodic background jobs inside the FastAPI process.

Two jobs:
  daily_agg   — every hour: aggregate AiRequest + RequestCost → DailyOrgSummary
  monthly_agg — every 24 hours: roll up DailyOrgSummary → MonthlyOrgSummary
"""
from __future__ import annotations

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import (
    get_license_check_interval_seconds,
    get_license_enforcement_enabled,
    get_scheduler_max_workers,
)

_scheduler: BackgroundScheduler | None = None

# Last successful run per job, for health-check staleness alerts — a hung
# scheduler thread fails silently otherwise (no exception, just stops ticking).
_last_success: dict[str, "datetime.datetime | None"] = {
    "daily_agg": None,
    "monthly_agg": None,
    "license_check": None,
}


def get_scheduler_heartbeat() -> dict:
    return {
        "running": bool(_scheduler and _scheduler.running),
        "last_success": {
            job_id: (ts.isoformat() if ts else None)
            for job_id, ts in _last_success.items()
        },
    }


def _job_daily_aggregation() -> None:
    import datetime
    import logging
    from app.database import SessionLocal
    from app.workers.tasks import (
        _detect_daily_anomalies,
        _rebuild_daily_summary,
        _rebuild_daily_user_summary,
    )

    _log = logging.getLogger(__name__)
    today = datetime.date.today()
    db = SessionLocal()
    try:
        _rebuild_daily_summary(db=db, summary_date=today)
        db.flush()
        _rebuild_daily_user_summary(db=db, summary_date=today)
        db.flush()
        _detect_daily_anomalies(db=db, summary_date=today)
        db.commit()
        _last_success["daily_agg"] = datetime.datetime.utcnow()
    except Exception:
        _log.exception("daily_aggregation job failed")
        db.rollback()
    finally:
        db.close()


def _job_license_check() -> None:
    """Re-verify the license file so expiry/renewal is caught without a restart."""
    import datetime

    from app.services.license_service import refresh_license_status

    refresh_license_status()
    _last_success["license_check"] = datetime.datetime.utcnow()


def _job_monthly_aggregation() -> None:
    import datetime

    from app.database import SessionLocal
    from app.workers.tasks import _rebuild_monthly_summary

    db = SessionLocal()
    try:
        _rebuild_monthly_summary(db=db)
        db.commit()
        _last_success["monthly_agg"] = datetime.datetime.utcnow()
    except Exception:
        db.rollback()
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler and _scheduler.running:
        return _scheduler

    _scheduler = BackgroundScheduler(
        timezone="UTC",
        executors={"default": ThreadPoolExecutor(max_workers=get_scheduler_max_workers())},
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 300},
    )
    _scheduler.add_job(
        _job_daily_aggregation, "interval", hours=1,
        id="daily_agg", replace_existing=True,
    )
    _scheduler.add_job(
        _job_monthly_aggregation, "interval", hours=24,
        id="monthly_agg", replace_existing=True,
    )
    if get_license_enforcement_enabled():
        _scheduler.add_job(
            _job_license_check, "interval", seconds=get_license_check_interval_seconds(),
            id="license_check", replace_existing=True,
        )
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
