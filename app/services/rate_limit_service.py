"""Rate limit enforcement — called by the proxy immediately after authentication.

Reads limit configuration from the rate_limits table and counts recent
requests / tokens from ai_requests and request_cost.

Scope resolution (all applicable limits are checked; any exceeded → 429):
  1. Per-key   — rate_limits row WHERE key_id = :key_id
  2. Per-project — rate_limits row WHERE project_id = :project_id AND key_id IS NULL
  3. Per-org   — rate_limits row WHERE org_id = :org_id AND project_id IS NULL AND key_id IS NULL

tool_name = '*' or NULL on a rate_limits row means "applies to all models".
A specific tool_name value applies only when that model is being called.

Migration note (DB → Redis):
  Counter logic is isolated in _count_requests_in_window() and _sum_tokens_today().
  To migrate: replace the bodies of those two functions with Redis INCR+EXPIRE calls.
  The rate_limits config table stays in Postgres; only counters move.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)

_WINDOW_SECONDS = 60


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
    """Count committed ai_requests rows within the sliding window.

    Replace this function body with Redis INCR+EXPIRE when migrating.
    """
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
    """Sum total_tokens from request_cost for today.

    Replace this function body with Redis INCR+EXPIRE when migrating.
    """
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
        scope_val = scope_val  # keep as-is for org fallback

    return (
        db.query(func.coalesce(func.sum(RequestCost.total_tokens), 0))
        .filter(col == scope_val, func.date(RequestCost.created_at) == today)
        .scalar() or 0
    )


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
    """Seconds until the oldest request in the window ages out."""
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
