"""Rate limit enforcement — called by the proxy immediately after authentication.

Reads limit configuration from the rate_limits table and counts recent
requests / tokens from ai_requests and request_cost.

Scope resolution (all applicable limits are checked; any exceeded → 429):
  1. Per-key   — rate_limits row WHERE key_id = :key_id
  2. Per-project — rate_limits row WHERE project_id = :project_id AND key_id IS NULL
  3. Per-org   — rate_limits row WHERE org_id = :org_id AND project_id IS NULL AND key_id IS NULL

tool_name = '*' or NULL on a rate_limits row means "applies to all models".
A specific tool_name value applies only when that model is being called.

Counters (Redis with Postgres fallback):
  _count_requests_in_window() and _sum_tokens_today() use Redis INCR+EXPIRE
  via app.services.redis_client when REDIS_URL is configured and reachable,
  falling back to the original Postgres queries otherwise. The rate_limits
  config table always stays in Postgres; only counters move to Redis.
  record_tokens_used() must be called once per completed request (after the
  Azure response, when total_tokens is known) to keep the daily token
  counter in Redis up to date.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.services.redis_client import get_redis_client

_log = logging.getLogger(__name__)

_WINDOW_SECONDS = 60
_TOKEN_COUNTER_KEY_FMT = "ratelimit:tokens:{scope}:{scope_val}:{day}"
_REQUEST_COUNTER_KEY_FMT = "ratelimit:reqs:{scope}:{scope_val}:{bucket}"


def check_rate_limit(
    *,
    db: Session,
    org_id: str,
    project_id: Optional[str],
    key_id: str,
    model: str,
    request_id: str,
    source_ip: Optional[str],
) -> None:
    """Raise HTTP 429 if any applicable rate limit is exceeded.

    Checks per-key, per-project, and per-org limits in that order.
    All applicable limits are evaluated; the first exceeded limit blocks.
    No-ops when no RateLimit row exists for any scope.
    """
    from app.models import RateLimit

    # Load all potentially-applicable limits in one query
    candidates = (
        db.query(RateLimit)
        .filter(
            or_(
                # per-key
                RateLimit.key_id == key_id,
                # per-project (requires new project_id column — safe if NULL col missing)
                (RateLimit.project_id == project_id) if project_id else (RateLimit.id == None),
                # per-org
                RateLimit.org_id == org_id,
            )
        )
        .all()
    )

    for limit_row in candidates:
        # Skip if tool_name is set and doesn't match current model
        if limit_row.tool_name and limit_row.tool_name not in ("*", model):
            continue

        scope, scope_label = _resolve_scope(
            limit_row=limit_row, org_id=org_id, project_id=project_id, key_id=key_id,
        )

        if limit_row.max_requests_per_min:
            count = _count_requests_in_window(
                db=db, scope=scope, scope_val=scope_label,
                window_seconds=_WINDOW_SECONDS,
            )
            if count >= limit_row.max_requests_per_min:
                retry_after = _retry_after(
                    db=db, scope=scope, scope_val=scope_label,
                    window_seconds=_WINDOW_SECONDS,
                )
                _write_rate_limit_audit(
                    db=db, org_id=org_id, project_id=project_id,
                    request_id=request_id, source_ip=source_ip,
                    limit_type="max_requests_per_min",
                    scope=scope, limit=limit_row.max_requests_per_min, count=count,
                )
                db.commit()
                raise HTTPException(
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                    detail={
                        "error": "rate_limit_exceeded",
                        "scope": scope,
                        "limit_type": "max_requests_per_min",
                        "limit": limit_row.max_requests_per_min,
                        "current_count": count,
                        "window_seconds": _WINDOW_SECONDS,
                        "retry_after": retry_after,
                        "request_id": request_id,
                        "detail": (
                            f"{scope.capitalize()} rate limit of "
                            f"{limit_row.max_requests_per_min} requests/minute exceeded."
                        ),
                    },
                )

        if limit_row.max_tokens_per_day:
            tokens_used = _sum_tokens_today(db=db, scope=scope, scope_val=scope_label)
            if tokens_used >= limit_row.max_tokens_per_day:
                _write_rate_limit_audit(
                    db=db, org_id=org_id, project_id=project_id,
                    request_id=request_id, source_ip=source_ip,
                    limit_type="max_tokens_per_day",
                    scope=scope, limit=limit_row.max_tokens_per_day, count=tokens_used,
                )
                db.commit()
                raise HTTPException(
                    status_code=429,
                    headers={"Retry-After": str(_seconds_until_midnight())},
                    detail={
                        "error": "rate_limit_exceeded",
                        "scope": scope,
                        "limit_type": "max_tokens_per_day",
                        "limit": limit_row.max_tokens_per_day,
                        "tokens_used_today": tokens_used,
                        "retry_after": _seconds_until_midnight(),
                        "request_id": request_id,
                        "detail": (
                            f"{scope.capitalize()} daily token limit of "
                            f"{limit_row.max_tokens_per_day:,} tokens exhausted."
                        ),
                    },
                )


# ---------------------------------------------------------------------------
# Counter functions — isolated for Redis migration
# ---------------------------------------------------------------------------

def _count_requests_in_window(
    *, db: Session, scope: str, scope_val: str, window_seconds: int,
) -> int:
    """Count requests within the current fixed window.

    Uses Redis INCR+EXPIRE (one counter per scope per window bucket) when
    available; each call counts the in-flight request being checked. Falls
    back to a Postgres count of committed ai_requests rows when Redis is
    not configured or unreachable.
    """
    client = get_redis_client()
    if client is not None:
        try:
            bucket = int(time.time() // window_seconds)
            key = _REQUEST_COUNTER_KEY_FMT.format(scope=scope, scope_val=scope_val, bucket=bucket)
            count = client.incr(key)
            if count == 1:
                client.expire(key, window_seconds)
            return count
        except Exception as exc:
            _log.warning("Redis request counter failed, falling back to Postgres: %s", exc)

    from app.models import AiRequest

    cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
    col_map = {
        "key": AiRequest.governance_key_id,
        "project": AiRequest.project_id,
        "org": AiRequest.org_id,
    }
    col = col_map.get(scope, AiRequest.org_id)
    return (
        db.query(func.count(AiRequest.id))
        .filter(col == scope_val, AiRequest.created_at >= cutoff)
        .scalar() or 0
    )


def _sum_tokens_today(*, db: Session, scope: str, scope_val: str) -> int:
    """Return today's token usage for this scope.

    Reads the Redis daily counter (kept up to date by record_tokens_used())
    when available; falls back to summing request_cost.total_tokens from
    Postgres when Redis is not configured or unreachable.
    """
    client = get_redis_client()
    if client is not None:
        try:
            key = _TOKEN_COUNTER_KEY_FMT.format(
                scope=scope, scope_val=scope_val, day=date.today().isoformat(),
            )
            return int(client.get(key) or 0)
        except Exception as exc:
            _log.warning("Redis token counter failed, falling back to Postgres: %s", exc)

    from app.models import RequestCost

    today = date.today()
    col_map = {
        "key": None,  # request_cost has no key_id column; fall back to org
        "project": RequestCost.project_id,
        "org": RequestCost.org_id,
    }
    col = col_map.get(scope)
    if col is None:
        col = RequestCost.org_id

    return (
        db.query(func.coalesce(func.sum(RequestCost.total_tokens), 0))
        .filter(col == scope_val, func.date(RequestCost.created_at) == today)
        .scalar() or 0
    )


def record_tokens_used(*, org_id: str, project_id: Optional[str], tokens: int) -> None:
    """Increment today's Redis token counters for org and project scope.

    Call once per completed request, after total_tokens is known (i.e. after
    the Azure response), so _sum_tokens_today() reflects current usage.
    No-ops when Redis is not configured/unreachable — Postgres summation
    remains the source of truth in that case.
    """
    if tokens <= 0:
        return
    client = get_redis_client()
    if client is None:
        return

    day = date.today().isoformat()
    ttl = _seconds_until_midnight() + _WINDOW_SECONDS  # small buffer past midnight
    try:
        pipe = client.pipeline()
        org_key = _TOKEN_COUNTER_KEY_FMT.format(scope="org", scope_val=org_id, day=day)
        pipe.incrby(org_key, tokens)
        pipe.expire(org_key, ttl)
        if project_id:
            project_key = _TOKEN_COUNTER_KEY_FMT.format(scope="project", scope_val=project_id, day=day)
            pipe.incrby(project_key, tokens)
            pipe.expire(project_key, ttl)
        pipe.execute()
    except Exception as exc:
        _log.warning("Redis token counter increment failed: %s", exc)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _resolve_scope(*, limit_row, org_id: str, project_id: Optional[str], key_id: str):
    """Return (scope_name, scope_value) for the most-specific match on this row."""
    if getattr(limit_row, "key_id", None) == key_id:
        return "key", key_id
    if project_id and getattr(limit_row, "project_id", None) == project_id:
        return "project", project_id
    return "org", org_id


def _retry_after(*, db: Session, scope: str, scope_val: str, window_seconds: int) -> int:
    """Seconds until the rate limit window resets."""
    client = get_redis_client()
    if client is not None:
        try:
            return max(1, window_seconds - int(time.time() % window_seconds))
        except Exception as exc:
            _log.warning("Redis retry-after lookup failed, falling back to Postgres: %s", exc)

    from app.models import AiRequest

    cutoff = datetime.utcnow() - timedelta(seconds=window_seconds)
    col_map = {
        "key": AiRequest.governance_key_id,
        "project": AiRequest.project_id,
        "org": AiRequest.org_id,
    }
    col = col_map.get(scope, AiRequest.org_id)
    oldest = (
        db.query(func.min(AiRequest.created_at))
        .filter(col == scope_val, AiRequest.created_at >= cutoff)
        .scalar()
    )
    if oldest is None:
        return 1
    elapsed = (datetime.utcnow() - oldest).total_seconds()
    return max(1, int(window_seconds - elapsed))


def _seconds_until_midnight() -> int:
    now = datetime.utcnow()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int((midnight - now).total_seconds())


def _write_rate_limit_audit(
    *, db: Session, org_id: str, project_id: Optional[str],
    request_id: str, source_ip: Optional[str],
    limit_type: str, scope: str, limit: int, count: int,
) -> None:
    from app.services.audit_service import log_event

    log_event(
        db,
        org_id=org_id,
        project_id=project_id,
        audit_category="governance",
        audit_action="rate_limit_exceeded",
        audit_status="failure",
        actor_type="governance_engine",
        actor_ip=source_ip,
        entity_type="rate_limit",
        request_id=request_id,
        policy_triggered=True,
        compliance_relevant=False,
        requires_review=False,
        change_summary=(
            f"Rate limit exceeded ({scope}): {count}/{limit} {limit_type.replace('_', ' ')}."
        ),
        metadata={
            "scope": scope,
            "limit_type": limit_type,
            "limit": limit,
            "count": count,
            "window_seconds": _WINDOW_SECONDS if "per_min" in limit_type else 86400,
        },
        flush=False,
    )
