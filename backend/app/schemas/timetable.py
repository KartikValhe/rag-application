"""
app/schemas/timetable.py
────────────────────────
Pydantic schemas for Timetables, Entries, Conflict Detection, and Schedule Lookups.
"""

import uuid
from datetime import datetime, time
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class DayOfWeek(int):
    """0 = Monday, 1 = Tuesday, 2 = Wednesday, 3 = Thursday, 4 = Friday, 5 = Saturday, 6 = Sunday."""
    pass


DAY_NAMES = {
    0: "Monday",
    1: "Tuesday",
    2: "Wednesday",
    3: "Thursday",
    4: "Friday",
    5: "Saturday",
    6: "Sunday",
}


class ConflictType(StrEnum):
    CLASS_OVERLAP = "class_overlap"
    TEACHER_OVERLAP = "teacher_overlap"
    ROOM_OVERLAP = "room_overlap"


class TimetableEntryBase(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6, description="0=Monday ... 6=Sunday")
    start_time: time
    end_time: time
    subject_id: uuid.UUID
    teacher_id: uuid.UUID | None = None
    room: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=255)

    @field_validator("end_time")
    @classmethod
    def _validate_time_order(cls, v: time, info: object) -> time:
        data = getattr(info, "data", {})
        start = data.get("start_time")
        if start and v <= start:
            raise ValueError("end_time must be strictly after start_time")
        return v


class TimetableEntryCreate(TimetableEntryBase):
    pass


class TimetableEntryUpdate(TimetableEntryBase):
    pass


class TimetableEntryResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    timetable_id: uuid.UUID
    class_id: uuid.UUID
    class_name: str | None = None
    subject_id: uuid.UUID
    subject_name: str | None = None
    subject_code: str | None = None
    teacher_id: uuid.UUID | None = None
    teacher_name: str | None = None
    day_of_week: int
    day_name: str | None = None
    start_time: str
    end_time: str
    room: str | None = None
    note: str | None = None
    created_at: datetime


class TimetableCreate(BaseModel):
    academic_year: str | None = Field(default=None, max_length=50)
    term: str | None = Field(default=None, max_length=50)
    entries: list[TimetableEntryCreate] = Field(default_factory=list, max_length=200)


class TimetableUpdate(BaseModel):
    academic_year: str | None = Field(default=None, max_length=50)
    term: str | None = Field(default=None, max_length=50)
    entries: list[TimetableEntryCreate] | None = Field(default=None, max_length=200)


class TimetableResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    class_id: uuid.UUID
    class_name: str | None = None
    academic_year: str | None = None
    term: str | None = None
    status: str  # draft | published
    version: int
    entries: list[TimetableEntryResponse] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TimetableConflict(BaseModel):
    conflict_type: ConflictType
    day_of_week: int
    day_name: str
    start_time: str
    end_time: str
    description: str
    class_name: str | None = None
    subject_name: str | None = None
    teacher_name: str | None = None
    room: str | None = None


class ConflictCheckResponse(BaseModel):
    has_conflicts: bool
    conflicts: list[TimetableConflict] = Field(default_factory=list)


class PublishTimetableRequest(BaseModel):
    confirm_conflicts: bool = Field(
        default=False,
        description="Proceed with publishing even if warnings/conflicts were found.",
    )


class ScheduleItem(BaseModel):
    entry_id: uuid.UUID | None = None
    class_id: uuid.UUID
    class_name: str
    subject_id: uuid.UUID
    subject_name: str
    subject_code: str | None = None
    teacher_id: uuid.UUID | None = None
    teacher_name: str | None = None
    room: str | None = None
    day_of_week: int
    day_name: str
    start_time: str
    end_time: str
    date: str  # YYYY-MM-DD
    is_current: bool = False
    note: str | None = None


class NextLectureResponse(BaseModel):
    status: str  # in_progress | upcoming_today | upcoming_future | no_more_today | no_lectures_scheduled | no_published_timetable
    message: str
    current_lecture: ScheduleItem | None = None
    next_lecture: ScheduleItem | None = None
    tenant_timezone: str = "Asia/Kolkata"


class MyTimetableResponse(BaseModel):
    tenant_timezone: str
    classes: list[TimetableResponse] = Field(default_factory=list)
    teaching_entries: list[TimetableEntryResponse] = Field(default_factory=list)


# ─── Timetable Import & Preview Schemas ─────────────────────────────────────────

class ImportedDraftEntryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    day_of_week: int = Field(..., ge=0, le=6, description="0=Monday ... 6=Sunday")
    start_time: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="HH:MM format")
    end_time: str = Field(..., pattern=r"^\d{2}:\d{2}$", description="HH:MM format")
    subject_id: uuid.UUID
    teacher_id: uuid.UUID | None = None
    room: str | None = Field(default=None, max_length=50)
    note: str | None = Field(default=None, max_length=200)

    @field_validator("end_time")
    @classmethod
    def _validate_end_after_start(cls, v: str, info: object) -> str:
        data = getattr(info, "data", {})
        start = data.get("start_time")
        if start and v <= start:
            raise ValueError("end_time must be strictly after start_time")
        return v


class SaveImportedDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replace_confirmed: bool = Field(
        default=False,
        description="Explicit confirmation to replace existing draft timetable entries.",
    )
    entries: list[ImportedDraftEntryItem] = Field(
        ...,
        max_length=500,
        description="List of validated entries to save to the draft timetable.",
    )


class ParsedPreviewRow(BaseModel):
    row_index: int
    day_of_week: int | None = None
    day_name: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    subject_id: uuid.UUID | None = None
    subject_code: str | None = None
    subject_name: str | None = None
    teacher_id: uuid.UUID | None = None
    teacher_email: str | None = None
    teacher_name: str | None = None
    room: str | None = None
    note: str | None = None
    raw_data: dict[str, str | None] = Field(default_factory=dict)
    period_label: str | None = None
    requires_period_time: bool = False
    merged_cell: bool = False
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class AvailableSubjectItem(BaseModel):
    id: uuid.UUID
    code: str | None = None
    name: str


class AvailableTeacherItem(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str


class ImportPreviewResponse(BaseModel):
    filename: str
    total_rows: int
    valid_rows: int
    error_rows: int
    requires_period_times: bool = False
    period_labels: list[str] = Field(default_factory=list)
    rows: list[ParsedPreviewRow] = Field(default_factory=list)
    available_subjects: list[AvailableSubjectItem] = Field(default_factory=list)
    available_teachers: list[AvailableTeacherItem] = Field(default_factory=list)


