"""Admin user management — the only way dashboard accounts are created.

Admins add a user by name, email, role and password, and share those
credentials with the user themselves; no email is sent. Admins also set a new
password for anyone who forgets theirs, which signs that user out everywhere.

Every endpoint needs an admin session (require_admin). Removing a user deletes
the row, which ends their sessions: tokens are resolved against the database
on every request. Role changes apply on the user's next request for the same
reason.
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import get_auth_default_role
from app.core.auth import (
    ADMIN_ROLE,
    EMAIL_MAX_LENGTH,
    NAME_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    check_password_policy,
    find_user_by_email,
    hash_password,
    is_valid_email,
    normalize_email,
    require_admin,
)
from app.core.deps import get_db
from app.models import User
from app.routers.auth import NoStoreRoute, client_ip, limit_ip, set_session_cookie
from app.routers.lookups import list_user_roles
from app.services import auth_rate_limit as limits
from app.services.audit_service import log_event

router = APIRouter(prefix="/admin/users", tags=["admin"], route_class=NoStoreRoute)

# audit_logs.org_id is NOT NULL, and users (admins included) may have no org.
_AUDIT_ORG_FALLBACK = "system"


# ─────────────────── request bodies ───────────────────

class CreateUserRequest(BaseModel):
    name: str = Field(..., max_length=NAME_MAX_LENGTH)
    email: str = Field(..., max_length=EMAIL_MAX_LENGTH)
    role: Optional[str] = Field(None, max_length=50)
    password: str = Field(..., max_length=PASSWORD_MAX_LENGTH)


class SetPasswordRequest(BaseModel):
    password: str = Field(..., max_length=PASSWORD_MAX_LENGTH)


class UpdateUserRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=NAME_MAX_LENGTH)
    role: Optional[str] = Field(None, max_length=50)


# ─────────────────── helpers ───────────────────

def _is_admin(user) -> bool:
    return (user.role or "").strip().lower() == ADMIN_ROLE


def _status(user) -> str:
    # "no_password": older rows created before sign-in existed. They can't
    # log in until an admin sets a password.
    return "active" if user.password_hash else "no_password"


def _user_row(user) -> dict:
    """Never includes password_hash."""
    return {
        "id": user.id,
        "name": user.name,
        "email": user.email,
        "role": user.role,
        "status": _status(user),
        "created_at": user.created_at.isoformat() if isinstance(user.created_at, datetime) else user.created_at,
    }


def _get_target(db: Session, user_id: str):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    return user


def _clean_name(name: str) -> str:
    cleaned = " ".join((name or "").split())
    if not cleaned:
        raise HTTPException(status_code=422, detail="Please enter a name.")
    return cleaned


def _resolve_role(db: Session, role: Optional[str]) -> str:
    """Return the canonical spelling of a known role, or 422."""
    wanted = (role or "").strip() or get_auth_default_role()
    for known in list_user_roles(db=db):
        if known.lower() == wanted.lower():
            return known
    raise HTTPException(status_code=422, detail=f"Unknown role '{wanted}'.")


def _other_admins_exist(db: Session, user_id: str) -> bool:
    """Whether an admin other than *user_id* exists. Locks the admin rows so
    two admins can't demote/remove each other concurrently and leave none."""
    admin_ids = [
        row[0]
        for row in db.query(User.id)
        .filter(func.lower(func.trim(User.role)) == ADMIN_ROLE)
        .with_for_update()
        .all()
    ]
    return any(i != user_id for i in admin_ids)


def _audit(db: Session, request: Request, admin, target, action: str, summary: str, **metadata) -> None:
    log_event(
        db,
        org_id=admin.org_id or target.org_id or _AUDIT_ORG_FALLBACK,
        audit_category="user_management",
        audit_action=action,
        actor_type="user",
        actor_id=admin.id,
        actor_email=admin.email,
        actor_ip=client_ip(request),
        entity_type="user",
        entity_id=target.id,
        compliance_relevant=True,
        change_summary=summary,
        metadata={"target_email": target.email, **metadata},
        flush=False,
    )


# ─────────────────── endpoints ───────────────────

@router.get("")
def list_users(db: Session = Depends(get_db), admin=Depends(require_admin)) -> list[dict]:
    users = db.query(User).order_by(User.created_at.desc().nullslast(), User.email).all()
    return [_user_row(u) for u in users]


@router.post("", status_code=201)
def create_user(
    body: CreateUserRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
) -> dict:
    limit_ip(request, "admin_users_create")

    name = _clean_name(body.name)
    email = normalize_email(body.email)
    if not is_valid_email(email):
        raise HTTPException(status_code=422, detail="Please enter a valid email address.")
    role = _resolve_role(db, body.role)
    if find_user_by_email(db, email):
        raise HTTPException(status_code=409, detail="An account with this email already exists.")
    check_password_policy(body.password, email=email, name=name)

    user = User(
        id=str(uuid.uuid4()),
        email=email,
        name=name,
        role=role,
        org_id=admin.org_id,
        password_hash=hash_password(body.password),
    )
    db.add(user)
    _audit(db, request, admin, user, "user_created", f"Created user {email} with role {role}", role=role)
    db.commit()
    db.refresh(user)
    return {"user": _user_row(user)}


@router.patch("/{user_id}")
def update_user(
    user_id: str,
    body: UpdateUserRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
) -> dict:
    limit_ip(request, "admin_users_update")
    if body.name is None and body.role is None:
        raise HTTPException(status_code=422, detail="Nothing to update.")

    user = _get_target(db, user_id)

    if body.role is not None:
        role = _resolve_role(db, body.role)
        if role != user.role:
            if user.id == admin.id:
                raise HTTPException(status_code=403, detail="You can't change your own role.")
            if _is_admin(user) and role.lower() != ADMIN_ROLE and not _other_admins_exist(db, user.id):
                raise HTTPException(status_code=409, detail="You can't demote the last administrator.")
            _audit(
                db, request, admin, user, "user_role_changed",
                f"Changed role of {user.email} from {user.role} to {role}",
                old_role=user.role, new_role=role,
            )
            user.role = role

    if body.name is not None:
        name = _clean_name(body.name)
        if name != user.name:
            _audit(db, request, admin, user, "user_renamed", f"Renamed {user.email}", old_name=user.name, new_name=name)
            user.name = name

    db.commit()
    db.refresh(user)
    return _user_row(user)


@router.put("/{user_id}/password")
def set_password(
    user_id: str,
    body: SetPasswordRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
) -> dict:
    """Set a new password for a user, e.g. one who forgot theirs. Changing the
    hash signs them out of every existing session."""
    limit_ip(request, "admin_users_password")
    user = _get_target(db, user_id)
    check_password_policy(body.password, email=user.email or "", name=user.name or "")

    user.password_hash = hash_password(body.password)
    _audit(db, request, admin, user, "user_password_set", f"Set a new password for {user.email}")
    db.commit()
    db.refresh(user)

    if user.email:
        limits.clear(limits.email_key("login_failures", normalize_email(user.email)))
    if user.id == admin.id:
        # The admin's own session was just invalidated with the old hash.
        set_session_cookie(response, user)
    return _user_row(user)


@router.delete("/{user_id}", status_code=204)
def delete_user(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
) -> Response:
    limit_ip(request, "admin_users_delete")
    user = _get_target(db, user_id)
    if user.id == admin.id:
        raise HTTPException(status_code=403, detail="You can't remove yourself.")
    if _is_admin(user) and not _other_admins_exist(db, user.id):
        raise HTTPException(status_code=409, detail="You can't remove the last administrator.")

    _audit(db, request, admin, user, "user_removed", f"Removed user {user.email}", role=user.role)
    db.delete(user)
    db.commit()
    return Response(status_code=204)
