"""Create (or promote) an admin account. Safe to run in production.

Usage:
    python -m scripts.create_admin --phone 9XXXXXXXXX --name "Owner Name" [--email x@y.in]

The password is read from the ADMIN_PASSWORD environment variable, or asked for interactively.
It is never printed or logged.
"""

import argparse
import getpass
import os

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.security import hash_password, password_problem
from app.db.models import User, UserRole
from app.db.session import get_engine
from app.services.checkout import PHONE_RE, normalise_phone


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--email")
    args = parser.parse_args()

    phone = normalise_phone(args.phone)
    if not PHONE_RE.match(phone):
        raise SystemExit("Enter a valid 10-digit mobile number.")
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")
    problem = password_problem(password)
    if problem:
        raise SystemExit(problem)

    with Session(get_engine()) as db:
        user = db.scalar(select(User).where(User.phone == phone))
        if user is None:
            user = User(
                phone=phone, full_name=args.name, email=args.email or None, password_hash="!"
            )
            db.add(user)
        user.role = UserRole.ADMIN
        user.is_active = True
        user.full_name = args.name
        user.password_hash = hash_password(password)
        db.commit()
        print(f"Admin account ready for {phone}.")


if __name__ == "__main__":
    main()
