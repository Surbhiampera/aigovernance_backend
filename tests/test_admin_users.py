"""Admin user management: /admin/users and /lookups/user-roles.

Reuses the sign-in fixtures from test_auth.py (JWT secret, in-memory rate
limits, captured emails). Users are created inside the rolled-back test
transaction.
"""
import re
import uuid
from urllib.parse import unquote

import pytest

from app.models import AuditLog, User
from tests.test_auth import (  # noqa: F401  (auth_env is an autouse fixture)
    NEW_PASSWORD,
    _email,
    _login,
    auth_env,
    make_user,
    sent_emails,
)


@pytest.fixture(autouse=True)
def roles_env(monkeypatch):
    monkeypatch.setenv("LOOKUP_USER_ROLES", "viewer,security_reviewer,admin")


@pytest.fixture
def admin(client, db_session):
    user = make_user(db_session, role="admin", name="Ada Admin")
    assert _login(client, user.email).status_code == 200
    return user


def _token(body):
    match = re.search(r"#token=(\S+)", body)
    assert match, "message should contain a link"
    return unquote(match.group(1))


def _create(client, email=None, role="viewer", name="New Person"):
    return client.post("/admin/users", json={"name": name, "email": email or _email(), "role": role})


def _audit_actions(db_session, user_id):
    return [a.audit_action for a in db_session.query(AuditLog).filter(AuditLog.entity_id == user_id)]


# ─────────────────── access control ───────────────────

def test_non_admin_gets_403_everywhere(client, db_session):
    target = make_user(db_session)
    viewer = make_user(db_session, role="viewer")
    assert _login(client, viewer.email).status_code == 200

    calls = [
        client.get("/admin/users"),
        _create(client),
        client.patch(f"/admin/users/{target.id}", json={"role": "admin"}),
        client.post(f"/admin/users/{target.id}/resend-invite"),
        client.delete(f"/admin/users/{target.id}"),
    ]
    assert [r.status_code for r in calls] == [403] * 5
    assert all(r.headers["cache-control"] == "no-store" for r in calls)
    assert db_session.get(User, target.id).role == "viewer"


def test_signed_out_gets_401(client):
    assert client.get("/admin/users").status_code == 401


# ─────────────────── create + invite ───────────────────

def test_create_user_sends_single_use_invite(client, db_session, admin, sent_emails):
    email = _email()
    res = _create(client, email=email.upper(), role="security_reviewer")
    assert res.status_code == 201
    body = res.json()
    assert body["invite_sent"] is True
    assert "invite_link" not in body
    assert body["user"]["email"] == email
    assert body["user"]["role"] == "security_reviewer"
    assert body["user"]["status"] == "invited"
    assert "password_hash" not in body["user"]
    assert "user_created" in _audit_actions(db_session, body["user"]["id"])

    assert sent_emails[-1]["to"] == email
    assert "/set-password#token=" in sent_emails[-1]["body"]
    token = _token(sent_emails[-1]["body"])

    # Can't sign in before setting a password.
    client.cookies.clear()
    assert _login(client, email).status_code == 401

    ok = client.post("/auth/reset-password", json={"token": token, "password": NEW_PASSWORD})
    assert ok.status_code == 200
    reused = client.post("/auth/reset-password", json={"token": token, "password": "An0ther-Str0ng-One!"})
    assert reused.status_code == 400

    assert _login(client, email, NEW_PASSWORD).status_code == 200
    assert client.get("/auth/me").json()["role"] == "security_reviewer"


def test_invite_link_returned_only_when_email_fails(client, admin, monkeypatch):
    from app.routers import auth as auth_router

    monkeypatch.setattr(auth_router.notification_service, "send_email_to", lambda *a: False)
    body = _create(client).json()
    assert body["invite_sent"] is False
    assert "/set-password#token=" in body["invite_link"]


def test_duplicate_email_409(client, db_session, admin, sent_emails):
    existing = make_user(db_session)
    assert _create(client, email=existing.email.upper()).status_code == 409


def test_rejects_unknown_role_and_bad_email(client, admin, sent_emails):
    assert _create(client, role="superuser").status_code == 422
    assert _create(client, email="not-an-email").status_code == 422


def test_list_users_shows_status_and_no_hashes(client, db_session, admin, sent_emails):
    invited = _create(client).json()["user"]
    rows = {u["id"]: u for u in client.get("/admin/users").json()}
    assert rows[admin.id]["status"] == "active"
    assert rows[invited["id"]]["status"] == "invited"
    assert all("password_hash" not in u for u in rows.values())


def test_resend_invite_vs_reset(client, db_session, admin, sent_emails):
    invited = make_user(db_session, password=None)
    active = make_user(db_session)

    res = client.post(f"/admin/users/{invited.id}/resend-invite")
    assert res.status_code == 200 and res.json() == {"invite_sent": True}
    assert "/set-password#token=" in sent_emails[-1]["body"]

    res = client.post(f"/admin/users/{active.id}/resend-invite")
    assert res.status_code == 200 and res.json() == {"invite_sent": True}
    assert "/reset-password#token=" in sent_emails[-1]["body"]
    assert "user_invite_resent" in _audit_actions(db_session, active.id)


# ─────────────────── roles ───────────────────

def test_role_change_applies_immediately(client, db_session, admin):
    other = make_user(db_session, role="admin")
    other_session = _login(client, other.email).cookies["aigov_session"]
    assert _login(client, admin.email).status_code == 200

    res = client.patch(f"/admin/users/{other.id}", json={"role": "viewer", "name": "  Renamed   Person "})
    assert res.status_code == 200
    assert res.json()["role"] == "viewer"
    assert res.json()["name"] == "Renamed Person"
    assert "user_role_changed" in _audit_actions(db_session, other.id)

    headers = {"Authorization": f"Bearer {other_session}"}
    client.cookies.clear()
    assert client.get("/auth/me", headers=headers).json()["role"] == "viewer"
    assert client.get("/admin/users", headers=headers).status_code == 403


def test_admin_cannot_change_own_role(client, admin):
    assert client.patch(f"/admin/users/{admin.id}", json={"role": "viewer"}).status_code == 403
    # Renaming yourself is fine.
    assert client.patch(f"/admin/users/{admin.id}", json={"name": "Ada L."}).status_code == 200


def test_last_admin_cannot_be_demoted_or_removed(client, db_session, admin):
    """Two admins acting on each other at once: the caller passed the admin
    check, but by the time the action runs they've been demoted, so the
    target is the last admin left."""
    from types import SimpleNamespace

    from app.core.auth import require_admin
    from app.main import app

    target = make_user(db_session, role="admin")
    db_session.query(User).filter(User.role == "admin", User.id != target.id).update({"role": "viewer"})
    stale = SimpleNamespace(id=admin.id, email=admin.email, org_id=None, role="admin")
    app.dependency_overrides[require_admin] = lambda: stale

    assert client.patch(f"/admin/users/{target.id}", json={"role": "viewer"}).status_code == 409
    assert client.delete(f"/admin/users/{target.id}").status_code == 409
    assert db_session.get(User, target.id).role == "admin"


def test_lookup_user_roles(client):
    roles = client.get("/lookups/user-roles").json()
    assert roles[:3] == ["viewer", "security_reviewer", "admin"]


# ─────────────────── removal ───────────────────

def test_removed_user_session_ends_and_cannot_sign_in(client, db_session, admin):
    victim = make_user(db_session)
    victim_session = _login(client, victim.email).cookies["aigov_session"]
    assert _login(client, admin.email).status_code == 200

    assert client.delete(f"/admin/users/{victim.id}").status_code == 204
    assert db_session.get(User, victim.id) is None
    assert "user_removed" in _audit_actions(db_session, victim.id)

    client.cookies.clear()
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {victim_session}"}).status_code == 401
    assert _login(client, victim.email).status_code == 401


def test_admin_cannot_remove_self(client, admin):
    assert client.delete(f"/admin/users/{admin.id}").status_code == 403


def test_missing_user_404(client, admin):
    assert client.delete(f"/admin/users/{uuid.uuid4()}").status_code == 404
