"""Authentication utilities for dashboard sign-in.

Passwords: PBKDF2-SHA256 (stdlib), stored as
``pbkdf2_sha256$<iterations>$<salt_b64>$<hash_b64>``.

Tokens: HS256 JWTs carrying ``sub`` (user id), ``purpose`` (access or
password_reset), ``iat``, ``exp`` and ``pwv`` — a short fingerprint of the
user's current password hash. Changing the password changes the fingerprint,
which makes a reset link single-use and ends every existing session.
Tokens never carry the role: it is read from the database on every request,
so role changes and removals apply immediately.
"""

import base64
import hashlib
import hmac
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import (
    get_auth_access_token_minutes,
    get_auth_cookie_name,
    get_auth_jwt_secret,
    get_auth_password_min_length,
    get_auth_reset_token_minutes,
)
from app.core.deps import get_db

# Kept for existing importers.
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

PURPOSE_ACCESS = "access"
PURPOSE_RESET = "password_reset"

ADMIN_ROLE = "admin"

PASSWORD_MAX_LENGTH = 128
EMAIL_MAX_LENGTH = 254
NAME_MAX_LENGTH = 150

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600_000
_JWT_ALG = "HS256"
_MIN_SECRET_LENGTH = 32

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

# Mirrors COMMON_WORDS in the frontend's authUtils.js.
_COMMON_WORDS = (
    "password", "passw0rd", "qwerty", "letmein", "welcome", "admin", "iloveyou",
    "monkey", "dragon", "football", "baseball", "abc", "abcdef", "changeme",
    "secret", "login", "master", "sunshine", "princess", "trustno",
)


# ─────────────────── configuration ───────────────────

def auth_configured() -> bool:
    return len(get_auth_jwt_secret()) >= _MIN_SECRET_LENGTH


def require_auth_configured() -> None:
    if not auth_configured():
        raise HTTPException(status_code=503, detail="Sign-in is not configured on this server.")


# ─────────────────── passwords ───────────────────

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "$".join((
        _ALGORITHM,
        str(_ITERATIONS),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ))


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        algorithm, iterations, salt_b64, hash_b64 = stored.split("$")
        if algorithm != _ALGORITHM:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


_dummy_hash: Optional[str] = None


def burn_password_check(password: str) -> None:
    """Spend the same time as a real check, so a missing account isn't
    distinguishable from a wrong password by response time."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(os.urandom(16).hex())
    verify_password(password, _dummy_hash)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    e = normalize_email(email)
    return 0 < len(e) <= EMAIL_MAX_LENGTH and bool(_EMAIL_RE.match(e))


def password_problems(password: str, email: str = "", name: str = "") -> list[str]:
    """Same rules as checkPassword() in the frontend's authUtils.js."""
    pw = password or ""
    lower = pw.lower()
    letters = re.sub(r"[^a-z]", "", lower)
    email_local = normalize_email(email).split("@")[0]
    name_parts = [p for p in (name or "").lower().split() if len(p) >= 3]
    min_len = get_auth_password_min_length()

    problems = []
    if not (min_len <= len(pw) <= PASSWORD_MAX_LENGTH):
        problems.append(f"be {min_len}–{PASSWORD_MAX_LENGTH} characters long")
    if not re.search(r"[a-z]", pw):
        problems.append("contain a lowercase letter")
    if not re.search(r"[A-Z]", pw):
        problems.append("contain an uppercase letter")
    if not re.search(r"\d", pw):
        problems.append("contain a number")
    if not re.search(r"[^A-Za-z0-9\s]", pw):
        problems.append("contain a symbol")
    if (len(email_local) >= 3 and email_local in lower) or any(p in lower for p in name_parts):
        problems.append("not contain your name or email")
    if (
        any(letters == w or (len(w) >= 6 and w in letters) for w in _COMMON_WORDS)
        or re.search(r"(.)\1{3,}", pw)
    ):
        problems.append("not be a common or repetitive password")
    return problems


def check_password_policy(password: str, email: str = "", name: str = "") -> None:
    problems = password_problems(password, email, name)
    if problems:
        raise HTTPException(status_code=422, detail="Password must " + ", ".join(problems) + ".")


# ─────────────────── tokens ───────────────────

def password_fingerprint(password_hash: Optional[str]) -> str:
    return hashlib.sha256((password_hash or "").encode("utf-8")).hexdigest()[:16]


def create_token(user, purpose: str) -> str:
    minutes = get_auth_access_token_minutes() if purpose == PURPOSE_ACCESS else get_auth_reset_token_minutes()
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user.id,
        "purpose": purpose,
        "iat": now,
        "exp": now + timedelta(minutes=minutes),
        "pwv": password_fingerprint(user.password_hash),
    }
    return jwt.encode(payload, get_auth_jwt_secret(), algorithm=_JWT_ALG)


def user_from_token(db: Session, token: str, purpose: str):
    """Return the user a valid token belongs to, or None.

    Checks signature, expiry, purpose, that the user still exists, and that
    the password hasn't changed since the token was issued.
    """
    from app.models import User

    if not token or not auth_configured():
        return None
    try:
        payload = jwt.decode(
            token,
            get_auth_jwt_secret(),
            algorithms=[_JWT_ALG],
            options={"require": ["sub", "exp", "iat", "purpose", "pwv"]},
        )
    except jwt.PyJWTError:
        return None
    if payload.get("purpose") != purpose:
        return None
    user = db.query(User).filter(User.id == str(payload["sub"])).first()
    if not user:
        return None
    if not hmac.compare_digest(str(payload["pwv"]), password_fingerprint(user.password_hash)):
        return None
    return user


def find_user_by_email(db: Session, email: str):
    """Case-insensitive lookup. If older data holds case-variant duplicates,
    prefer the row that already has a password."""
    from app.models import User

    return (
        db.query(User)
        .filter(func.lower(User.email) == normalize_email(email))
        .order_by(User.password_hash.is_(None), User.created_at)
        .first()
    )


def user_payload(user) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "org_id": user.org_id,
    }


def _session_token(request: Request) -> str:
    token = request.cookies.get(get_auth_cookie_name(), "")
    if token:
        return token
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer":
        return value.strip()
    return ""


def require_user(request: Request, db: Session = Depends(get_db)):
    """Dependency for dashboard routes: the signed-in user, or 401.

    Reads the session cookie, or an ``Authorization: Bearer`` header. Don't put
    this on /proxy or the SDK/API-key routes — they authenticate differently.
    """
    require_auth_configured()
    user = user_from_token(db, _session_token(request), PURPOSE_ACCESS)
    if not user:
        raise HTTPException(status_code=401, detail="Not signed in.")
    return user


def require_admin(user=Depends(require_user)):
    """Dependency for /admin routes: the signed-in admin, 401 or 403."""
    if (user.role or "").strip().lower() != ADMIN_ROLE:
        raise HTTPException(status_code=403, detail="Administrator access required.")
    return user
