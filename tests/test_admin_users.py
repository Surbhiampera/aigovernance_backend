"""Admin user management: /admin/users and /lookups/user-roles.

Reuses the sign-in fixtures from test_auth.py (JWT secret, in-memory rate
limits). Users are created inside the rolled-back test transaction.
"""
import uuid

import pytest

from app.models import AuditLog, User
from tests.test_auth import (  # noqa: F401  (auth_env is an autouse fixture)
    NEW_PASSWORD,
    STRONG_PASSWORD,
    _email,
    _login,
    auth_env,
    make_user,
)


@pytest.fixture(autouse=True)
def roles_env(monkeypatch):
    monkeypatch.setenv("LOOKUP_USER_ROLES", "viewer,security_reviewer,admin")


@pytest.fixture
def admin(client, db_session):
    user = make_user(db_session, role="admin", name="Ada Admin")
    assert _login(client, user.email).status_code == 200
    return user


def _create(client, email=None, role="viewer", name="New Person", password=NEW_PASSWORD):
    return client.post(
        "/admin/users",
        json={"name": name, "email": email or _email(), "role": role, "password": password},
    )


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
        client.put(f"/admin/users/{target.id}/password", json={"password": NEW_PASSWORD}),
        client.delete(f"/admin/users/{target.id}"),
    ]
    assert [r.status_code for r in calls] == [403] * 5
    assert all(r.headers["cache-control"] == "no-store" for r in calls)
    assert db_session.get(User, target.id).role == "viewer"


def test_signed_out_gets_401(client):
    assert client.get("/admin/users").status_code == 401


# ─────────────────── create + passwords ───────────────────

def test_create_user_with_password_then_they_sign_in(client, db_session, admin):
    email = _email()
    res = _create(client, email=email.upper(), role="security_reviewer")
    assert res.status_code == 201
    user = res.json()["user"]
    assert user["email"] == email
    assert user["role"] == "security_reviewer"
    assert user["status"] == "active"
    assert "password_hash" not in user
    assert "user_created" in _audit_actions(db_session, user["id"])

    client.cookies.clear()
    assert _login(client, email, NEW_PASSWORD).status_code == 200
    assert client.get("/auth/me").json()["role"] == "security_reviewer"


def test_create_rejects_weak_password(client, admin):
    assert _create(client, password="password").status_code == 422
    assert _create(client, name="Mallory Smith", password="Mallory-Smith-99!").status_code == 422


def test_duplicate_email_409(client, db_session, admin):
    existing = make_user(db_session)
    assert _create(client, email=existing.email.upper()).status_code == 409


def test_rejects_unknown_role_and_bad_email(client, admin):
    assert _create(client, role="superuser").status_code == 422
    assert _create(client, email="not-an-email").status_code == 422


def test_list_users_shows_status_and_no_hashes(client, db_session, admin):
    created = _create(client).json()["user"]
    legacy = make_user(db_session, password=None)
    rows = {u["id"]: u for u in client.get("/admin/users").json()}
    assert rows[admin.id]["status"] == "active"
    assert rows[created["id"]]["status"] == "active"
    assert rows[legacy.id]["status"] == "no_password"
    assert all("password_hash" not in u for u in rows.values())


def test_admin_sets_password_and_old_sessions_end(client, db_session, admin):
    user = make_user(db_session)
    old_session = _login(client, user.email).cookies["aigov_session"]
    assert _login(client, admin.email).status_code == 200

    weak = client.put(f"/admin/users/{user.id}/password", json={"password": "short"})
    assert weak.status_code == 422

    res = client.put(f"/admin/users/{user.id}/password", json={"password": NEW_PASSWORD})
    assert res.status_code == 200
    assert "user_password_set" in _audit_actions(db_session, user.id)

    client.cookies.clear()
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {old_session}"}).status_code == 401
    assert _login(client, user.email, STRONG_PASSWORD).status_code == 401
    assert _login(client, user.email, NEW_PASSWORD).status_code == 200


def test_admin_setting_own_password_stays_signed_in(client, admin):
    res = client.put(f"/admin/users/{admin.id}/password", json={"password": NEW_PASSWORD})
    assert res.status_code == 200
    assert client.get("/auth/me").status_code == 200


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
