"""Scheduler jobs that notify (email/Teams) on license and DB health events —
see OPERATIONS.md and app/scheduler.py. These test the wiring (does the
right severity/message go out under the right condition, and does the
once-a-day dedup actually suppress repeats) — not the SMTP/Teams delivery
mechanics themselves, which live in notification_service.py.
"""
import datetime

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import scheduler
from app.services import license_service


@pytest.fixture
def notify_spy(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.services.notification_service.notification_service.notify",
        lambda *a, **k: calls.append((a, k)),
    )
    # Each test starts with a clean dedup state — otherwise test order could
    # make an earlier test's "already notified today" suppress this one.
    monkeypatch.setattr(scheduler, "_last_notified_date", {
        "license_renewal": None, "license_expired": None,
        "db_unreachable": None, "db_size_warning": None,
    })
    return calls


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


def _issue(private_pem: str, *, days: int, license_id="acme-1") -> str:
    now = datetime.datetime.now(datetime.timezone.utc)
    claims = {
        "license_id": license_id,
        "customer": "Acme",
        "features": [],
        "iat": int(now.timestamp()),
        "exp": int((now + datetime.timedelta(days=days)).timestamp()),
    }
    return jwt.encode(claims, private_pem, algorithm="RS256")


def test_license_check_notifies_high_within_renewal_window(monkeypatch, tmp_path, keypair, notify_spy):
    private_pem, public_pem = keypair
    license_path = tmp_path / "license.lic"
    public_key_path = tmp_path / "public.pem"
    license_path.write_text(_issue(private_pem, days=5))
    public_key_path.write_text(public_pem)

    monkeypatch.setattr(license_service, "get_license_file_path", lambda: str(license_path))
    monkeypatch.setattr(license_service, "get_license_public_key_path", lambda: str(public_key_path))
    monkeypatch.setattr(license_service, "get_license_renewal_warning_days", lambda: 15)

    scheduler._job_license_check()

    assert len(notify_spy) == 1
    args, _ = notify_spy[0]
    assert args[0] == "license_renewal"
    assert args[1] == "high"
    assert "acme-1" in args[2]


def test_license_check_notifies_critical_when_expired(monkeypatch, tmp_path, keypair, notify_spy):
    private_pem, public_pem = keypair
    license_path = tmp_path / "license.lic"
    public_key_path = tmp_path / "public.pem"
    license_path.write_text(_issue(private_pem, days=-1))
    public_key_path.write_text(public_pem)

    monkeypatch.setattr(license_service, "get_license_file_path", lambda: str(license_path))
    monkeypatch.setattr(license_service, "get_license_public_key_path", lambda: str(public_key_path))

    scheduler._job_license_check()

    assert len(notify_spy) == 1
    args, _ = notify_spy[0]
    assert args[0] == "license_expired"
    assert args[1] == "critical"
    assert "expired" in args[2]


def test_license_check_dedups_within_the_same_day(monkeypatch, tmp_path, keypair, notify_spy):
    private_pem, public_pem = keypair
    license_path = tmp_path / "license.lic"
    public_key_path = tmp_path / "public.pem"
    license_path.write_text(_issue(private_pem, days=-1))
    public_key_path.write_text(public_pem)

    monkeypatch.setattr(license_service, "get_license_file_path", lambda: str(license_path))
    monkeypatch.setattr(license_service, "get_license_public_key_path", lambda: str(public_key_path))

    scheduler._job_license_check()
    scheduler._job_license_check()
    scheduler._job_license_check()

    assert len(notify_spy) == 1, "three ticks on the same day must produce exactly one notification, not three"


def test_license_check_no_notification_when_valid_and_not_near_expiry(monkeypatch, tmp_path, keypair, notify_spy):
    private_pem, public_pem = keypair
    license_path = tmp_path / "license.lic"
    public_key_path = tmp_path / "public.pem"
    license_path.write_text(_issue(private_pem, days=200))
    public_key_path.write_text(public_pem)

    monkeypatch.setattr(license_service, "get_license_file_path", lambda: str(license_path))
    monkeypatch.setattr(license_service, "get_license_public_key_path", lambda: str(public_key_path))

    scheduler._job_license_check()

    assert notify_spy == []


def test_db_health_check_notifies_critical_when_unreachable(monkeypatch, notify_spy):
    class _BoomSession:
        def execute(self, *a, **k):
            raise RuntimeError("connection refused")
        def close(self):
            pass

    monkeypatch.setattr(scheduler, "SessionLocal", lambda: _BoomSession(), raising=False)
    monkeypatch.setattr("app.database.SessionLocal", lambda: _BoomSession())

    scheduler._job_db_health_check()

    assert len(notify_spy) == 1
    args, _ = notify_spy[0]
    assert args[0] == "db_unreachable"
    assert args[1] == "critical"


def test_db_health_check_notifies_high_when_size_over_threshold(monkeypatch, notify_spy):
    class _FakeResult:
        def scalar(self):
            return 50 * (1024 ** 3)  # 50 GB

    class _FakeSession:
        def execute(self, *a, **k):
            return _FakeResult()
        def close(self):
            pass

    monkeypatch.setattr("app.database.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(scheduler, "get_db_size_warning_gb", lambda: 20)

    scheduler._job_db_health_check()

    assert len(notify_spy) == 1
    args, _ = notify_spy[0]
    assert args[0] == "db_size_warning"
    assert args[1] == "high"
    assert "50.0 GB" in args[2]


def test_db_health_check_no_notification_when_healthy_and_small(monkeypatch, notify_spy):
    class _FakeResult:
        def scalar(self):
            return 1 * (1024 ** 3)  # 1 GB

    class _FakeSession:
        def execute(self, *a, **k):
            return _FakeResult()
        def close(self):
            pass

    monkeypatch.setattr("app.database.SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(scheduler, "get_db_size_warning_gb", lambda: 20)

    scheduler._job_db_health_check()

    assert notify_spy == []
