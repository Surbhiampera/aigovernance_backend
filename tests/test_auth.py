"""Dashboard sign-in: register, login, lockout, /me, forgot/reset password.

Users are created inside the rolled-back test transaction (see conftest.py).
Rate-limit counters are forced onto the in-memory store so tests never touch
the real Redis.
"""
import re
import uuid
from urllib.parse import unquote

import pytest

from app.routers import auth as auth_router
from app.services import auth_rate_limit

STRONG_PASSWORD = "Tr0ub4dor&Zebra!"
NEW_PASSWORD = "C0rrect-Horse-Battery!"


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-" + "x" * 40)
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")  # TestClient talks plain HTTP
    monkeypatch.setenv("AUTH_ALLOW_REGISTRATION", "true")
    monkeypatch.setattr(auth_rate_limit, "get_redis_client", lambda: None)
    auth_rate_limit.reset_memory()
    yield
    auth_rate_limit.reset_memory()


@pytest.fixture
def sent_emails(monkeypatch):
    sent = []

    def fake_send(to, subject, body):
        sent.append({"to": to, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(auth_router.notification_service, "send_email_to", fake_send)
    return sent


def _email():
    return f"auth-test-{uuid.uuid4().hex[:10]}@example.com"


def _register(client, email, password=STRONG_PASSWORD, name="Test Person"):
    return client.post("/auth/register", json={"name": name, "email": email, "password": password})


def _reset_token(sent_emails):
    match = re.search(r"#token=(\S+)", sent_emails[-1]["body"])
    assert match, "reset email should contain a link"
    return unquote(match.group(1))


def test_register_success_and_duplicate(client):
    email = _email()
    res = _register(client, email)
    assert res.status_code == 200
    assert res.json()["user"]["email"] == email
    assert res.json()["user"]["role"] == "viewer"
    assert "aigov_session" in res.cookies
    assert res.headers["cache-control"] == "no-store"

    dup = _register(client, email.upper())
    assert dup.status_code == 409


def test_register_rejects_weak_password(client):
    res = _register(client, _email(), password="password")
    assert res.status_code == 422


def test_register_disabled(client, monkeypatch):
    monkeypatch.setenv("AUTH_ALLOW_REGISTRATION", "false")
    assert _register(client, _email()).status_code == 403


def test_login_success_and_bad_password(client):
    email = _email()
    _register(client, email)
    client.cookies.clear()

    ok = client.post("/auth/login", json={"email": email.upper(), "password": STRONG_PASSWORD})
    assert ok.status_code == 200
    assert ok.json()["user"]["email"] == email
    assert "aigov_session" in ok.cookies

    bad = client.post("/auth/login", json={"email": email, "password": "Wrong-Passw0rd!"})
    assert bad.status_code == 401
    unknown = client.post("/auth/login", json={"email": _email(), "password": "Wrong-Passw0rd!"})
    assert unknown.status_code == 401
    assert bad.json() == unknown.json()


def test_lockout_after_repeated_failures(client, monkeypatch):
    monkeypatch.setenv("AUTH_LOGIN_MAX_FAILURES", "3")
    email = _email()
    _register(client, email)
    client.cookies.clear()

    for _ in range(3):
        assert client.post("/auth/login", json={"email": email, "password": "Wrong-Passw0rd!"}).status_code == 401

    locked = client.post("/auth/login", json={"email": email, "password": STRONG_PASSWORD})
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0


def test_me_with_and_without_cookie(client):
    email = _email()
    _register(client, email)

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == email

    client.post("/auth/logout")
    client.cookies.clear()
    assert client.get("/auth/me").status_code == 401


def test_me_accepts_bearer_token(client):
    res = _register(client, _email())
    token = res.cookies["aigov_session"]
    client.cookies.clear()
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_forgot_password_same_response_for_known_and_unknown(client, sent_emails):
    email = _email()
    _register(client, email)

    known = client.post("/auth/forgot-password", json={"email": email})
    unknown = client.post("/auth/forgot-password", json={"email": _email()})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert [m["to"] for m in sent_emails] == [email]


def test_reset_works_once_and_kills_old_sessions(client, sent_emails):
    email = _email()
    old_session = _register(client, email).cookies["aigov_session"]

    client.post("/auth/forgot-password", json={"email": email})
    token = _reset_token(sent_emails)

    weak = client.post("/auth/reset-password", json={"token": token, "password": "short"})
    assert weak.status_code == 422

    ok = client.post("/auth/reset-password", json={"token": token, "password": NEW_PASSWORD})
    assert ok.status_code == 200
    assert sent_emails[-1]["subject"] == "Your AI Governance password was changed"

    reused = client.post("/auth/reset-password", json={"token": token, "password": "An0ther-Str0ng-One!"})
    assert reused.status_code == 400

    client.cookies.clear()
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {old_session}"}).status_code == 401

    assert client.post("/auth/login", json={"email": email, "password": STRONG_PASSWORD}).status_code == 401
    assert client.post("/auth/login", json={"email": email, "password": NEW_PASSWORD}).status_code == 200


def test_session_token_cannot_be_used_as_reset_token(client):
    session = _register(client, _email()).cookies["aigov_session"]
    res = client.post("/auth/reset-password", json={"token": session, "password": NEW_PASSWORD})
    assert res.status_code == 400


def test_unconfigured_secret_returns_503(client, monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", "")
    assert client.post("/auth/login", json={"email": _email(), "password": "x"}).status_code == 503
    assert client.get("/auth/me").status_code == 503


@pytest.mark.parametrize("header,expected", [
    ("203.0.113.5:54321", "203.0.113.5"),          # Azure App Service style
    ("10.0.0.1, 203.0.113.5", "203.0.113.5"),       # right-most entry wins
    ("[2001:db8::1]:443", "2001:db8::1"),
    ("2001:db8::1", "2001:db8::1"),
])
def test_client_ip_from_forwarded_header(monkeypatch, header, expected):
    from starlette.requests import Request

    monkeypatch.setenv("AUTH_TRUST_PROXY_HEADERS", "true")
    scope = {"type": "http", "headers": [(b"x-forwarded-for", header.encode())], "client": ("127.0.0.1", 1)}
    assert auth_router._client_ip(Request(scope)) == expected
