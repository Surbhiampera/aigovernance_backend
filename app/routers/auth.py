"""Authentication router — dashboard sign-in and sign-out.

There is no self-registration and no emailed links: admins create accounts,
and set new passwords when users forget theirs, in app/routers/admin_users.py.

Sessions are an httpOnly cookie holding a signed JWT (see app/core/auth.py).
Registered outside the license gate in app/main.py so users can still sign in
while a license is frozen.
"""

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.config import (
    get_auth_access_token_minutes,
    get_auth_cookie_name,
    get_auth_cookie_samesite,
    get_auth_cookie_secure,
    get_auth_ip_max_attempts,
    get_auth_ip_window_minutes,
    get_auth_lockout_minutes,
    get_auth_login_max_failures,
    get_auth_trust_proxy_headers,
)
from app.core.auth import (
    EMAIL_MAX_LENGTH,
    PASSWORD_MAX_LENGTH,
    PURPOSE_ACCESS,
    burn_password_check,
    create_token,
    find_user_by_email,
    is_valid_email,
    normalize_email,
    require_auth_configured,
    require_user,
    user_payload,
    verify_password,
)
from app.core.deps import get_db
from app.services import auth_rate_limit as limits



class NoStoreRoute(APIRoute):
    """Adds Cache-Control: no-store to every response, errors included. Shared
    with app/routers/admin_users.py."""

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


router = APIRouter(prefix="/auth", tags=["auth"], route_class=NoStoreRoute)

_LOGIN_FAILED = "Incorrect email or password."


# ─────────────────── request bodies ───────────────────

class LoginRequest(BaseModel):
    email: str = Field(..., max_length=EMAIL_MAX_LENGTH)
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


def client_ip(request: Request) -> str:
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


def limit_ip(request: Request, scope: str) -> None:
    retry = limits.hit(
        limits.ip_key(scope, client_ip(request)),
        get_auth_ip_max_attempts(),
        get_auth_ip_window_minutes() * 60,
    )
    if retry:
        raise _too_many(retry)


def set_session_cookie(response: Response, user) -> None:
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


# ─────────────────── endpoints ───────────────────

@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    require_auth_configured()
    limit_ip(request, "login")

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
    set_session_cookie(response, user)
    return {"user": user_payload(user)}


@router.get("/me")
def me(user=Depends(require_user)):
    """Return the signed-in user, or 401."""
    return user_payload(user)


@router.post("/logout")
def logout(response: Response):
    _clear_session_cookie(response)
    return {"status": "logged_out"}
