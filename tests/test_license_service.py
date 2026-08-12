"""Licensed, expiring downloadable package (proposal §2.5)."""
import dataclasses
import datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.routers.license import enforce_license
from app.services import license_service


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def _issue(private_pem: str, *, days: int, customer="Acme", license_id="acme-1", features=None) -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    claims = {
        "license_id": license_id,
        "customer": customer,
        "features": features or ["analytics"],
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(days=days)).timestamp()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def test_valid_unexpired_license(keypair):
    private_pem, public_pem = keypair
    token = _issue(private_pem, days=365)

    status = license_service.verify_license(token, public_pem)

    assert status.configured is True
    assert status.valid is True
    assert status.expired is False
    assert status.customer == "Acme"
    assert status.license_id == "acme-1"
    assert status.features == ["analytics"]
    assert status.analytics_frozen is False


def test_expired_license(keypair):
    private_pem, public_pem = keypair
    token = _issue(private_pem, days=-1)

    status = license_service.verify_license(token, public_pem)

    assert status.valid is True  # signature is still good
    assert status.expired is True
    assert status.analytics_frozen is True


def test_renewal_banner_within_warning_window(keypair):
    private_pem, public_pem = keypair
    token = _issue(private_pem, days=10)

    status = license_service.verify_license(token, public_pem)

    assert status.expired is False
    assert status.show_renewal_banner is True


def test_renewal_banner_not_shown_far_from_expiry(keypair):
    private_pem, public_pem = keypair
    token = _issue(private_pem, days=200)

    status = license_service.verify_license(token, public_pem)

    assert status.show_renewal_banner is False


def test_tampered_signature_is_invalid(keypair):
    _private_pem, public_pem = keypair
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_private_pem = other_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    token_signed_by_wrong_key = _issue(other_private_pem, days=365)

    status = license_service.verify_license(token_signed_by_wrong_key, public_pem)

    assert status.valid is False
    assert status.analytics_frozen is True
    assert status.error is not None


def test_refresh_license_status_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(license_service, "get_license_file_path", lambda: str(tmp_path / "missing.lic"))
    monkeypatch.setattr(license_service, "get_license_public_key_path", lambda: str(tmp_path / "missing_pub.pem"))

    status = license_service.refresh_license_status()

    assert status.configured is False
    assert status.analytics_frozen is True
    assert license_service.get_cached_license_status() is status


def test_refresh_license_status_reads_valid_file(monkeypatch, tmp_path, keypair):
    private_pem, public_pem = keypair
    license_path = tmp_path / "license.lic"
    public_key_path = tmp_path / "public.pem"
    license_path.write_text(_issue(private_pem, days=365))
    public_key_path.write_text(public_pem)

    monkeypatch.setattr(license_service, "get_license_file_path", lambda: str(license_path))
    monkeypatch.setattr(license_service, "get_license_public_key_path", lambda: str(public_key_path))

    status = license_service.refresh_license_status()

    assert status.valid is True
    assert status.analytics_frozen is False


def _keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


def test_verify_license_any_key_tries_each_key_in_turn():
    old_private, old_public = _keypair()
    new_private, new_public = _keypair()

    token_signed_by_old_key = _issue(old_private, days=365)

    # Only the new key configured — old-key license must NOT verify.
    status = license_service.verify_license_any_key(token_signed_by_old_key, [new_public])
    assert status.valid is False

    # Rotation window: both keys configured — old-key license still verifies.
    status = license_service.verify_license_any_key(token_signed_by_old_key, [new_public, old_public])
    assert status.valid is True

    # A license freshly issued under the new key also verifies against the
    # same [new, old] list — order doesn't matter, first match wins.
    token_signed_by_new_key = _issue(new_private, days=365, license_id="new-key-license")
    status = license_service.verify_license_any_key(token_signed_by_new_key, [new_public, old_public])
    assert status.valid is True
    assert status.license_id == "new-key-license"


def test_refresh_license_status_honors_extra_public_key_paths(monkeypatch, tmp_path):
    old_private, old_public = _keypair()
    new_private, new_public = _keypair()

    license_path = tmp_path / "license.lic"
    primary_key_path = tmp_path / "new_public.pem"   # primary = the NEW key
    extra_key_path = tmp_path / "old_public.pem"     # extra = the OLD key, still valid during rotation

    # A license issued under the OLD key, before rotation — must still work.
    license_path.write_text(_issue(old_private, days=365, license_id="pre-rotation-license"))
    primary_key_path.write_text(new_public)
    extra_key_path.write_text(old_public)

    monkeypatch.setattr(license_service, "get_license_file_path", lambda: str(license_path))
    monkeypatch.setattr(license_service, "get_license_public_key_path", lambda: str(primary_key_path))
    monkeypatch.setattr(license_service, "get_license_public_key_extra_paths", lambda: [str(extra_key_path)])

    status = license_service.refresh_license_status()

    assert status.valid is True
    assert status.license_id == "pre-rotation-license"


def test_refresh_license_status_detects_revoked_license(monkeypatch, tmp_path, keypair):
    private_pem, public_pem = keypair
    license_path = tmp_path / "license.lic"
    public_key_path = tmp_path / "public.pem"
    denylist_path = tmp_path / "denylist.txt"
    license_path.write_text(_issue(private_pem, days=365, license_id="revoked-id"))
    public_key_path.write_text(public_pem)
    denylist_path.write_text("revoked-id  # revoked for testing\n")

    monkeypatch.setattr(license_service, "get_license_file_path", lambda: str(license_path))
    monkeypatch.setattr(license_service, "get_license_public_key_path", lambda: str(public_key_path))
    monkeypatch.setattr(license_service, "get_license_denylist_path", lambda: str(denylist_path))

    status = license_service.refresh_license_status()

    assert status.valid is True
    assert status.revoked is True
    assert status.analytics_frozen is True


def test_refresh_license_status_unrelated_denylist_entry_is_ignored(monkeypatch, tmp_path, keypair):
    """A denylist with OTHER clients' revoked ids must not affect this one."""
    private_pem, public_pem = keypair
    license_path = tmp_path / "license.lic"
    public_key_path = tmp_path / "public.pem"
    denylist_path = tmp_path / "denylist.txt"
    license_path.write_text(_issue(private_pem, days=365, license_id="still-good"))
    public_key_path.write_text(public_pem)
    denylist_path.write_text("someone-elses-license\nanother-one\n")

    monkeypatch.setattr(license_service, "get_license_file_path", lambda: str(license_path))
    monkeypatch.setattr(license_service, "get_license_public_key_path", lambda: str(public_key_path))
    monkeypatch.setattr(license_service, "get_license_denylist_path", lambda: str(denylist_path))

    status = license_service.refresh_license_status()

    assert status.revoked is False
    assert status.analytics_frozen is False


def test_enforce_license_raises_402_when_revoked(monkeypatch, keypair):
    from fastapi import HTTPException

    private_pem, public_pem = keypair
    base_status = license_service.verify_license(_issue(private_pem, days=365, license_id="rev-1"), public_pem)
    revoked_status = dataclasses.replace(base_status, revoked=True)

    monkeypatch.setattr("app.routers.license.get_license_enforcement_enabled", lambda: True)
    monkeypatch.setattr(license_service, "get_cached_license_status", lambda: revoked_status)

    with pytest.raises(HTTPException) as exc_info:
        enforce_license()

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["error"] == "license_revoked"


def test_revoke_endpoint_freezes_only_this_deployment(monkeypatch, tmp_path, keypair, client):
    private_pem, public_pem = keypair
    license_path = tmp_path / "license.lic"
    public_key_path = tmp_path / "public.pem"
    denylist_path = tmp_path / "denylist.txt"
    license_path.write_text(_issue(private_pem, days=365, license_id="to-revoke"))
    public_key_path.write_text(public_pem)

    monkeypatch.setattr(license_service, "get_license_file_path", lambda: str(license_path))
    monkeypatch.setattr(license_service, "get_license_public_key_path", lambda: str(public_key_path))
    monkeypatch.setattr(license_service, "get_license_denylist_path", lambda: str(denylist_path))
    monkeypatch.setattr("app.routers.license.get_license_denylist_path", lambda: str(denylist_path))
    monkeypatch.setattr("app.routers.license.get_license_enforcement_enabled", lambda: True)
    monkeypatch.setenv("GOVERNANCE_MASTER_KEY", "test-master-key")

    response = client.post(
        "/license/revoke",
        json={"license_id": "to-revoke", "reason": "test"},
        headers={"X-API-Key": "test-master-key"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["revoked"] is True
    assert data["analytics_frozen"] is True
    assert "to-revoke" in denylist_path.read_text()


def test_enforce_license_noop_when_enforcement_disabled(monkeypatch):
    monkeypatch.setattr("app.routers.license.get_license_enforcement_enabled", lambda: False)
    # Cache is left in whatever (possibly frozen) state other tests left it in —
    # must not matter while enforcement is off.
    enforce_license()  # should not raise


def test_enforce_license_raises_402_when_expired(monkeypatch, keypair):
    from fastapi import HTTPException

    private_pem, public_pem = keypair
    expired_status = license_service.verify_license(_issue(private_pem, days=-1), public_pem)

    monkeypatch.setattr("app.routers.license.get_license_enforcement_enabled", lambda: True)
    monkeypatch.setattr(license_service, "get_cached_license_status", lambda: expired_status)

    with pytest.raises(HTTPException) as exc_info:
        enforce_license()

    assert exc_info.value.status_code == 402
    assert exc_info.value.detail["error"] == "license_expired"


def test_enforce_license_passes_when_valid(monkeypatch, keypair):
    private_pem, public_pem = keypair
    valid_status = license_service.verify_license(_issue(private_pem, days=365), public_pem)

    monkeypatch.setattr("app.routers.license.get_license_enforcement_enabled", lambda: True)
    monkeypatch.setattr(license_service, "get_cached_license_status", lambda: valid_status)

    enforce_license()  # should not raise


def test_license_status_endpoint_always_reachable(client):
    response = client.get("/license/status")
    assert response.status_code == 200
    data = response.json()
    assert "enforcement_enabled" in data
    assert "analytics_frozen" in data


def test_health_endpoint_never_license_gated(client):
    """Sanity check that core/health paths aren't accidentally caught by the gate."""
    response = client.get("/health")
    assert response.status_code == 200
