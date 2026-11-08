"""
app/db/models/academic.py
─────────────────────────
Academic structure models:
  - Class: school section, college batch, or course cohort.
  - Subject: course subject (e.g. Mathematics, Physics).
  - ClassSubject: association linking subjects offered in a specific class.
  - Enrollment: student enrolled in a class.
  - TeacherAssignment: teacher assigned to teach a specific class-subject.

All tenant-scoped tables enforce composite PK / FK and Postgres RLS.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Class(Base):
    """
    A tenant-scoped academic group (section, batch, or course).
    """
    __tablename__ = "classes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_classes_tenant_name"),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(50), default="section", nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Subject(Base):
    """
    A tenant-scoped subject/course.
    """
    __tablename__ = "subjects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_subjects_tenant_name"),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("app.tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ClassSubject(Base):
    """
    Junction linking a subject taught within a specific class.
    """
    __tablename__ = "class_subjects"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "class_id"],
            ["app.classes.tenant_id", "app.classes.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "subject_id"],
            ["app.subjects.tenant_id", "app.subjects.id"],
            ondelete="CASCADE",
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class Enrollment(Base):
    """
    Student enrollment in a class.
    """
    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "class_id", name="uq_enrollments_student_class"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["app.users.tenant_id", "app.users.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "class_id"],
            ["app.classes.tenant_id", "app.classes.id"],
            ondelete="CASCADE",
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    enrolled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class TeacherAssignment(Base):
    """
    Assignment of a teacher to a specific (class, subject) pair.
    """
    __tablename__ = "teacher_assignments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "class_id", "subject_id", name="uq_teacher_assignments"),
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["app.users.tenant_id", "app.users.id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "class_id", "subject_id"],
            ["app.class_subjects.tenant_id", "app.class_subjects.class_id", "app.class_subjects.subject_id"],
            ondelete="CASCADE",
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

