"""Lazy Redis client for rate-limit counters.

Purpose: store temporary rate-limit counters to reduce Postgres load and
improve performance. No sensitive application data is stored here.

If REDIS_URL is unset or the cache is unreachable, callers must fall back
to their Postgres-backed counting path — this module never raises on
connection problems, it just returns None.
"""
from __future__ import annotations

import logging
from typing import Optional

import redis

from app.config import get_redis_url

_log = logging.getLogger(__name__)

_client: Optional["redis.Redis"] = None
_client_initialized = False


def get_redis_client() -> Optional["redis.Redis"]:
    """Return a shared Redis client, or None if not configured/unreachable."""
    global _client, _client_initialized

    if _client_initialized:
        return _client

    _client_initialized = True
    url = get_redis_url()
    if not url:
        return None

    try:
        client = redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)
        client.ping()
    except redis.RedisError as exc:
        _log.warning("Redis unavailable, falling back to Postgres counters: %s", exc)
        return None

    _client = client
    return _client
