"""Admin user management — the only way dashboard accounts are created.

Admins add a user by name, email and role; the user gets an invite link and
sets their own password (POST /auth/reset-password accepts invite tokens).
Admins never choose or see a password. When the invite email can't be sent,
the link is returned once so the admin can share it by hand.

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

from app.config import get_auth_default_role, get_auth_forgot_max_per_hour
from app.core.auth import (
    ADMIN_ROLE,
    EMAIL_MAX_LENGTH,
    NAME_MAX_LENGTH,
    PURPOSE_INVITE,
    PURPOSE_RESET,
    find_user_by_email,
    is_valid_email,
    normalize_email,
    require_admin,
)
from app.core.deps import get_db
from app.models import User
from app.routers.auth import (
    NoStoreRoute,
    client_ip,
    limit_ip,
    send_invite_email,
    send_reset_email,
    token_link,
)
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


class UpdateUserRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=NAME_MAX_LENGTH)
    role: Optional[str] = Field(None, max_length=50)


# ─────────────────── helpers ───────────────────

def _is_admin(user) -> bool:
    return (user.role or "").strip().lower() == ADMIN_ROLE


def _status(user) -> str:
    return "active" if user.password_hash else "invited"


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


def _limit_invites(email: str) -> None:
    """Cap invite/reset emails per address, like /auth/forgot-password."""
    retry = limits.hit(limits.email_key("admin_invite", normalize_email(email)), get_auth_forgot_max_per_hour(), 3600)
    if retry:
        raise HTTPException(
            status_code=429,
            detail="Too many emails sent to this user. Please try again later.",
            headers={"Retry-After": str(retry)},
        )


def _send_invite(user) -> dict:
    """Email the set-password link; return the link only if sending failed."""
    link = token_link(user, PURPOSE_INVITE)
    sent = send_invite_email(user, link)
    return {"invite_sent": True} if sent else {"invite_sent": False, "invite_link": link}


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
    _limit_invites(email)

    user = User(id=str(uuid.uuid4()), email=email, name=name, role=role, org_id=admin.org_id)
    db.add(user)
    _audit(db, request, admin, user, "user_created", f"Created user {email} with role {role}", role=role)
    db.commit()
    db.refresh(user)
    return {"user": _user_row(user), **_send_invite(user)}


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


@router.post("/{user_id}/resend-invite")
def resend_invite(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
    admin=Depends(require_admin),
) -> dict:
    """Invite link for a user who hasn't set a password yet, reset link
    otherwise. A reset link is never returned to the admin: that would let
    them take over an active account."""
    limit_ip(request, "admin_users_invite")
    user = _get_target(db, user_id)
    if not user.email:
        raise HTTPException(status_code=422, detail="This user has no email address.")
    _limit_invites(user.email)

    kind = "invite" if not user.password_hash else "reset"
    _audit(db, request, admin, user, "user_invite_resent", f"Sent {kind} link to {user.email}", link_type=kind)
    db.commit()

    if kind == "invite":
        return _send_invite(user)
    return {"invite_sent": send_reset_email(user.email, token_link(user, PURPOSE_RESET))}


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
