"""Rate limits and lockout for the /auth endpoints.

Counters live in Redis (shared across workers) and fall back to a
per-process in-memory store if Redis is unset or unreachable. Emails are
hashed before being used in keys, so the cache never holds addresses.
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from typing import Optional

import redis

from app.services.redis_client import get_redis_client

_log = logging.getLogger(__name__)

_PREFIX = "aigov:auth:"

_mem: dict[str, tuple[int, float]] = {}
_mem_lock = threading.Lock()


def email_key(scope: str, email: str) -> str:
    digest = hashlib.sha256(email.encode("utf-8")).hexdigest()[:32]
    return f"{_PREFIX}{scope}:email:{digest}"


def ip_key(scope: str, ip: str) -> str:
    return f"{_PREFIX}{scope}:ip:{ip}"


# ─────────────────── in-memory fallback ───────────────────

def _mem_get(key: str) -> tuple[int, int]:
    now = time.monotonic()
    with _mem_lock:
        count, expires = _mem.get(key, (0, 0.0))
        if expires <= now:
            _mem.pop(key, None)
            return 0, 0
        return count, math.ceil(expires - now)


def _mem_incr(key: str, window_seconds: int) -> tuple[int, int]:
    now = time.monotonic()
    with _mem_lock:
        count, expires = _mem.get(key, (0, 0.0))
        if expires <= now:
            count, expires = 0, now + window_seconds
        count += 1
        _mem[key] = (count, expires)
        if len(_mem) > 50_000:  # bound memory under a flood of distinct keys
            for k in [k for k, (_, exp) in _mem.items() if exp <= now]:
                del _mem[k]
        return count, math.ceil(expires - now)


def reset_memory() -> None:
    """Clear the in-memory store (tests)."""
    with _mem_lock:
        _mem.clear()


# ─────────────────── public API ───────────────────

def get_count(key: str) -> tuple[int, int]:
    """Return (count, seconds until the window resets)."""
    client = get_redis_client()
    if client is not None:
        try:
            pipe = client.pipeline()
            pipe.get(key)
            pipe.ttl(key)
            raw, ttl = pipe.execute()
            return int(raw or 0), max(int(ttl or 0), 0)
        except redis.RedisError as exc:
            _log.warning("Redis error in auth rate limit, using in-memory counters: %s", exc)
    return _mem_get(key)


def increment(key: str, window_seconds: int) -> tuple[int, int]:
    """Add one to a fixed-window counter; return (count, seconds until reset)."""
    client = get_redis_client()
    if client is not None:
        try:
            # SET NX EX starts the window only on the first hit (works on
            # Redis < 7, unlike EXPIRE ... NX).
            pipe = client.pipeline()
            pipe.set(key, 0, ex=window_seconds, nx=True)
            pipe.incr(key)
            pipe.ttl(key)
            _, count, ttl = pipe.execute()
            return int(count), max(int(ttl), 1)
        except redis.RedisError as exc:
            _log.warning("Redis error in auth rate limit, using in-memory counters: %s", exc)
    return _mem_incr(key, window_seconds)


def clear(key: str) -> None:
    client = get_redis_client()
    if client is not None:
        try:
            client.delete(key)
        except redis.RedisError as exc:
            _log.warning("Redis error clearing auth counter: %s", exc)
    with _mem_lock:
        _mem.pop(key, None)


def hit(key: str, limit: int, window_seconds: int) -> Optional[int]:
    """Count one attempt. Return None if within *limit*, else the seconds to
    wait before retrying."""
    count, ttl = increment(key, window_seconds)
    if count > limit:
        return max(ttl, 1)
    return None
