"""
Operator commands. Run on the server, never over the network.

    python manage.py create-admin          create an administrator account
    python manage.py pending               list doctor / pharmacy accounts awaiting review
    python manage.py migrate               apply database migrations
    python manage.py new-key               print a fresh DATA_ENCRYPTION_KEY

In Docker:   docker compose exec prescriptai python manage.py create-admin

An administrator approves doctors and pharmacies before they can issue or open
a prescription (admin.py). `/api/auth/register` refuses the role, so this
command is the only way one comes to exist: whoever can run it already controls
the server.

`create-admin` asks for the password on the terminal (not echoed, not in shell
history). For a scripted first deploy, ADMIN_PASSWORD may be set in the
environment for that one command instead.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "ml_pipeline"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from env import load_env  # noqa: E402

load_env()

from auth.models import VETTED_ROLES, Role, User, Vetting, _now  # noqa: E402
from auth.routes import norm_email  # noqa: E402
from auth.security import hash_password, password_problem  # noqa: E402
from config import migrate  # noqa: E402
from config.database import SessionLocal  # noqa: E402


def create_admin(args) -> int:
    migrate.upgrade()
    email = args.email or input("Admin email: ").strip()
    try:
        email = norm_email(email)
    except Exception:
        print("That is not a valid email address.")
        return 2
    name = args.name or input("Full name: ").strip() or "Administrator"
    password = os.environ.get("ADMIN_PASSWORD")
    if not password:
        password = getpass.getpass("Password: ")
        if getpass.getpass("Password again: ") != password:
            print("The passwords do not match.")
            return 2
    problem = password_problem(password, email.split("@")[0])
    if problem:
        print(problem)
        return 2
    with SessionLocal() as db:
        if db.query(User).filter(User.email == email).first():
            print(f"{email} is already registered. Nothing changed.")
            return 1
        # The operator vouches for this address by typing it on the server, so
        # it is marked verified: an admin locked out of password reset would
        # have no one to appeal to.
        db.add(User(role=Role.ADMIN, full_name=name, email=email, email_verified_at=_now(),
                    password_hash=hash_password(password), profile_json="{}"))
        db.commit()
    print(f"Administrator {email} created. Sign in at /login, then turn on two-step "
          f"verification under Account: this account decides who is a doctor.")
    return 0


def pending(_args) -> int:
    migrate.upgrade()
    with SessionLocal() as db:
        rows = [u for u in db.query(User).filter(User.role.in_(VETTED_ROLES)).order_by(User.created_at)
                if (u.vetting_status or Vetting.PENDING.value) == Vetting.PENDING.value]
        if not rows:
            print("No accounts are waiting for review.")
        for u in rows:
            p = json.loads(u.profile_json or "{}")
            number = p.get("registration_no") or p.get("drug_licence_no") or "no number given"
            print(f"{u.role.value:8} {u.full_name:28} {u.email or u.phone or u.username:32} {number}")
    return 0


def run_migrations(_args) -> int:
    migrate.upgrade()
    missing = migrate.drift()
    print("Database is up to date." if not missing else
          "Models changed without a migration. Missing: " + ", ".join(missing))
    return 1 if missing else 0


def new_key(_args) -> int:
    from cryptography.fernet import Fernet

    print(Fernet.generate_key().decode())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PrescriptAI operator commands")
    sub = ap.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create-admin", help="create an administrator account")
    c.add_argument("--email")
    c.add_argument("--name")
    c.set_defaults(fn=create_admin)
    sub.add_parser("pending", help="list accounts awaiting review").set_defaults(fn=pending)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(fn=run_migrations)
    sub.add_parser("new-key", help="print a fresh DATA_ENCRYPTION_KEY").set_defaults(fn=new_key)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
