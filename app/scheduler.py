"""APScheduler — runs periodic background jobs inside the FastAPI process.

Jobs:
  daily_agg          — every hour: aggregate AiRequest + RequestCost → DailyOrgSummary
  monthly_agg        — every 24 hours: roll up DailyOrgSummary → MonthlyOrgSummary
  optimization_tips  — every 24 hours: evaluate tip rules → OptimizationTip rows
  startup_backfill   — once, 5s after boot: fill gaps in the daily rollups
"""
from __future__ import annotations

import datetime

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import (
    get_db_health_check_enabled,
    get_db_health_check_interval_seconds,
    get_db_size_warning_gb,
    get_license_check_interval_seconds,
    get_license_enforcement_enabled,
    get_scheduler_max_workers,
)

_scheduler: BackgroundScheduler | None = None

# Last successful run per job, for health-check staleness alerts — a hung
# scheduler thread fails silently otherwise (no exception, just stops ticking).
_last_success: dict[str, datetime.datetime | None] = {
    "daily_agg": None,
    "monthly_agg": None,
    "license_check": None,
    "db_health_check": None,
    "optimization_tips": None,
    "startup_backfill": None,
}

# Per-process, once-a-day dedup so a 15-day renewal window or an ongoing
# outage doesn't send a notification every single scheduler tick — each
# uvicorn worker tracks this independently, so a duplicate at most once per
# worker per day is possible; accepted as a minor tradeoff over adding
# cross-worker coordination for a low-volume alert.
_last_notified_date: dict[str, "datetime.date | None"] = {
    "license_renewal": None,
    "license_expired": None,
    "db_unreachable": None,
    "db_size_warning": None,
}


def _notify_once_per_day(key: str, alert_type: str, severity: str, message: str) -> None:
    import datetime

    from app.services.notification_service import notification_service

    today = datetime.date.today()
    if _last_notified_date.get(key) == today:
        return
    notification_service.notify(alert_type, severity, message)
    _last_notified_date[key] = today


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
    """Re-verify the license file so expiry/renewal is caught without a restart,
    and notify (email/Teams) once a day while it's expired or in the renewal
    window — this deployment's own SMTP/Teams credentials, nobody else's."""
    import datetime

    from app.services.license_service import refresh_license_status

    status = refresh_license_status()

    if status.expired or status.revoked:
        reason = "revoked" if status.revoked else "expired"
        _notify_once_per_day(
            "license_expired", "license_expired", "critical",
            f"License for customer={status.customer} (license_id={status.license_id}) is {reason}. "
            "The admin dashboard is frozen until a new license is installed; AI traffic is unaffected.",
        )
    elif status.show_renewal_banner:
        _notify_once_per_day(
            "license_renewal", "license_renewal", "high",
            f"License for customer={status.customer} (license_id={status.license_id}) expires in "
            f"{status.days_until_expiry} day(s). Renew via POST /license/upload before it lapses.",
        )

    _last_success["license_check"] = datetime.datetime.utcnow()


def _job_db_health_check() -> None:
    """Check the database is reachable and hasn't grown past a sanity
    threshold; notify on failure. Does not (cannot, from in here) check host
    disk space or backup freshness — see OPERATIONS.md."""
    import datetime

    from sqlalchemy import text

    from app.database import SessionLocal

    db = SessionLocal()
    try:
        size_bytes = db.execute(text("SELECT pg_database_size(current_database())")).scalar()
        _last_success["db_health_check"] = datetime.datetime.utcnow()

        size_gb = size_bytes / (1024 ** 3)
        warning_gb = get_db_size_warning_gb()
        if size_gb >= warning_gb:
            _notify_once_per_day(
                "db_size_warning", "db_size_warning", "high",
                f"Database size is {size_gb:.1f} GB, at or above the {warning_gb:.0f} GB warning "
                "threshold (DB_SIZE_WARNING_GB). Check disk space on this host.",
            )
    except Exception as exc:
        _notify_once_per_day(
            "db_unreachable", "db_unreachable", "critical",
            f"Database health check failed: {exc}",
        )
    finally:
        db.close()


def _job_startup_backfill() -> None:
    """One-shot: fill in DailyOrgSummary/DailyUserUsage for past days that have
    AiRequest rows but no summary.

    Runs on a scheduler thread rather than inline in the FastAPI lifespan — with
    up to 90 days to rebuild this can take minutes, and Uvicorn does not bind the
    port until lifespan startup returns.
    """
    import datetime
    import logging

    from sqlalchemy import func

    from app.database import SessionLocal
    from app.models import AiRequest, DailyOrgSummary, DailyUserUsage
    from app.workers.tasks import (
        _detect_daily_anomalies,
        _rebuild_daily_summary,
        _rebuild_daily_user_summary,
    )

    _log = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        dates_with_requests = (
            db.query(func.date(AiRequest.created_at).label("d"))
            .filter(AiRequest.created_at >= datetime.datetime.utcnow() - datetime.timedelta(days=90))
            .distinct()
            .all()
        )
        dates_already_summarised = {r[0] for r in db.query(DailyOrgSummary.date).distinct().all()}
        dates_already_user_summarised = {r[0] for r in db.query(DailyUserUsage.date).distinct().all()}

        for (d,) in dates_with_requests:
            if d not in dates_already_summarised:
                try:
                    _rebuild_daily_summary(db=db, summary_date=d)
                    _detect_daily_anomalies(db=db, summary_date=d)
                    db.commit()
                    _log.info("Backfilled DailyOrgSummary for %s", d)
                except Exception:
                    db.rollback()
                    _log.exception("Backfill failed for %s", d)
            if d not in dates_already_user_summarised:
                try:
                    _rebuild_daily_user_summary(db=db, summary_date=d)
                    db.commit()
                except Exception:
                    db.rollback()
                    _log.exception("DailyUserUsage backfill failed for %s", d)

        _last_success["startup_backfill"] = datetime.datetime.utcnow()
    except Exception:
        db.rollback()
        _log.exception("Startup backfill failed")
    finally:
        db.close()


def _job_optimization_tips() -> None:
    """Rule 5 (cache_opportunity) runs the heaviest query in the system — a
    sha256 hash-and-group over ai_requests — so this stays a separate daily
    job rather than riding along with _job_daily_aggregation, which must not
    be delayed by it. The thread pool is only SCHEDULER_MAX_WORKERS wide.
    """
    import datetime
    import logging

    from app.database import SessionLocal
    from app.workers.tasks import _generate_optimization_tips

    _log = logging.getLogger(__name__)
    db = SessionLocal()
    try:
        _generate_optimization_tips(db=db, window_end=datetime.date.today())
        db.commit()
        _last_success["optimization_tips"] = datetime.datetime.utcnow()
    except Exception:
        _log.exception("optimization_tips job failed")
        db.rollback()
    finally:
        db.close()


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
    if get_db_health_check_enabled():
        _scheduler.add_job(
            _job_db_health_check, "interval", seconds=get_db_health_check_interval_seconds(),
            id="db_health_check", replace_existing=True,
        )
    _scheduler.add_job(
        _job_optimization_tips, "interval", hours=24,
        id="optimization_tips", replace_existing=True,
    )
    # One-shot backfill, a few seconds after boot so it never delays the port opening.
    _scheduler.add_job(
        _job_startup_backfill, "date",
        run_date=datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=5),
        id="startup_backfill", replace_existing=True,
    )
    _scheduler.start()
    return _scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
