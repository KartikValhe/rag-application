"""
backend/app/scripts/create_superadmin.py
────────────────────────────────────────
CLI command to safely create or update a Platform Superadmin account.
Prompts interactively for credentials and enforces strong password requirements.

Usage:
    python -m app.scripts.create_superadmin
    python -m app.scripts.create_superadmin --email admin@platform.local --password "StrongPass123!"
"""

import argparse
import asyncio
import getpass
import re
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.security import hash_password
from app.db.models.tenant import PlatformAdmin


def validate_password_strength(password: str) -> None:
    """
    Enforce strong password policy for superadmin accounts:
      - At least 12 characters
      - At least one lowercase letter
      - At least one uppercase letter
      - At least one digit
      - At least one special symbol
    """
    if len(password) < 12:
        raise ValueError("Password must be at least 12 characters long.")
    if not re.search(r"[a-z]", password):
        raise ValueError("Password must contain at least one lowercase letter.")
    if not re.search(r"[A-Z]", password):
        raise ValueError("Password must contain at least one uppercase letter.")
    if not re.search(r"[0-9]", password):
        raise ValueError("Password must contain at least one digit.")
    if not re.search(r"[\W_]", password):
        raise ValueError("Password must contain at least one special character.")


async def create_or_update_superadmin(
    email: str,
    password: str,
    full_name: str = "Super Administrator",
) -> None:
    clean_email = email.strip().lower()
    if not clean_email or "@" not in clean_email:
        raise ValueError("A valid email address is required.")

    validate_password_strength(password)

    engine = create_async_engine(
        settings.MIGRATION_DATABASE_URL,
        connect_args={"statement_cache_size": 0},
    )
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as session, session.begin():
        res = await session.execute(
            select(PlatformAdmin).where(PlatformAdmin.email == clean_email)
        )
        admin = res.scalar_one_or_none()
        hashed = hash_password(password)

        if not admin:
            admin = PlatformAdmin(
                email=clean_email,
                full_name=full_name,
                password_hash=hashed,
                is_active=True,
            )
            session.add(admin)
            print(f"[OK] Created platform superadmin: {clean_email}")
        else:
            admin.password_hash = hashed
            admin.full_name = full_name
            admin.is_active = True
            print(f"[OK] Updated existing platform superadmin: {clean_email}")

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update a platform superadmin.")
    parser.add_argument("--email", help="Superadmin email address", default=None)
    parser.add_argument("--password", help="Superadmin password (must be strong)", default=None)
    parser.add_argument("--name", help="Superadmin full name", default="Super Administrator")
    args = parser.parse_args()

    email = args.email
    if not email:
        try:
            email = input("Enter superadmin email: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)

    password = args.password
    if not password:
        try:
            password = getpass.getpass("Enter superadmin password (min 12 chars, mixed case, digit, symbol): ")
            password_confirm = getpass.getpass("Confirm password: ")
            if password != password_confirm:
                print("Error: Passwords do not match.", file=sys.stderr)
                sys.exit(1)
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            sys.exit(1)

    try:
        asyncio.run(create_or_update_superadmin(email, password, args.name))
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

