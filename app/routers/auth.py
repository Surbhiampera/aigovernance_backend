"""Authentication router — dashboard sign-in, registration and password reset.

Sessions are an httpOnly cookie holding a signed JWT (see app/core/auth.py).
Registered outside the license gate in app/main.py so users can still sign in
while a license is frozen.
"""

import logging
import uuid
from typing import Callable
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import (
    get_auth_access_token_minutes,
    get_auth_allow_registration,
    get_auth_cookie_name,
    get_auth_cookie_samesite,
    get_auth_cookie_secure,
    get_auth_default_role,
    get_auth_dev_log_reset_links,
    get_auth_forgot_max_per_hour,
    get_auth_ip_max_attempts,
    get_auth_ip_window_minutes,
    get_auth_lockout_minutes,
    get_auth_login_max_failures,
    get_auth_reset_token_minutes,
    get_auth_trust_proxy_headers,
    get_frontend_url,
)
from app.core.auth import (
    EMAIL_MAX_LENGTH,
    NAME_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    PURPOSE_ACCESS,
    PURPOSE_RESET,
    burn_password_check,
    check_password_policy,
    create_token,
    find_user_by_email,
    hash_password,
    is_valid_email,
    normalize_email,
    require_auth_configured,
    require_user,
    user_from_token,
    user_payload,
    verify_password,
)
from app.core.deps import get_db
from app.services import auth_rate_limit as limits
from app.services.notification_service import notification_service

_log = logging.getLogger(__name__)


class _NoStoreRoute(APIRoute):
    """Adds Cache-Control: no-store to every auth response, errors included."""

    def get_route_handler(self) -> Callable:
        handler = super().get_route_handler()

        async def no_store_handler(request: Request) -> Response:
            try:
                response = await handler(request)
            except HTTPException as exc:
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store"}
                raise
            response.headers["Cache-Control"] = "no-store"
            return response

        return no_store_handler


router = APIRouter(prefix="/auth", tags=["auth"], route_class=_NoStoreRoute)

_LOGIN_FAILED = "Incorrect email or password."
_FORGOT_MESSAGE = "If an account exists for that email, a password reset link has been sent."
_INVALID_RESET = "This reset link is invalid, expired or already used."


# ─────────────────── request bodies ───────────────────

class RegisterRequest(BaseModel):
    name: str = Field(..., max_length=NAME_MAX_LENGTH)
    email: str = Field(..., max_length=EMAIL_MAX_LENGTH)
    password: str = Field(..., max_length=PASSWORD_MAX_LENGTH)


class LoginRequest(BaseModel):
    email: str = Field(..., max_length=EMAIL_MAX_LENGTH)
    password: str = Field(..., max_length=PASSWORD_MAX_LENGTH)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., max_length=EMAIL_MAX_LENGTH)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., max_length=2048)
    password: str = Field(..., max_length=PASSWORD_MAX_LENGTH)


# ─────────────────── helpers ───────────────────

def _strip_port(value: str) -> str:
    """Azure App Service sends X-Forwarded-For as "ip:port" — drop the port so
    each client gets one bucket, not one per connection."""
    if value.startswith("["):  # [IPv6]:port
        return value[1:].split("]", 1)[0]
    if value.count(":") == 1:  # IPv4:port (bare IPv6 has several colons)
        return value.split(":", 1)[0]
    return value


def _client_ip(request: Request) -> str:
    if get_auth_trust_proxy_headers():
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            # The right-most entry is the one appended by our own proxy.
            return _strip_port(forwarded.split(",")[-1].strip()) or "unknown"
    return request.client.host if request.client else "unknown"


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(
        status_code=429,
        detail="Too many attempts. Please try again later.",
        headers={"Retry-After": str(retry_after)},
    )


def _limit_ip(request: Request, scope: str) -> None:
    retry = limits.hit(
        limits.ip_key(scope, _client_ip(request)),
        get_auth_ip_max_attempts(),
        get_auth_ip_window_minutes() * 60,
    )
    if retry:
        raise _too_many(retry)


def _set_session_cookie(response: Response, user) -> None:
    response.set_cookie(
        key=get_auth_cookie_name(),
        value=create_token(user, PURPOSE_ACCESS),
        max_age=get_auth_access_token_minutes() * 60,
        path="/",
        httponly=True,
        secure=get_auth_cookie_secure(),
        samesite=get_auth_cookie_samesite(),
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=get_auth_cookie_name(),
        path="/",
        httponly=True,
        secure=get_auth_cookie_secure(),
        samesite=get_auth_cookie_samesite(),
    )


def _send_reset_email(email: str, link: str) -> None:
    minutes = get_auth_reset_token_minutes()
    body = (
        "We received a request to reset the password for your AI Governance account.\n\n"
        f"Reset your password here (valid for {minutes} minutes, one use only):\n{link}\n\n"
        "If you didn't ask for this, you can ignore this email — your password won't change."
    )
    sent = notification_service.send_email_to(email, "Reset your AI Governance password", body)
    if get_auth_dev_log_reset_links():
        _log.warning("AUTH_DEV_LOG_RESET_LINKS is on — reset link for %s: %s", email, link)
    elif not sent:
        _log.warning("Password reset email could not be sent (check SMTP_* settings)")


def _send_password_changed_email(email: str) -> None:
    body = (
        "The password for your AI Governance account was just changed, and all "
        "existing sessions were signed out.\n\n"
        "If this wasn't you, reset your password immediately and contact your administrator."
    )
    notification_service.send_email_to(email, "Your AI Governance password was changed", body)


# ─────────────────── endpoints ───────────────────

@router.post("/register")
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    from app.models import User

    require_auth_configured()
    if not get_auth_allow_registration():
        raise HTTPException(status_code=403, detail="Self-registration is disabled. Ask an administrator for access.")
    _limit_ip(request, "register")

    name = " ".join(body.name.split())
    email = normalize_email(body.email)
    if not name:
        raise HTTPException(status_code=422, detail="Please enter your name.")
    if not is_valid_email(email):
        raise HTTPException(status_code=422, detail="Please enter a valid email address.")
    check_password_policy(body.password, email=email, name=name)

    if find_user_by_email(db, email):
        raise HTTPException(status_code=409, detail="An account with this email already exists.")

    user = User(
        id=str(uuid.uuid4()),
        email=email,
        name=name,
        role=get_auth_default_role(),
        password_hash=hash_password(body.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    _set_session_cookie(response, user)
    return {"user": user_payload(user)}


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    require_auth_configured()
    _limit_ip(request, "login")

    email = normalize_email(body.email)
    failures_key = limits.email_key("login_failures", email)
    failures, ttl = limits.get_count(failures_key)
    if failures >= get_auth_login_max_failures():
        raise _too_many(ttl or get_auth_lockout_minutes() * 60)

    user = find_user_by_email(db, email) if is_valid_email(email) else None
    if user and user.password_hash:
        ok = verify_password(body.password, user.password_hash)
    else:
        burn_password_check(body.password)
        ok = False

    if not ok:
        limits.increment(failures_key, get_auth_lockout_minutes() * 60)
        raise HTTPException(status_code=401, detail=_LOGIN_FAILED)

    limits.clear(failures_key)
    _set_session_cookie(response, user)
    return {"user": user_payload(user)}


@router.get("/me")
def me(user=Depends(require_user)):
    """Return the signed-in user, or 401."""
    return user_payload(user)


@router.post("/logout")
def logout(response: Response):
    _clear_session_cookie(response)
    return {"status": "logged_out"}


@router.post("/forgot-password")
def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """Always the same answer, so it can't be used to discover accounts."""
    require_auth_configured()
    _limit_ip(request, "forgot")

    email = normalize_email(body.email)
    if is_valid_email(email):
        user = find_user_by_email(db, email)
        if user and user.email:
            over_limit = limits.hit(
                limits.email_key("forgot", email), get_auth_forgot_max_per_hour(), 3600,
            )
            if not over_limit:
                token = create_token(user, PURPOSE_RESET)
                # Token in the fragment, not the query, so it never reaches
                # server access logs or Referer headers.
                link = f"{get_frontend_url()}/reset-password#token={quote(token)}"
                background_tasks.add_task(_send_reset_email, user.email, link)

    return {"message": _FORGOT_MESSAGE}


@router.post("/reset-password")
def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    require_auth_configured()
    _limit_ip(request, "reset")

    user = user_from_token(db, body.token.strip(), PURPOSE_RESET)
    if not user:
        raise HTTPException(status_code=400, detail=_INVALID_RESET)
    check_password_policy(body.password, email=user.email or "", name=user.name or "")

    # Changing the hash changes the token fingerprint: this link and every
    # existing session stop working.
    user.password_hash = hash_password(body.password)
    db.commit()

    if user.email:
        limits.clear(limits.email_key("login_failures", normalize_email(user.email)))
        background_tasks.add_task(_send_password_changed_email, user.email)
    return {"message": "Your password has been reset. Sign in with your new password."}
