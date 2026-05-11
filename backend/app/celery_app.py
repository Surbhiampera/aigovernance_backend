import os

from celery import Celery
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "ai_governance",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "daily-aggregation": {
            "task": "app.workers.tasks.run_daily_aggregation",
            "schedule": 3600.0,  # every hour
        },
        "monthly-aggregation": {
            "task": "app.workers.tasks.run_monthly_aggregation",
            "schedule": 86400.0,  # every 24 hours
        },
        "anomaly-detection": {
            "task": "app.workers.tasks.run_anomaly_detection",
            "schedule": 1800.0,  # every 30 minutes
        },
        "alert-scan": {
            "task": "app.workers.tasks.run_alert_scan",
            "schedule": 1800.0,  # every 30 minutes
        },
        "connector-poll": {
            "task": "app.workers.tasks.run_connector_poll",
            "schedule": 900.0,  # every 15 minutes
        },
    },
)
