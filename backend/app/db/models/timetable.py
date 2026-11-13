"""
app/db/models/timetable.py
──────────────────────────
Phase 4 Timetable data models:
  - Timetable: Class-level schedule container with status (draft -> published -> archived) and versioning.
  - TimetableEntry: Individual weekly class session (day_of_week, start_time, end_time, subject, teacher, room).

Every table enforces tenant_id composite PK/FK constraints and Row-Level Security (RLS).
"""

import uuid
from datetime import datetime, time

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    Time,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class Timetable(Base):
    """
    Class-level timetable with draft / published status lifecycle and optimistic versioning.
    """
    __tablename__ = "timetables"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'published', 'archived')",
            name="timetables_status_check",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "class_id"],
            ["app.classes.tenant_id", "app.classes.id"],
            ondelete="CASCADE",
        ),
        # At most one draft and one published timetable per class (migration 0007).
        Index(
            "uq_timetables_one_draft_per_class",
            "tenant_id",
            "class_id",
            unique=True,
            postgresql_where=text("status = 'draft'"),
        ),
        Index(
            "uq_timetables_one_published_per_class",
            "tenant_id",
            "class_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    academic_year: Mapped[str | None] = mapped_column(String(50), nullable=True)
    term: Mapped[str | None] = mapped_column(String(50), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20),
        default="draft",
        nullable=False,
    )  # draft | published | archived (read-only history)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class TimetableEntry(Base):
    """
    A single scheduled lecture/session inside a timetable.
    day_of_week: 0 = Monday, 1 = Tuesday, ..., 6 = Sunday (matching Python datetime.weekday()).
    """
    __tablename__ = "timetable_entries"
    __table_args__ = (
        CheckConstraint(
            "day_of_week >= 0 AND day_of_week <= 6",
            name="timetable_entries_day_check",
        ),
        CheckConstraint(
            "start_time < end_time",
            name="timetable_entries_time_check",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "timetable_id"],
            ["app.timetables.tenant_id", "app.timetables.id"],
            ondelete="CASCADE",
        ),
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
        ForeignKeyConstraint(
            ["tenant_id", "teacher_id"],
            ["app.users.tenant_id", "app.users.id"],
            ondelete="SET NULL",
        ),
        {"schema": "app"},
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    timetable_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    class_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    teacher_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0=Monday ... 6=Sunday
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)
    room: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

