"""APScheduler — runs periodic background jobs inside the FastAPI process.

Two jobs:
  daily_agg   — every hour: aggregate AiRequest + RequestCost → DailyOrgSummary
  monthly_agg — every 24 hours: roll up DailyOrgSummary → MonthlyOrgSummary
"""
from __future__ import annotations

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import get_scheduler_max_workers

_scheduler: BackgroundScheduler | None = None


def _job_daily_aggregation() -> None:
    import datetime
    from app.database import SessionLocal
    from app.workers.tasks import _rebuild_daily_summary

    db = SessionLocal()
    try:
        _rebuild_daily_summary(db=db, summary_date=datetime.date.today())
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
        _rebuild_monthly_summary(db=db)
        db.commit()
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
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
