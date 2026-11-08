"""
app/services/invitation.py
───────────────────────────
Invitation and bulk-import logic for tenant admins.
"""

import csv
import io
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import generate_email_token, hash_email_token, hash_password
from app.db.models.auth import Invitation
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services.email import send_invitation_email
from app.services.rbac import seed_tenant_system_roles

logger = structlog.get_logger(__name__)


async def create_invitation(
    *,
    tenant: Tenant,
    email: str,
    role_name: str,
    invited_by_user: User,
    db: AsyncSession,
) -> Invitation:
    """
    Create an invitation and send the invite email.
    Idempotent: re-sending to the same email cancels the previous pending
    invitation and issues a fresh one.
    """
    email = email.lower()

    # Check if the user is already a member.
    existing_user = await db.execute(select(User).where(User.email == email))
    if existing_user.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "user_exists", "message": "A user with this email already exists."},
        )

    # Cancel any previous pending invitation for this email.
    await db.execute(
        update(Invitation)
        .where(
            Invitation.email == email,
            Invitation.status == "pending",
        )
        .values(status="cancelled")
    )

    raw_token = generate_email_token()
    invitation = Invitation(
        tenant_id=tenant.id,
        email=email,
        role_name=role_name,
        token_hash=hash_email_token(raw_token),
        invited_by=invited_by_user.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db.add(invitation)
    await db.flush()

    accept_url = (
        f"{settings.FRONTEND_URL}/accept-invitation?token={raw_token}"
        f"&tenant={tenant.slug}"
    )
    await send_invitation_email(
        to_email=email,
        invited_by_name=invited_by_user.full_name,
        accept_url=accept_url,
        tenant_name=tenant.name,
        role_name=role_name,
    )
    logger.info("invitation_created", email=email, invitation_id=str(invitation.id))
    return invitation


async def accept_invitation(
    *,
    raw_token: str,
    full_name: str,
    password: str,
    db: AsyncSession,
) -> User:
    """
    Accept a pending invitation: create the user and mark the invitation accepted.
    """
    hashed = hash_email_token(raw_token)
    result = await db.execute(
        select(Invitation).where(Invitation.token_hash == hashed)
    )
    invitation = result.scalar_one_or_none()
    expires_at = invitation.expires_at if invitation else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)

    if (
        invitation is None
        or invitation.status != "pending"
        or (expires_at is not None and expires_at < datetime.now(UTC))
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_token", "message": "Invitation is invalid or expired."},
        )

    # Check the email is still available (edge case: same email registered meanwhile).
    dup = await db.execute(select(User).where(User.email == invitation.email))
    if dup.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "email_taken"},
        )

    user = User(
        tenant_id=invitation.tenant_id,
        email=invitation.email,
        full_name=full_name,
        password_hash=hash_password(password),
        status="active",
        is_admin=False,
    )
    db.add(user)
    await db.execute(
        update(Invitation)
        .where(Invitation.id == invitation.id)
        .values(status="accepted")
    )
    logger.info("invitation_accepted", email=invitation.email)
    return user


# ── Bulk CSV import ──────────────────────────────────────────────────────────

class ImportRow:
    """Result for one CSV row."""
    __slots__ = ("row_number", "email", "full_name", "role_name", "success", "error")

    def __init__(
        self,
        row_number: int,
        email: str,
        full_name: str,
        role_name: str,
        success: bool,
        error: str | None = None,
    ) -> None:
        self.row_number = row_number
        self.email = email
        self.full_name = full_name
        self.role_name = role_name
        self.success = success
        self.error = error

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_number": self.row_number,
            "email": self.email,
            "full_name": self.full_name,
            "role_name": self.role_name,
            "success": self.success,
            "error": self.error,
        }


async def bulk_import_users(
    *,
    csv_content: bytes,
    tenant: Tenant,
    imported_by: User,
    db: AsyncSession,
) -> list[ImportRow]:
    """
    Parse a CSV file and create users (or invitations) in bulk.

    Expected CSV columns (order-independent, header row required):
      email, full_name, role_name (optional, defaults to "student")

    Each row is processed independently; a failure on one row does not
    abort the others.  Returns one ImportRow per input row.
    """
    try:
        text = csv_content.decode("utf-8-sig")  # Handle BOM from Excel exports.
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "invalid_csv", "message": "CSV must be UTF-8 encoded."},
        ) from None

    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "empty_csv", "message": "CSV file is empty."},
        )

    required_fields = {"email", "full_name"}
    actual_fields = {f.strip().lower() for f in reader.fieldnames}
    missing = required_fields - actual_fields
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "missing_columns",
                "message": f"CSV is missing required columns: {missing}",
            },
        )

    results: list[ImportRow] = []

    for row_num, row in enumerate(reader, start=2):  # Row 1 is header.
        email = (row.get("email") or "").strip().lower()
        full_name = (row.get("full_name") or "").strip()
        role_name = (row.get("role_name") or "student").strip().lower()

        if not email or "@" not in email:
            results.append(
                ImportRow(row_num, email, full_name, role_name, False, "Invalid email.")
            )
            continue
        if not full_name:
            results.append(
                ImportRow(row_num, email, full_name, role_name, False, "full_name is required.")
            )
            continue

        try:
            # Try to create an invitation (re-uses invite logic + email send).
            await create_invitation(
                tenant=tenant,
                email=email,
                role_name=role_name,
                invited_by_user=imported_by,
                db=db,
            )
            results.append(ImportRow(row_num, email, full_name, role_name, True))
        except HTTPException as exc:
            detail = exc.detail
            error_msg = (
                detail.get("message", str(detail))
                if isinstance(detail, dict)
                else str(detail)
            )
            results.append(ImportRow(row_num, email, full_name, role_name, False, error_msg))

    succeeded = sum(1 for r in results if r.success)
    logger.info(
        "bulk_import_complete",
        total=len(results),
        succeeded=succeeded,
        failed=len(results) - succeeded,
    )
    return results


# ── Admin-created users (superadmin flow) ────────────────────────────────────

async def create_admin_user(
    *,
    tenant: Tenant,
    email: str,
    full_name: str,
    db: AsyncSession,
) -> tuple[User, str]:
    """
    Create the first tenant admin with a temporary password.
    Returns (user, temp_password) so the caller can email the temp password.
    """
    temp_password = secrets.token_urlsafe(12)
    user = User(
        tenant_id=tenant.id,
        email=email.lower(),
        full_name=full_name,
        password_hash=hash_password(temp_password),
        status="active",
        is_admin=True,
    )
    db.add(user)
    await db.flush()

    await seed_tenant_system_roles(tenant.id, db)
    admin_role_stmt = select(Role.id).where(Role.tenant_id == tenant.id, Role.name == "tenant_admin")
    admin_role_id = (await db.execute(admin_role_stmt)).scalar_one_or_none()
    if admin_role_id:
        db.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=admin_role_id))
        await db.flush()

    logger.info("admin_user_created", user_id=str(user.id), tenant_id=str(tenant.id))
    return user, temp_password

