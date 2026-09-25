"""Dashboard sign-in: login, lockout, /me, forgot/reset password, and that
public registration is gone.

Users are created inside the rolled-back test transaction (see conftest.py).
Rate-limit counters are forced onto the in-memory store so tests never touch
the real Redis.
"""
import re
import uuid
from urllib.parse import unquote

import pytest

from app.core.auth import hash_password
from app.models import User
from app.routers import auth as auth_router
from app.services import auth_rate_limit

STRONG_PASSWORD = "Tr0ub4dor&Zebra!"
NEW_PASSWORD = "C0rrect-Horse-Battery!"


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    monkeypatch.setenv("AUTH_JWT_SECRET", "test-secret-" + "x" * 40)
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")  # TestClient talks plain HTTP
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


def make_user(db_session, email=None, password=STRONG_PASSWORD, role="viewer", name="Test Person"):
    """Insert a user directly (there is no sign-up). password=None → no password."""
    user = User(
        id=str(uuid.uuid4()),
        email=email or _email(),
        name=name,
        role=role,
        password_hash=hash_password(password) if password else None,
    )
    db_session.add(user)
    db_session.flush()
    return user


def _login(client, email, password=STRONG_PASSWORD):
    return client.post("/auth/login", json={"email": email, "password": password})


def _session(client, db_session, **kwargs):
    """Create a user, sign in; return (email, session token)."""
    user = make_user(db_session, **kwargs)
    res = _login(client, user.email)
    assert res.status_code == 200
    return user.email, res.cookies["aigov_session"]


def _reset_token(sent_emails):
    match = re.search(r"#token=(\S+)", sent_emails[-1]["body"])
    assert match, "reset email should contain a link"
    return unquote(match.group(1))


def test_register_endpoint_is_gone(client, db_session):
    email = _email()
    res = client.post("/auth/register", json={"name": "X Y", "email": email, "password": STRONG_PASSWORD})
    assert res.status_code in (404, 405)
    assert db_session.query(User).filter(User.email == email).count() == 0


def test_login_success_and_bad_password(client, db_session):
    email = make_user(db_session).email

    ok = client.post("/auth/login", json={"email": email.upper(), "password": STRONG_PASSWORD})
    assert ok.status_code == 200
    assert ok.json()["user"]["email"] == email
    assert "aigov_session" in ok.cookies
    assert ok.headers["cache-control"] == "no-store"

    bad = client.post("/auth/login", json={"email": email, "password": "Wrong-Passw0rd!"})
    assert bad.status_code == 401
    unknown = client.post("/auth/login", json={"email": _email(), "password": "Wrong-Passw0rd!"})
    assert unknown.status_code == 401
    assert bad.json() == unknown.json()


def test_lockout_after_repeated_failures(client, db_session, monkeypatch):
    monkeypatch.setenv("AUTH_LOGIN_MAX_FAILURES", "3")
    email = make_user(db_session).email

    for _ in range(3):
        assert client.post("/auth/login", json={"email": email, "password": "Wrong-Passw0rd!"}).status_code == 401

    locked = client.post("/auth/login", json={"email": email, "password": STRONG_PASSWORD})
    assert locked.status_code == 429
    assert int(locked.headers["retry-after"]) > 0


def test_me_with_and_without_cookie(client, db_session):
    email, _ = _session(client, db_session)

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == email

    client.post("/auth/logout")
    client.cookies.clear()
    assert client.get("/auth/me").status_code == 401


def test_me_accepts_bearer_token(client, db_session):
    _, token = _session(client, db_session)
    client.cookies.clear()
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_forgot_password_same_response_for_known_and_unknown(client, db_session, sent_emails):
    email = make_user(db_session).email

    known = client.post("/auth/forgot-password", json={"email": email})
    unknown = client.post("/auth/forgot-password", json={"email": _email()})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert [m["to"] for m in sent_emails] == [email]


def test_reset_works_once_and_kills_old_sessions(client, db_session, sent_emails):
    email, old_session = _session(client, db_session)

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


def test_session_token_cannot_be_used_as_reset_token(client, db_session):
    _, session = _session(client, db_session)
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
    assert auth_router.client_ip(Request(scope)) == expected
