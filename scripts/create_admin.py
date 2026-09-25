"""Create the first dashboard admin (there is no public sign-up), or set a new
password for an admin who is locked out.

Prompts for the password (hidden, typed twice) so it never lands in shell
history. If the email already belongs to a user, that user is promoted to
admin and given the new password, which signs them out everywhere.

Needs the same DATABASE_URL as the server (read from .env).

Example:

    python scripts/create_admin.py --email you@company.com --name "Your Name"
"""
import argparse
import getpass
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.auth import (  # noqa: E402
    ADMIN_ROLE,
    find_user_by_email,
    hash_password,
    is_valid_email,
    normalize_email,
    password_problems,
)
from app.database import SessionLocal  # noqa: E402
from app.models import User  # noqa: E402
from app.services.audit_service import log_event  # noqa: E402


def _ask_password(email: str, name: str) -> str:
    while True:
        password = getpass.getpass("New password: ")
        problems = password_problems(password, email, name)
        if problems:
            print("Password must " + ", ".join(problems) + ".")
            continue
        if getpass.getpass("Repeat password: ") != password:
            print("Passwords don't match.")
            continue
        return password


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True, help="The admin's work email")
    parser.add_argument("--name", required=True, help="The admin's full name")
    parser.add_argument("--org-id", default=None, help="Organization id to attach a new user to (optional)")
    args = parser.parse_args()

    email = normalize_email(args.email)
    name = " ".join(args.name.split())
    if not is_valid_email(email) or not name:
        sys.exit("Please pass a valid --email and a non-empty --name.")

    db = SessionLocal()
    try:
        user = find_user_by_email(db, email)
        password_hash = hash_password(_ask_password(email, user.name if user and user.name else name))
        if user:
            action = "promoted to admin" if user.role != ADMIN_ROLE else "is an admin"
            user.role = ADMIN_ROLE
            user.name = user.name or name
            user.password_hash = password_hash
        else:
            action = "created as admin"
            user = User(
                id=str(uuid.uuid4()),
                email=email,
                name=name,
                role=ADMIN_ROLE,
                org_id=args.org_id,
                password_hash=password_hash,
            )
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
            change_summary=f"{email} {action} (password set) via scripts/create_admin.py",
            metadata={"target_email": email},
            flush=False,
        )
        db.commit()
        print(f"{email} {action}; password set. They can sign in now.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
