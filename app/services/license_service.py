"""Licensed, expiring downloadable package (see GOVERNANCE_EXPANSION_PROPOSAL.docx §2.5).

Verifies a signed JWT license file against a baked-in RSA public key —
entirely offline, no dependency on external infrastructure. The matching
private key is never shipped; only whoever issues licenses (scripts/license_issue.py)
holds it.

Only relevant when LICENSE_ENFORCEMENT_ENABLED=true (packaged/per-client
deployments). Expiry never blocks the AI proxy path — only the admin/analytics
API is gated, so a lapsed license degrades the dashboard, not live traffic.
"""
from __future__ import annotations

import dataclasses
import datetime
import logging
import os
import threading

import jwt

from app.config import (
    get_license_check_interval_seconds,
    get_license_denylist_path,
    get_license_file_path,
    get_license_public_key_extra_paths,
    get_license_public_key_path,
    get_license_renewal_warning_days,
)

_log = logging.getLogger(__name__)

ALGORITHM = "RS256"


@dataclasses.dataclass
class LicenseStatus:
    configured: bool = False  # a license file + public key were found and parsed
    valid: bool = False       # signature verified (may still be expired)
    expired: bool = False
    revoked: bool = False     # license_id appears on the local denylist — see scripts/license_revoke.py
    license_id: str | None = None
    customer: str | None = None
    features: list[str] = dataclasses.field(default_factory=list)
    issued_at: datetime.datetime | None = None
    expires_at: datetime.datetime | None = None
    days_until_expiry: int | None = None
    show_renewal_banner: bool = False
    error: str | None = None
    checked_at: datetime.datetime = dataclasses.field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )

    @property
    def analytics_frozen(self) -> bool:
        """True once the dashboard should stop serving — no valid, unexpired,
        unrevoked license."""
        return not (self.configured and self.valid and not self.expired and not self.revoked)


_lock = threading.Lock()
_cached_status = LicenseStatus(error="not checked yet")
_cache_populated = False
_cached_mtime: float | None = None  # mtime of the license file as of the last refresh
_cached_denylist_mtime: float | None = None  # mtime of the denylist file as of the last refresh


def _read_license_token(path: str) -> str:
    with open(path, "r") as f:
        return f.read().strip()


def _read_public_key(path: str) -> str:
    with open(path, "r") as f:
        return f.read()


def _public_key_paths() -> list[str]:
    """Primary key first, then any extras configured for a key rotation in
    progress (LICENSE_PUBLIC_KEY_EXTRA_PATHS) — see LICENSING_PACKAGING.md."""
    return [get_license_public_key_path()] + get_license_public_key_extra_paths()


def _read_public_keys(paths: list[str]) -> list[str]:
    keys = []
    for path in paths:
        try:
            keys.append(_read_public_key(path))
        except OSError:
            continue
    if not keys:
        raise FileNotFoundError(2, "No such file or directory", paths[0])
    return keys


def verify_license_any_key(token: str, public_key_pems: list[str]) -> LicenseStatus:
    """Try each configured public key in turn — the one that verifies wins.
    Supports rotating the signing key without invalidating licenses already
    issued under the old one: keep both keys configured during the rotation
    window, reissue active licenses under the new key, then drop the old one.
    """
    status = LicenseStatus(configured=True, valid=False, error="no public key available")
    for pem in public_key_pems:
        status = verify_license(token, pem)
        if status.valid:
            return status
    return status


def _license_file_mtime() -> float | None:
    try:
        return os.stat(get_license_file_path()).st_mtime
    except OSError:
        return None


def _denylist_mtime() -> float | None:
    try:
        return os.stat(get_license_denylist_path()).st_mtime
    except OSError:
        return None


def _read_denylist(path: str) -> set[str]:
    """Revoked license_ids, one per line ('#'-comments and trailing notes ok).

    A missing file means nothing has been revoked — normal for almost every
    deployment — not an error.
    """
    try:
        with open(path, "r") as f:
            lines = f.read().splitlines()
    except OSError:
        return set()
    return {
        stripped.split()[0]
        for line in lines
        if (stripped := line.strip()) and not stripped.startswith("#")
    }


def verify_license(token: str, public_key_pem: str) -> LicenseStatus:
    """Verify a license JWT's signature and expiry against *public_key_pem*.

    Signature failures and expiry are distinguished so an admin dashboard can
    show "expired, please renew" instead of "invalid license file" — the two
    have different remedies.
    """
    try:
        claims = jwt.decode(
            token, public_key_pem, algorithms=[ALGORITHM],
            options={"verify_exp": False},  # check expiry ourselves for a clean expired-vs-invalid split
        )
    except jwt.InvalidTokenError as exc:
        return LicenseStatus(configured=True, valid=False, error=f"invalid license signature: {exc}")

    expires_at = datetime.datetime.fromtimestamp(claims["exp"], tz=datetime.timezone.utc)
    issued_at = (
        datetime.datetime.fromtimestamp(claims["iat"], tz=datetime.timezone.utc)
        if "iat" in claims else None
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    days_left = (expires_at - now).days
    expired = now >= expires_at

    return LicenseStatus(
        configured=True,
        valid=True,
        expired=expired,
        license_id=claims.get("license_id"),
        customer=claims.get("customer"),
        features=claims.get("features", []),
        issued_at=issued_at,
        expires_at=expires_at,
        days_until_expiry=days_left,
        show_renewal_banner=(not expired) and days_left <= get_license_renewal_warning_days(),
    )


def refresh_license_status() -> LicenseStatus:
    """Re-read the license file + public key from disk and update the cache.

    Called at startup and periodically by the scheduler. Any I/O or parse
    failure yields a "not configured" status rather than raising — a missing
    license file freezes the dashboard (via analytics_frozen) but must never
    crash the process or touch the proxy path.
    """
    global _cached_status, _cached_mtime, _cached_denylist_mtime, _cache_populated
    try:
        token = _read_license_token(get_license_file_path())
        public_key_pems = _read_public_keys(_public_key_paths())
        status = verify_license_any_key(token, public_key_pems)
    except FileNotFoundError as exc:
        status = LicenseStatus(configured=False, error=f"license not installed: {exc.filename}")
    except OSError as exc:
        status = LicenseStatus(configured=False, error=f"could not read license: {exc}")

    if status.valid and status.license_id in _read_denylist(get_license_denylist_path()):
        status = dataclasses.replace(status, revoked=True, error="license revoked")

    with _lock:
        _cached_status = status
        _cached_mtime = _license_file_mtime()
        _cached_denylist_mtime = _denylist_mtime()
        _cache_populated = True

    if not status.valid:
        _log.warning("License check: %s", status.error)
    elif status.revoked:
        _log.warning("License revoked for customer=%s (license_id=%s)", status.customer, status.license_id)
    elif status.expired:
        _log.warning("License expired %s ago for customer=%s", now_minus(status.expires_at), status.customer)
    elif status.show_renewal_banner:
        _log.info("License for customer=%s expires in %s day(s)", status.customer, status.days_until_expiry)

    return status


def now_minus(expires_at: datetime.datetime | None) -> str:
    if expires_at is None:
        return "unknown"
    delta = datetime.datetime.now(datetime.timezone.utc) - expires_at
    return f"{delta.days}d"


def get_cached_license_status() -> LicenseStatus:
    """Return the cached status, transparently refreshing first if the license
    file or the denylist on disk has changed since it was last read.

    Each uvicorn worker holds its own in-memory cache — with no cross-worker
    signal when one worker handles a POST /license/upload (or a revocation is
    staged in), the others would otherwise keep serving a stale status until
    their own hourly scheduler tick. Comparing mtimes is a cheap stat() on
    every call in the common case, and only triggers a full re-verify when
    something actually changed, so a renewal or revocation is visible to
    every worker on its next request rather than up to an hour later.
    """
    with _lock:
        populated, known_mtime, known_denylist_mtime = _cache_populated, _cached_mtime, _cached_denylist_mtime
    if (
        populated
        and _license_file_mtime() == known_mtime
        and _denylist_mtime() == known_denylist_mtime
    ):
        with _lock:
            return _cached_status
    return refresh_license_status()
