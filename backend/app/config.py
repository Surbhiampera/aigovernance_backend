"""Application configuration loaded from environment variables."""
import os
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))


def get_log_level() -> str:
    return os.getenv("LOG_LEVEL", "INFO").upper()


def get_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "*")
    if raw.strip() == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def get_lookup_defaults(name: str) -> list[str]:
    """Return injected lookup defaults from env vars (no hardcoded enums in source).

    Format: env var ``LOOKUP_<NAME>`` is a comma-separated list, e.g.
    ``LOOKUP_AUTH_TYPES="API Key,OAuth,Basic Auth"``.
    Anything not configured returns an empty list — DB values still drive the
    dropdown.
    """
    env_key = f"LOOKUP_{name.upper().replace('-', '_')}"
    raw = os.getenv(env_key, "")
    return [item.strip() for item in raw.split(",") if item and item.strip()]


def _dec(key: str, default: str) -> Decimal:
    return Decimal(os.getenv(key, default))


def _int(key: str, default: str) -> int:
    return int(os.getenv(key, default))


# ── Cost engine rates ──────────────────────────────────────────────────────
def get_cost_default_rate_per_1k() -> Decimal:
    return _dec("COST_DEFAULT_RATE_PER_1K", "0.0025")


def get_cost_default_per_second_rate() -> Decimal:
    return _dec("COST_DEFAULT_PER_SECOND_RATE", "0.0001")


def get_cost_infra_rate_per_ms() -> Decimal:
    return _dec("COST_INFRA_RATE_PER_MS", "0.00008")


# ── Anomaly detection thresholds ───────────────────────────────────────────
def get_anomaly_spike_ratio() -> Decimal:
    """Ratio of observed/baseline above which a scheduled anomaly is recorded."""
    return _dec("ANOMALY_SPIKE_RATIO", "1.8")


def get_anomaly_high_severity_ratio() -> Decimal:
    """Ratio above which an anomaly is escalated to high severity."""
    return _dec("ANOMALY_HIGH_SEVERITY_RATIO", "2.5")


def get_anomaly_spike_threshold() -> Decimal:
    """Per-event real-time ratio above which a usage spike alert fires."""
    return _dec("ANOMALY_SPIKE_THRESHOLD", "1.5")


def get_anomaly_baseline_days() -> int:
    """How many prior days to use as the anomaly baseline window."""
    return _int("ANOMALY_BASELINE_DAYS", "7")


# ── Alert engine thresholds ────────────────────────────────────────────────
def get_alert_budget_default_threshold_pct() -> Decimal:
    """Fallback budget alert threshold % when the budget row has none set."""
    return _dec("ALERT_BUDGET_DEFAULT_THRESHOLD_PCT", "80")


def get_alert_budget_mid_pct() -> Decimal:
    """Additional mid-tier budget alert percentage (fires between threshold and 100%)."""
    return _dec("ALERT_BUDGET_MID_PCT", "90")


def get_alert_token_quota_warning_pct() -> Decimal:
    """Token quota % at which to fire a warning alert."""
    return _dec("ALERT_TOKEN_QUOTA_WARNING_PCT", "80")


def get_alert_dedup_days() -> int:
    """Days within which a duplicate alert is suppressed."""
    return _int("ALERT_DEDUP_DAYS", "1")


def get_alert_anomaly_batch_limit() -> int:
    """Max open anomalies converted to alerts per scheduler run."""
    return _int("ALERT_ANOMALY_BATCH_LIMIT", "20")
