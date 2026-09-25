"""Create the first dashboard admin (there is no public sign-up).

Creates the user with the admin role and no password, then emails them a
set-password invite link. The link is printed instead when the email can't be
sent (SMTP not configured), or always with --print-link. If the email already
belongs to a user, that user is promoted to admin; they get an invite link only
if they haven't set a password yet.

Needs the same DATABASE_URL, AUTH_JWT_SECRET and FRONTEND_URL as the server
(read from .env), so the link it prints is one the server accepts.

Example:

    python scripts/create_admin.py --email you@company.com --name "Your Name"
"""
import argparse
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.auth import (  # noqa: E402
    ADMIN_ROLE,
    PURPOSE_INVITE,
    auth_configured,
    find_user_by_email,
    is_valid_email,
    normalize_email,
)
from app.database import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.routers.auth import send_invite_email, token_link  # noqa: E402
from app.services.audit_service import log_event  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="The admin's work email")
    parser.add_argument("--name", required=True, help="The admin's full name")
    parser.add_argument("--org-id", default=None, help="Organization id to attach a new user to (optional)")
    parser.add_argument("--print-link", action="store_true", help="Print the invite link even if the email was sent")
    args = parser.parse_args()

    if not auth_configured():
        sys.exit("AUTH_JWT_SECRET is missing or shorter than 32 characters; set it first (same value as the server).")
    email = normalize_email(args.email)
    name = " ".join(args.name.split())
    if not is_valid_email(email) or not name:
        sys.exit("Please pass a valid --email and a non-empty --name.")

    db = SessionLocal()
    try:
        user = find_user_by_email(db, email)
        if user:
            action = "promoted to admin" if user.role != ADMIN_ROLE else "is already an admin"
            user.role = ADMIN_ROLE
            user.name = user.name or name
        else:
            action = "created as admin"
            user = User(id=str(uuid.uuid4()), email=email, name=name, role=ADMIN_ROLE, org_id=args.org_id)
            db.add(user)
        log_event(
            db,
            org_id=user.org_id or "system",
            audit_category="user_management",
            audit_action="admin_bootstrapped",
            actor_type="cli",
            entity_type="user",
            entity_id=user.id,
            compliance_relevant=True,
            change_summary=f"{email} {action} via scripts/create_admin.py",
            metadata={"target_email": email},
            flush=False,
        )
        db.commit()
        db.refresh(user)
        print(f"{email} {action}.")

        if user.password_hash:
            print("They already have a password and can sign in (or use 'Forgot password').")
            return
        link = token_link(user, PURPOSE_INVITE)
        sent = send_invite_email(user, link)
        print("Invite email sent." if sent else "Invite email could NOT be sent (check SMTP_* settings).")
        if args.print_link or not sent:
            print("Set-password link (one use; share only with this person):")
            print(link)
    finally:
        db.close()


if __name__ == "__main__":
    main()
