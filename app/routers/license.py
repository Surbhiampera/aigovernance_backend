"""License status + renewal for packaged/licensed deployments (proposal §2.5).

Two things live here:
  - GET  /license/status  — always reachable, even when the license has
    lapsed, so the admin UI can render the renewal banner / frozen state.
  - POST /license/upload  — lets an admin install a renewed license file
    without shell access to the box the package runs on.

`enforce_license` is the dependency other routers are gated behind (wired up
in app/main.py) — it never applies to the proxy router.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File

from app.config import get_license_enforcement_enabled, get_license_file_path
from app.core.deps import require_role
from app.services import license_service

router = APIRouter(prefix="/license", tags=["license"])


def enforce_license() -> None:
    """FastAPI dependency: 402 once a licensed deployment's license has lapsed.

    No-op unless LICENSE_ENFORCEMENT_ENABLED=true — the shared platform
    deployment never hits this. Missing/invalid/expired all read the same
    to callers (analytics frozen); /license/status carries the detail.
    """
    if not get_license_enforcement_enabled():
        return
    status = license_service.get_cached_license_status()
    if status.analytics_frozen:
        if status.revoked:
            error, message = "license_revoked", (
                "This license has been revoked. AI traffic is unaffected, but the "
                "admin dashboard is frozen until a new license is installed."
            )
        elif status.expired:
            error, message = "license_expired", (
                "This license has expired. AI traffic is unaffected, but the "
                "admin dashboard is frozen until it's renewed."
            )
        else:
            error, message = "license_invalid", (
                "No valid license is installed. The admin dashboard is frozen "
                "until a valid license is installed."
            )
        raise HTTPException(status_code=402, detail={"error": error, "message": message})


def _status_payload() -> dict:
    s = license_service.get_cached_license_status()
    return {
        "enforcement_enabled": get_license_enforcement_enabled(),
        "configured": s.configured,
        "valid": s.valid,
        "expired": s.expired,
        "revoked": s.revoked,
        "analytics_frozen": s.analytics_frozen if get_license_enforcement_enabled() else False,
        "license_id": s.license_id,
        "customer": s.customer,
        "features": s.features,
        "issued_at": s.issued_at.isoformat() if s.issued_at else None,
        "expires_at": s.expires_at.isoformat() if s.expires_at else None,
        "days_until_expiry": s.days_until_expiry,
        "show_renewal_banner": s.show_renewal_banner,
        "error": s.error,
    }


@router.get("/status")
async def license_status():
    """Never gated by enforce_license — the UI needs this to explain a freeze."""
    return _status_payload()


@router.post("/upload")
async def upload_license(
    file: UploadFile = File(...),
    _key=Depends(require_role("admin")),
):
    """Install a renewed license file and re-verify immediately."""
    contents = (await file.read()).decode("utf-8").strip()
    with open(get_license_file_path(), "w") as f:
        f.write(contents)
    status = license_service.refresh_license_status()
    if not status.valid:
        raise HTTPException(status_code=400, detail=f"Uploaded file is not a valid license: {status.error}")
    if status.revoked:
        raise HTTPException(status_code=400, detail=f"Uploaded license '{status.license_id}' is on the revocation list")
    return _status_payload()
