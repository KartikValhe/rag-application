"""
app/services/document_scope.py
──────────────────────────────
THE document scope (design A4). Every document route uses these functions, and the retriever will
too; no route has its own copy of the rule.

Scope is always a set of (class_id, subject_id) PAIRS, never a cross product of classes and
subjects: a teacher on 10-A Math and 10-B Physics never reaches 10-A Physics.

    authorized_pairs(db, user, purpose)
        admin   -> every class_subjects row of the tenant
        teacher -> the teacher_assignments pairs that the class really offers (class_subjects)
        student -> purpose "read" only: enrolled classes x the subjects offered in THAT class
        other   -> nothing
    authorize_document_scope(db, user, subject_id, class_ids)
        upload / link: every (class, subject) pair must be in authorized_pairs("manage"), else the
        generic 404.
    load_document_for(db, user, document_id, action)
        view:                    any linked pair in scope
        manage, publish, delete: ALL linked pairs in scope; some but not all -> 403
                                 document_manage_requires_all_classes; none -> generic 404
        soft-deleted, unknown or another tenant's document -> generic 404

Admins can always view and manage (also a document that lost all its class links, backlog B19).
Every query names the tenant and runs in the caller's tenant session, so RLS applies underneath.
"""

import uuid
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import GENERIC_NOT_FOUND
from app.db.models.academic import ClassSubject, Enrollment, TeacherAssignment
from app.db.models.document import Document, DocumentClass
from app.db.models.rbac import Role, UserRole
from app.db.models.user import User

Pair = tuple[uuid.UUID, uuid.UUID]  # (class_id, subject_id)
Purpose = Literal["manage", "read"]
Action = Literal["view", "manage", "publish", "delete"]

MANAGE_BLOCKED_CODE = "document_manage_requires_all_classes"
MANAGE_BLOCKED = {
    "code": MANAGE_BLOCKED_CODE,
    "message": "You can only change this document if you teach its subject in every class it is linked to.",
}


def not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)


def manage_blocked() -> HTTPException:
    return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MANAGE_BLOCKED)


@dataclass(frozen=True)
class CallerScope:
    """What one caller may reach: admin flag plus the authorized pairs."""

    is_admin: bool
    pairs: frozenset[Pair]

    def access(self, linked: frozenset[Pair]) -> tuple[bool, bool]:
        """(visible, can_manage) of a document linked to `linked`. The only copy of the rule."""
        if self.is_admin:
            return True, True
        visible = bool(linked & self.pairs)
        return visible, visible and linked <= self.pairs


@dataclass(frozen=True)
class ScopedDocument:
    """A document the caller may see, with its linked pairs and whether they may change it."""

    document: Document
    linked: frozenset[Pair]
    can_manage: bool

    @property
    def blocked_reason(self) -> str | None:
        return None if self.can_manage else MANAGE_BLOCKED_CODE


async def _role_names(db: AsyncSession, user: User) -> set[str]:
    rows = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.tenant_id == user.tenant_id, UserRole.user_id == user.id, Role.tenant_id == user.tenant_id)
    )
    return set(rows.scalars().all())


async def is_document_admin(db: AsyncSession, user: User) -> bool:
    return user.is_admin or "tenant_admin" in await _role_names(db, user)


async def authorized_pairs(db: AsyncSession, user: User, *, purpose: Purpose) -> frozenset[Pair]:
    """The (class_id, subject_id) pairs this user may reach for `purpose`. See the module docstring."""
    tid = user.tenant_id
    roles = await _role_names(db, user)
    offered = select(ClassSubject.class_id, ClassSubject.subject_id).where(ClassSubject.tenant_id == tid)
    if user.is_admin or "tenant_admin" in roles:
        return frozenset((r[0], r[1]) for r in (await db.execute(offered)).all())

    pairs: set[Pair] = set()
    if "teacher" in roles:
        rows = await db.execute(
            select(TeacherAssignment.class_id, TeacherAssignment.subject_id)
            .join(
                ClassSubject,
                (ClassSubject.tenant_id == TeacherAssignment.tenant_id)
                & (ClassSubject.class_id == TeacherAssignment.class_id)
                & (ClassSubject.subject_id == TeacherAssignment.subject_id),
            )
            .where(TeacherAssignment.tenant_id == tid, TeacherAssignment.user_id == user.id)
        )
        pairs |= {(r[0], r[1]) for r in rows.all()}
    if purpose == "read" and "student" in roles:
        rows = await db.execute(
            select(ClassSubject.class_id, ClassSubject.subject_id)
            .join(
                Enrollment,
                (Enrollment.tenant_id == ClassSubject.tenant_id) & (Enrollment.class_id == ClassSubject.class_id),
            )
            .where(ClassSubject.tenant_id == tid, Enrollment.user_id == user.id)
        )
        pairs |= {(r[0], r[1]) for r in rows.all()}
    return frozenset(pairs)


async def caller_scope(db: AsyncSession, user: User, *, purpose: Purpose = "manage") -> CallerScope:
    """The caller's scope for the document routes (teachers and admins manage by their pairs)."""
    admin = await is_document_admin(db, user)
    return CallerScope(is_admin=admin, pairs=await authorized_pairs(db, user, purpose=purpose))


async def authorize_document_scope(
    db: AsyncSession, user: User, subject_id: uuid.UUID, class_ids: list[uuid.UUID]
) -> None:
    """Upload or link: every (class, subject) pair must be manageable by the user, else the generic 404."""
    requested = {(c, subject_id) for c in class_ids}
    if not requested:
        raise not_found()
    allowed = await authorized_pairs(db, user, purpose="manage")
    if not requested <= allowed:
        raise not_found()


async def linked_pairs(db: AsyncSession, tenant_id: uuid.UUID, document_ids: list[uuid.UUID]) -> dict[uuid.UUID, frozenset[Pair]]:
    """The (class, subject) links of each document (documents without links map to an empty set)."""
    out: dict[uuid.UUID, set[Pair]] = {d: set() for d in document_ids}
    if document_ids:
        rows = await db.execute(
            select(DocumentClass.document_id, DocumentClass.class_id, DocumentClass.subject_id).where(
                DocumentClass.tenant_id == tenant_id, DocumentClass.document_id.in_(document_ids)
            )
        )
        for doc_id, class_id, subject_id in rows.all():
            out[doc_id].add((class_id, subject_id))
    return {d: frozenset(p) for d, p in out.items()}


async def load_document_for(
    db: AsyncSession, user: User, document_id: uuid.UUID, *, action: Action, for_update: bool = False
) -> ScopedDocument:
    """
    The document if the caller may perform `action` on it. view needs one linked pair in scope;
    manage, publish and delete need all of them (403 when only some). Anything else is the generic
    404. With for_update the document row is locked (FOR NO KEY UPDATE) for the caller's transaction.
    """
    stmt = select(Document).where(
        Document.tenant_id == user.tenant_id, Document.id == document_id, Document.deleted_at.is_(None)
    )
    if for_update:
        stmt = stmt.with_for_update(key_share=True)
    document = (await db.execute(stmt)).scalar_one_or_none()
    if document is None:
        raise not_found()
    linked = (await linked_pairs(db, user.tenant_id, [document_id]))[document_id]
    visible, can_manage = (await caller_scope(db, user)).access(linked)
    if not visible:
        raise not_found()
    if action != "view" and not can_manage:
        raise manage_blocked()
    return ScopedDocument(document=document, linked=linked, can_manage=can_manage)
