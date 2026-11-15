"""
tests/fixtures/academic_world.py
────────────────────────────────
A realistic academic world, reused by the document, retrieval and chat tests (design C2).

Two tenants. In each tenant:
  - 3 classes: 10-A, 10-B, 11-A and 4 subjects: Math, Physics, Chemistry, English.
  - Offered (class, subject) pairs:
        10-A: Math, Physics, English      10-B: Math, Physics, English      11-A: Chemistry, English
    so a pair such as 11-A Physics is NOT offered (useful for composite FK tests).
  - Teacher T1 teaches 10-A Math, 10-A Physics and 10-B Math.
    Teacher T2 teaches 10-B Physics and 11-A Chemistry.
  - Student S1 is enrolled in 10-A and 11-A. Student S2 is enrolled in 10-B.
  - One admin (tenant_admin role) and one "viewer" user whose custom role holds ONLY document:view.

All slugs and emails are unique, full-length random values, so tests never collide.
Everything is created through the privileged (no-RLS) session, like the other fixtures.
"""

import hashlib
import uuid
from dataclasses import dataclass, field

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models.academic import Class, ClassSubject, Enrollment, Subject, TeacherAssignment
from app.db.models.document import (
    Chunk,
    ChunkEmbedding,
    Document,
    DocumentClass,
    DocumentVersion,
    IngestJob,
)
from app.db.models.rbac import Role, RolePermission, UserRole
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.ml.model_keys import E5_SMALL_DIM, E5_SMALL_MODEL_KEY
from app.services.rbac import seed_tenant_system_roles

CLASS_NAMES = ("10-A", "10-B", "11-A")
SUBJECT_NAMES = ("Math", "Physics", "Chemistry", "English")
OFFERED_PAIRS = (
    ("10-A", "Math"),
    ("10-A", "Physics"),
    ("10-A", "English"),
    ("10-B", "Math"),
    ("10-B", "Physics"),
    ("10-B", "English"),
    ("11-A", "Chemistry"),
    ("11-A", "English"),
)
T1_PAIRS = (("10-A", "Math"), ("10-A", "Physics"), ("10-B", "Math"))
T2_PAIRS = (("10-B", "Physics"), ("11-A", "Chemistry"))
S1_CLASSES = ("10-A", "11-A")
S2_CLASSES = ("10-B",)


@dataclass
class WorldTenant:
    """Everything created for one tenant."""

    tenant: Tenant
    classes: dict[str, Class]
    subjects: dict[str, Subject]
    admin: User
    t1: User
    t2: User
    s1: User
    s2: User
    viewer: User
    viewer_role: Role
    tag: str = field(default="")

    @property
    def id(self) -> uuid.UUID:
        return self.tenant.id

    def pair(self, class_name: str, subject_name: str) -> tuple[uuid.UUID, uuid.UUID]:
        """(class_id, subject_id) for a named pair."""
        return self.classes[class_name].id, self.subjects[subject_name].id

    def token(self, user: User) -> str:
        """A tenant access token for one of this tenant's users."""
        return create_access_token(
            tenant_id=str(self.tenant.id), user_id=str(user.id), is_admin=user.is_admin
        )


@dataclass
class AcademicWorld:
    a: WorldTenant
    b: WorldTenant


async def _make_user(
    db: AsyncSession, tenant: Tenant, label: str, *, is_admin: bool = False
) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"{label}-{uuid.uuid4().hex}@world.example.com",
        full_name=f"World {label}",
        password_hash=None,
        status="active",
        is_admin=is_admin,
    )
    db.add(user)
    await db.flush()
    return user


async def _system_role_id(db: AsyncSession, tenant_id: uuid.UUID, name: str) -> uuid.UUID:
    res = await db.execute(select(Role.id).where(Role.tenant_id == tenant_id, Role.name == name))
    return res.scalar_one()


async def build_world_tenant(db: AsyncSession, tag: str) -> WorldTenant:
    """Create one tenant with the full academic structure and people. Commits."""
    tenant = Tenant(slug=f"world-{tag}-{uuid.uuid4().hex}", name=f"World {tag}", kind="school")
    db.add(tenant)
    await db.flush()
    await seed_tenant_system_roles(tenant.id, db)

    classes = {n: Class(tenant_id=tenant.id, name=n) for n in CLASS_NAMES}
    subjects = {n: Subject(tenant_id=tenant.id, name=n) for n in SUBJECT_NAMES}
    db.add_all([*classes.values(), *subjects.values()])
    await db.flush()

    db.add_all(
        ClassSubject(
            tenant_id=tenant.id, class_id=classes[c].id, subject_id=subjects[s].id
        )
        for c, s in OFFERED_PAIRS
    )
    await db.flush()

    admin = await _make_user(db, tenant, "admin", is_admin=True)
    t1 = await _make_user(db, tenant, "t1")
    t2 = await _make_user(db, tenant, "t2")
    s1 = await _make_user(db, tenant, "s1")
    s2 = await _make_user(db, tenant, "s2")
    viewer = await _make_user(db, tenant, "viewer")

    db.add_all(
        [
            UserRole(
                tenant_id=tenant.id,
                user_id=admin.id,
                role_id=await _system_role_id(db, tenant.id, "tenant_admin"),
            ),
            UserRole(
                tenant_id=tenant.id,
                user_id=t1.id,
                role_id=await _system_role_id(db, tenant.id, "teacher"),
            ),
            UserRole(
                tenant_id=tenant.id,
                user_id=t2.id,
                role_id=await _system_role_id(db, tenant.id, "teacher"),
            ),
            UserRole(
                tenant_id=tenant.id,
                user_id=s1.id,
                role_id=await _system_role_id(db, tenant.id, "student"),
            ),
            UserRole(
                tenant_id=tenant.id,
                user_id=s2.id,
                role_id=await _system_role_id(db, tenant.id, "student"),
            ),
        ]
    )

    # Custom role holding ONLY document:view.
    viewer_role = Role(
        tenant_id=tenant.id, name="doc-viewer", description="Can only view documents", is_system=False
    )
    db.add(viewer_role)
    await db.flush()
    db.add(RolePermission(tenant_id=tenant.id, role_id=viewer_role.id, permission_id="document:view"))
    db.add(UserRole(tenant_id=tenant.id, user_id=viewer.id, role_id=viewer_role.id))

    for teacher, pairs in ((t1, T1_PAIRS), (t2, T2_PAIRS)):
        for c, s in pairs:
            db.add(
                TeacherAssignment(
                    tenant_id=tenant.id,
                    user_id=teacher.id,
                    class_id=classes[c].id,
                    subject_id=subjects[s].id,
                )
            )
    for student, class_names in ((s1, S1_CLASSES), (s2, S2_CLASSES)):
        for c in class_names:
            db.add(Enrollment(tenant_id=tenant.id, user_id=student.id, class_id=classes[c].id))

    await db.commit()
    return WorldTenant(
        tenant=tenant,
        classes=classes,
        subjects=subjects,
        admin=admin,
        t1=t1,
        t2=t2,
        s1=s1,
        s2=s2,
        viewer=viewer,
        viewer_role=viewer_role,
        tag=tag,
    )


async def build_academic_world(db: AsyncSession) -> AcademicWorld:
    """Create both tenants of the world."""
    return AcademicWorld(
        a=await build_world_tenant(db, "a"),
        b=await build_world_tenant(db, "b"),
    )


@pytest_asyncio.fixture
async def academic_world(raw_db: AsyncSession) -> AcademicWorld:
    return await build_academic_world(raw_db)


# ── Document graph helper (rows only; no files, no embedding model) ───────────


@dataclass
class DocumentGraph:
    document: Document
    version: DocumentVersion
    chunk: Chunk
    embedding: ChunkEmbedding
    job: IngestJob
    canary: str


def fake_vector(seed: str, dim: int = E5_SMALL_DIM) -> list[float]:
    """A deterministic unit-ish vector for tests (not a real embedding)."""
    digest = hashlib.sha256(seed.encode()).digest()
    vec = [((digest[i % len(digest)] / 255.0) - 0.5) + (i % 7) * 0.01 for i in range(dim)]
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec]


async def create_document_graph(
    db: AsyncSession,
    wt: WorldTenant,
    *,
    subject_name: str,
    class_names: tuple[str, ...],
    uploader: User,
    status: str = "published",
    canary: str | None = None,
) -> DocumentGraph:
    """
    Insert a document linked to the given classes with one version, one chunk (containing a
    unique canary string), one embedding and one ingest job. Uses the privileged session.
    """
    canary = canary or f"CANARY-{wt.tag}-{subject_name[:3].upper()}-{uuid.uuid4().hex[:8]}"
    subject = wt.subjects[subject_name]
    doc = Document(
        tenant_id=wt.id, subject_id=subject.id, title=f"{subject_name} syllabus", created_by=uploader.id
    )
    db.add(doc)
    await db.flush()
    db.add_all(
        DocumentClass(
            tenant_id=wt.id,
            document_id=doc.id,
            class_id=wt.classes[c].id,
            subject_id=subject.id,
        )
        for c in class_names
    )
    version = DocumentVersion(
        tenant_id=wt.id,
        document_id=doc.id,
        version_no=1,
        status=status,
        file_kind="pdf",
        original_filename="syllabus.pdf",
        size_bytes=1234,
        sha256=hashlib.sha256(canary.encode()).hexdigest(),
        storage_key=f"t/{wt.id}/d/{uuid.uuid4().hex}",
        uploaded_by=uploader.id,
        embedding_model=E5_SMALL_MODEL_KEY,
    )
    db.add(version)
    await db.flush()
    text_body = f"Unit 1 of {subject_name}. {canary}. Newton's laws and the nature of motion."
    chunk = Chunk(
        tenant_id=wt.id,
        document_version_id=version.id,
        document_id=doc.id,
        subject_id=subject.id,
        ordinal=0,
        page_start=1,
        page_end=1,
        heading_path=f"{subject_name} > Unit 1",
        content=text_body,
        content_sha256=hashlib.sha256(text_body.encode()).hexdigest(),
        token_count=20,
        script="latn",
    )
    db.add(chunk)
    await db.flush()
    embedding = ChunkEmbedding(
        tenant_id=wt.id,
        chunk_id=chunk.id,
        embedding_model=E5_SMALL_MODEL_KEY,
        embedding_dim=E5_SMALL_DIM,
        embedding=fake_vector(canary),
    )
    job = IngestJob(tenant_id=wt.id, version_id=version.id, status="queued")
    db.add_all([embedding, job])
    await db.flush()
    return DocumentGraph(doc, version, chunk, embedding, job, canary)
