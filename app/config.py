"""Application configuration loaded from environment variables."""
import json
import logging
import os
from decimal import Decimal

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

_log = logging.getLogger(__name__)


def get_log_level() -> str:
    return os.getenv("LOG_LEVEL", "INFO").upper()


def get_cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "")
    if not raw.strip():
        return []  # default: no cross-origin browser access; server-to-server unaffected
    if raw.strip() == "*":
        _log.warning(
            "CORS_ORIGINS=* permits all browser origins. "
            "Set explicit origins (e.g. https://governance.company.com) for production."
        )
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


def get_standard_model_deployments() -> list[dict]:
    """Return the deployment templates every new org should be auto-provisioned with.

    Format: env var ``STANDARD_MODEL_DEPLOYMENTS`` is a JSON array, e.g.::

        STANDARD_MODEL_DEPLOYMENTS=[
          {"model_name": "gpt-5-nano", "provider": "azure_openai",
           "deployment_name": "gpt-5-nano-prod", "endpoint_url": "https://...",
           "api_key": "...", "api_version": "2025-01-01-preview"},
          {"model_name": "gpt-4.1-mini", "provider": "azure_openai",
           "deployment_name": "gpt-4.1-mini-prod", "endpoint_url": "https://...",
           "api_key": "...", "api_version": "2025-01-01-preview"}
        ]

    Not configured or invalid JSON returns an empty list — org creation still
    succeeds, it just won't auto-provision any deployments.
    """
    raw = os.getenv("STANDARD_MODEL_DEPLOYMENTS", "")
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        _log.warning("STANDARD_MODEL_DEPLOYMENTS is not valid JSON — skipping auto-provisioning.")
        return []
    if not isinstance(parsed, list):
        _log.warning("STANDARD_MODEL_DEPLOYMENTS must be a JSON array — skipping auto-provisioning.")
        return []
    return parsed


def _dec(key: str, default: str) -> Decimal:
    return Decimal(os.getenv(key, default))


def _int(key: str, default: str) -> int:
    return int(os.getenv(key, default))


def get_azure_max_concurrent_requests() -> int:
    """Cap on in-flight requests to Azure OpenAI per process.

    Backpressure valve: once this many requests are already waiting on Azure,
    new ones get a fast 503 instead of piling up and exhausting the DB pool
    or httpx connection limits while waiting on a slow/saturated upstream.

    Each in-flight proxy request holds its DB session open for the whole
    Azure call (including retries/failover, which can run to several
    minutes if Azure is slow or unreachable), so this valve only protects
    the DB pool if its cap stays at or below DB_POOL_SIZE + DB_MAX_OVERFLOW
    (default 14 + 6 = 20 per worker). A higher default here previously let
    proxy traffic alone exhaust every pooled connection — including those
    needed by unrelated admin/scheduler queries in the same process —
    before this limiter ever kicked in.
    """
    return _int("AZURE_MAX_CONCURRENT_REQUESTS", "15")


def get_azure_read_timeout_seconds() -> float:
    """Per-attempt wait for an Azure/provider response body once connected.

    Previously hardcoded to 120s in three places in proxy.py. That let a
    single slow/hung attempt run for up to 120s, and — multiplied by
    AZURE_RETRY_MAX_ATTEMPTS retries — could keep a request (and its held
    DB connection, see get_db_pool_size) in flight for several minutes.
    Most reverse proxies / API gateways time out well before that, so the
    caller's own gateway would return a 502 while we were still retrying
    in the background — a slow eventual success or failure on our side
    still reads as "governance is down" to them. 60s keeps each attempt
    within common gateway timeout windows while still giving normal
    completions room to finish.
    """
    return float(os.getenv("AZURE_READ_TIMEOUT_SECONDS", "60"))


def get_azure_retry_max_attempts() -> int:
    """Total attempts (including the first) for transient Azure failures."""
    return _int("AZURE_RETRY_MAX_ATTEMPTS", "3")


def get_azure_total_deadline_seconds() -> float:
    """Hard wall-clock budget for the whole outbound call: every retry across
    every failover candidate deployment combined.

    Per-attempt timeouts (AZURE_READ_TIMEOUT_SECONDS) bound a single try, but
    retries × failover candidates can still stack up past a platform-level
    gateway timeout that this app has no control over — e.g. Azure App
    Service's front end ('Microsoft.Web') kills the connection at a fixed
    230s with no app-level override. If that fires first, our own except
    blocks never run: no clean 502 body, no audit/AiRequest failure row.
    Kept well under 230s so our own handling always wins that race.
    """
    return float(os.getenv("AZURE_TOTAL_DEADLINE_SECONDS", "90"))


def get_azure_retry_backoff_seconds() -> float:
    """Base delay for exponential backoff between Azure retry attempts."""
    return float(os.getenv("AZURE_RETRY_BACKOFF_SECONDS", "0.5"))


def get_azure_circuit_failure_threshold() -> int:
    """Consecutive Azure failures before the circuit breaker opens."""
    return _int("AZURE_CIRCUIT_FAILURE_THRESHOLD", "5")


def get_azure_circuit_reset_seconds() -> float:
    """How long the circuit stays open before allowing a probe request."""
    return float(os.getenv("AZURE_CIRCUIT_RESET_SECONDS", "30"))


def get_db_pool_size() -> int:
    """Maximum persistent DB connections per API process.

    Default sized for 2 Uvicorn workers against a 50-connection managed Postgres
    plan: 2 workers * (14 pool + 6 overflow) = 40, leaving headroom for the
    scheduler and external clients. Re-tune if worker count or plan changes.
    """
    return _int("DB_POOL_SIZE", "14")


def get_db_max_overflow() -> int:
    """Temporary DB connections above pool size; keep low for managed Postgres."""
    return _int("DB_MAX_OVERFLOW", "6")


def get_db_pool_timeout() -> int:
    """Seconds to wait for a pooled connection before failing fast."""
    return _int("DB_POOL_TIMEOUT", "10")


def get_db_pool_recycle() -> int:
    """Recycle pooled DB connections before server-side idle timeouts."""
    return _int("DB_POOL_RECYCLE", "1800")


def get_scheduler_max_workers() -> int:
    """Maximum APScheduler worker threads inside each API process."""
    return _int("SCHEDULER_MAX_WORKERS", "2")


def get_redis_url() -> str:
    """Connection string for the rate-limit counter cache (Azure Cache for Redis).

    Stores only temporary rate-limit counters to reduce Postgres load and
    improve performance — no sensitive application data. Empty string means
    Redis is not configured; rate limiting falls back to Postgres counting.
    """
    return os.getenv("REDIS_URL", "")


# ── Azure OpenAI credentials (admin-only — never exposed to external teams) ─
def get_azure_openai_api_key() -> str:
    return os.getenv("AZURE_OPENAI_API_KEY", "")


def get_azure_openai_endpoint() -> str:
    return os.getenv("AZURE_OPENAI_ENDPOINT", "")


def get_azure_openai_deployment() -> str:
    return os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "")


def get_azure_openai_api_version() -> str:
    return (
        os.getenv("AZURE_OPENAI_API_VERSION")
        or os.getenv("OPENAI_API_VERSION")
        or "2024-02-01"
    )


# ── Cost engine rates ──────────────────────────────────────────────────────
def get_cost_default_rate_per_1k() -> Decimal:
    return _dec("COST_DEFAULT_RATE_PER_1K", "0.0025")


def get_cost_default_per_second_rate() -> Decimal:
    return _dec("COST_DEFAULT_PER_SECOND_RATE", "0.0001")


def get_cost_infra_rate_per_ms() -> Decimal:
    return _dec("COST_INFRA_RATE_PER_MS", "0.00008")


def get_cost_infra_rate_per_mb() -> Decimal:
    return _dec("COST_INFRA_RATE_PER_MB", "0.00001")


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


# ── Licensed, expiring downloadable package ────────────────────────────────
# Opt-in: only packaged/licensed deployments (e.g. a consulting arm's
# per-client install) set LICENSE_ENFORCEMENT_ENABLED=true. The main
# platform deployment leaves this off and none of the below applies.
def get_license_enforcement_enabled() -> bool:
    return os.getenv("LICENSE_ENFORCEMENT_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def get_license_file_path() -> str:
    """Path to the signed license token (JWT) installed alongside the package."""
    return os.getenv("LICENSE_FILE_PATH", "license.lic")


def get_license_public_key_path() -> str:
    """Path to the RSA public key (PEM) baked into the package image, used to
    verify the license signature offline — the matching private key never
    ships and stays with whoever issues licenses.
    """
    return os.getenv("LICENSE_PUBLIC_KEY_PATH", "license_public_key.pem")


def get_license_public_key_extra_paths() -> list[str]:
    """Additional public keys to accept, comma-separated — used during a
    signing-key rotation so licenses already issued under the old key keep
    verifying while new ones are issued under the new key. Empty normally;
    see LICENSING_PACKAGING.md's key rotation section.
    """
    raw = os.getenv("LICENSE_PUBLIC_KEY_EXTRA_PATHS", "")
    return [p.strip() for p in raw.split(",") if p.strip()]


def get_license_check_interval_seconds() -> int:
    """How often the background job re-reads and re-verifies the license file."""
    return _int("LICENSE_CHECK_INTERVAL_SECONDS", "3600")


def get_license_renewal_warning_days() -> int:
    """Days before expiry at which the admin dashboard shows a renewal banner."""
    return _int("LICENSE_RENEWAL_WARNING_DAYS", "15")


def get_license_denylist_path() -> str:
    """Path to a newline-separated list of revoked license_ids, if any.

    Missing file is normal (nothing revoked) — only used to cut a client off
    before their license's natural expiry. See scripts/license_revoke.py.
    """
    return os.getenv("LICENSE_DENYLIST_PATH", "license_denylist.txt")


# ── Self-hosted DB health check (opt-in) ────────────────────────────────────
# For a client's own self-hosted deployment (see OPERATIONS.md) — checks
# that the database is reachable and hasn't grown past a sanity threshold,
# and alerts via app.services.notification_service using that deployment's
# own SMTP/Teams credentials. Does NOT check host disk space or backup
# freshness — those aren't visible from inside the app container; see
# OPERATIONS.md for what's still manual.
def get_db_health_check_enabled() -> bool:
    return os.getenv("DB_HEALTH_CHECK_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


def get_db_health_check_interval_seconds() -> int:
    return _int("DB_HEALTH_CHECK_INTERVAL_SECONDS", "3600")


def get_db_size_warning_gb() -> float:
    """Logical database size (pg_database_size) above which to warn — a rough
    proxy for "might be approaching disk limits soon", not a direct disk
    check."""
    return float(os.getenv("DB_SIZE_WARNING_GB", "20"))


# ── Optimization tips thresholds ───────────────────────────────────────────
def get_tip_window_days() -> int:
    """Lookback window the optimization-tips job evaluates each run."""
    return _int("TIP_WINDOW_DAYS", "30")


def get_tip_output_input_ratio() -> Decimal:
    """Rule 1 (response_length): completion/prompt token ratio above which a tip fires."""
    return _dec("TIP_OUTPUT_INPUT_RATIO", "3.0")


def get_tip_min_requests() -> int:
    """Minimum request volume required before a rule is allowed to fire."""
    return _int("TIP_MIN_REQUESTS", "50")


def get_tip_min_monthly_savings() -> Decimal:
    """Minimum projected monthly saving (USD) required for a cost-based tip to fire."""
    return _dec("TIP_MIN_MONTHLY_SAVINGS", "5")


def get_tip_prompt_outlier_ratio() -> Decimal:
    """Rule 3 (oversized_prompt): p95/median input-token ratio above which a tip fires."""
    return _dec("TIP_PROMPT_OUTLIER_RATIO", "3.0")


def get_tip_truncation_rate() -> Decimal:
    """Rule 4 (response_truncated): finish_reason='length' ratio above which a tip fires."""
    return _dec("TIP_TRUNCATION_RATE", "0.15")


def get_tip_duplicate_min_hits() -> int:
    """Rule 5 (cache_opportunity): minimum repeat count for a duplicate prompt hash to fire."""
    return _int("TIP_DUPLICATE_MIN_HITS", "5")


def get_tip_cooldown_days() -> int:
    """Days a dismissed tip is suppressed from reappearing."""
    return _int("TIP_COOLDOWN_DAYS", "14")
