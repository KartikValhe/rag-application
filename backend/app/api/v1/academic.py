"""
app/api/v1/academic.py
───────────────────────
Academic structure endpoints:
  - Classes (CRUD, scoped filtering)
  - Subjects (CRUD, scoped filtering)
  - Class-Subject links
  - Enrollments (add, remove, roster, CSV import)
  - Teacher assignments (create, remove, list)
  - "My Classes" view for teachers and students
"""

import csv
import io
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select

from app.api.deps import TenantDB
from app.core.authz import Scope, ScopeType, load_user_permissions, require
from app.db.models.academic import Class, ClassSubject, Enrollment, Subject, TeacherAssignment
from app.db.models.rbac import Role, UserRole
from app.db.models.user import User
from app.services.audit import record_audit_event

router = APIRouter(tags=["academic"])

GENERIC_NOT_FOUND = {"code": "not_found", "message": "Resource not found."}


# ── Schemas ───────────────────────────────────────────────────────────────────

class ClassResponse(BaseModel):
    id: str
    name: str
    kind: str
    description: str | None = None
    created_at: str


class ClassListResponse(BaseModel):
    items: list[ClassResponse]
    total: int


class CreateClassRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    kind: str = Field("section", max_length=50)
    description: str | None = Field(None, max_length=500)


class UpdateClassRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    kind: str | None = Field(None, max_length=50)
    description: str | None = Field(None, max_length=500)


class SubjectResponse(BaseModel):
    id: str
    name: str
    code: str | None = None
    description: str | None = None
    created_at: str


class SubjectListResponse(BaseModel):
    items: list[SubjectResponse]
    total: int


class CreateSubjectRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    code: str | None = Field(None, max_length=50)
    description: str | None = Field(None, max_length=500)


class UpdateSubjectRequest(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    code: str | None = Field(None, max_length=50)
    description: str | None = Field(None, max_length=500)


class EnrollStudentRequest(BaseModel):
    user_id: uuid.UUID


class AssignTeacherRequest(BaseModel):
    user_id: uuid.UUID


class RosterStudentResponse(BaseModel):
    id: str
    user_id: str
    email: str
    full_name: str
    enrolled_at: str


class RosterResponse(BaseModel):
    items: list[RosterStudentResponse]
    total: int


class TeacherAssignmentResponse(BaseModel):
    id: str
    user_id: str
    teacher_name: str
    teacher_email: str
    class_id: str
    class_name: str
    subject_id: str
    subject_name: str
    assigned_at: str


class TeacherAssignmentListResponse(BaseModel):
    items: list[TeacherAssignmentResponse]
    total: int


class RowErrorResponse(BaseModel):
    row: int
    email: str
    error: str


class EnrollmentImportResponse(BaseModel):
    total_rows: int
    succeeded: int
    failed: int
    errors: list[RowErrorResponse]


class MySubjectResponse(BaseModel):
    id: str
    name: str
    code: str | None = None


class MyClassResponse(BaseModel):
    id: str
    name: str
    kind: str
    subjects: list[MySubjectResponse] = Field(default_factory=list)


class MessageResponse(BaseModel):
    message: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _to_class_response(c: Class) -> ClassResponse:
    return ClassResponse(
        id=str(c.id),
        name=c.name,
        kind=c.kind,
        description=c.description,
        created_at=c.created_at.isoformat() if hasattr(c.created_at, "isoformat") else str(c.created_at),
    )


def _to_subject_response(s: Subject) -> SubjectResponse:
    return SubjectResponse(
        id=str(s.id),
        name=s.name,
        code=s.code,
        description=s.description,
        created_at=s.created_at.isoformat() if hasattr(s.created_at, "isoformat") else str(s.created_at),
    )


async def _get_user_role_names(tenant_id: uuid.UUID, user_id: uuid.UUID, db: TenantDB) -> set[str]:
    res = await db.execute(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.tenant_id == tenant_id, UserRole.user_id == user_id)
    )
    return set(res.scalars().all())


# ── Classes CRUD ──────────────────────────────────────────────────────────────

@router.post("/classes", response_model=ClassResponse, status_code=status.HTTP_201_CREATED)
async def create_class(
    body: CreateClassRequest,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("class:manage")),
) -> ClassResponse:
    """Create a new academic class or cohort."""
    clean_name = body.name.strip()
    existing = await db.execute(
        select(Class).where(Class.tenant_id == current_user.tenant_id, func.lower(Class.name) == clean_name.lower())
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "class_exists", "message": f"Class '{clean_name}' already exists."},
        )

    new_class = Class(
        tenant_id=current_user.tenant_id,
        name=clean_name,
        kind=body.kind.strip(),
        description=body.description,
    )
    db.add(new_class)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="class:create",
        target_type="class",
        target_id=str(new_class.id),
        details={"name": new_class.name, "kind": new_class.kind},
        request=request,
        db=db,
    )
    return _to_class_response(new_class)


@router.get("/classes", response_model=ClassListResponse)
async def list_classes(
    db: TenantDB,
    current_user: User = Depends(require("class:manage")),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> ClassListResponse:
    """
    List classes with caller-scope filtering.
    - Tenant admin: sees all classes in tenant.
    - Teacher: sees ONLY classes assigned to them.
    - Student: sees ONLY classes enrolled in.
    """
    roles = await _get_user_role_names(current_user.tenant_id, current_user.id, db)
    is_admin_user = current_user.is_admin or ("tenant_admin" in roles)

    query = select(Class).where(Class.tenant_id == current_user.tenant_id)

    if not is_admin_user:
        if "teacher" in roles:
            # Teacher assigned classes
            assigned_class_ids = select(TeacherAssignment.class_id).where(
                TeacherAssignment.tenant_id == current_user.tenant_id,
                TeacherAssignment.user_id == current_user.id,
            )
            query = query.where(Class.id.in_(assigned_class_ids))
        elif "student" in roles:
            # Student enrolled classes
            enrolled_class_ids = select(Enrollment.class_id).where(
                Enrollment.tenant_id == current_user.tenant_id,
                Enrollment.user_id == current_user.id,
            )
            query = query.where(Class.id.in_(enrolled_class_ids))
        else:
            return ClassListResponse(items=[], total=0)

    total_res = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_res.scalar_one()

    result = await db.execute(query.order_by(Class.name).offset(skip).limit(limit))
    items = result.scalars().all()
    return ClassListResponse(items=[_to_class_response(c) for c in items], total=total)


@router.get("/classes/{class_id}", response_model=ClassResponse)
async def get_class(
    class_id: uuid.UUID,
    db: TenantDB,
    current_user: User = Depends(require("class:manage", Scope(ScopeType.CLASS))),
) -> ClassResponse:
    """Get single class details. Gated by class scope."""
    res = await db.execute(
        select(Class).where(Class.tenant_id == current_user.tenant_id, Class.id == class_id)
    )
    c = res.scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)
    return _to_class_response(c)


@router.patch("/classes/{class_id}", response_model=ClassResponse)
async def update_class(
    class_id: uuid.UUID,
    body: UpdateClassRequest,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("class:manage", Scope(ScopeType.CLASS))),
) -> ClassResponse:
    """Update class details. Gated by class scope."""
    res = await db.execute(
        select(Class).where(Class.tenant_id == current_user.tenant_id, Class.id == class_id)
    )
    c = res.scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    if body.name is not None:
        clean_name = body.name.strip()
        dup = await db.execute(
            select(Class).where(
                Class.tenant_id == current_user.tenant_id,
                func.lower(Class.name) == clean_name.lower(),
                Class.id != class_id,
            )
        )
        if dup.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "class_exists", "message": f"Class '{clean_name}' already exists."},
            )
        c.name = clean_name

    if body.kind is not None:
        c.kind = body.kind.strip()

    if body.description is not None:
        c.description = body.description

    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="class:update",
        target_type="class",
        target_id=str(class_id),
        details={"name": c.name, "kind": c.kind},
        request=request,
        db=db,
    )
    return _to_class_response(c)


@router.delete("/classes/{class_id}", response_model=MessageResponse)
async def delete_class(
    class_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("class:manage", Scope(ScopeType.CLASS))),
) -> MessageResponse:
    """Delete a class. Gated by class scope."""
    res = await db.execute(
        select(Class).where(Class.tenant_id == current_user.tenant_id, Class.id == class_id)
    )
    c = res.scalar_one_or_none()
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    class_name = c.name
    await db.delete(c)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="class:delete",
        target_type="class",
        target_id=str(class_id),
        details={"name": class_name},
        request=request,
        db=db,
    )
    return MessageResponse(message="Class deleted successfully.")


# ── Subjects CRUD ─────────────────────────────────────────────────────────────

@router.post("/subjects", response_model=SubjectResponse, status_code=status.HTTP_201_CREATED)
async def create_subject(
    body: CreateSubjectRequest,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("subject:manage")),
) -> SubjectResponse:
    """Create a new academic subject."""
    clean_name = body.name.strip()
    existing = await db.execute(
        select(Subject).where(
            Subject.tenant_id == current_user.tenant_id,
            func.lower(Subject.name) == clean_name.lower(),
        )
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "subject_exists", "message": f"Subject '{clean_name}' already exists."},
        )

    new_sub = Subject(
        tenant_id=current_user.tenant_id,
        name=clean_name,
        code=body.code.strip() if body.code else None,
        description=body.description,
    )
    db.add(new_sub)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="subject:create",
        target_type="subject",
        target_id=str(new_sub.id),
        details={"name": new_sub.name, "code": new_sub.code},
        request=request,
        db=db,
    )
    return _to_subject_response(new_sub)


@router.get("/subjects", response_model=SubjectListResponse)
async def list_subjects(
    db: TenantDB,
    current_user: User = Depends(require("subject:manage")),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> SubjectListResponse:
    """
    List subjects with caller-scope filtering.
    - Tenant admin: sees all subjects.
    - Teacher: sees subjects assigned to them.
    """
    roles = await _get_user_role_names(current_user.tenant_id, current_user.id, db)
    is_admin_user = current_user.is_admin or ("tenant_admin" in roles)

    query = select(Subject).where(Subject.tenant_id == current_user.tenant_id)

    if not is_admin_user and "teacher" in roles:
        assigned_sub_ids = select(TeacherAssignment.subject_id).where(
            TeacherAssignment.tenant_id == current_user.tenant_id,
            TeacherAssignment.user_id == current_user.id,
        )
        query = query.where(Subject.id.in_(assigned_sub_ids))

    total_res = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_res.scalar_one()

    result = await db.execute(query.order_by(Subject.name).offset(skip).limit(limit))
    items = result.scalars().all()
    return SubjectListResponse(items=[_to_subject_response(s) for s in items], total=total)


@router.get("/subjects/{subject_id}", response_model=SubjectResponse)
async def get_subject(
    subject_id: uuid.UUID,
    db: TenantDB,
    current_user: User = Depends(require("subject:manage")),
) -> SubjectResponse:
    """Get single subject details. Enforces teacher scope."""
    res = await db.execute(
        select(Subject).where(Subject.tenant_id == current_user.tenant_id, Subject.id == subject_id)
    )
    s = res.scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    roles = await _get_user_role_names(current_user.tenant_id, current_user.id, db)
    if not (current_user.is_admin or "tenant_admin" in roles) and "teacher" in roles:
        # Check if teacher is assigned to this subject
        ta_check = await db.execute(
            select(TeacherAssignment.id).where(
                TeacherAssignment.tenant_id == current_user.tenant_id,
                TeacherAssignment.user_id == current_user.id,
                TeacherAssignment.subject_id == subject_id,
            )
        )
        if ta_check.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    return _to_subject_response(s)


@router.patch("/subjects/{subject_id}", response_model=SubjectResponse)
async def update_subject(
    subject_id: uuid.UUID,
    body: UpdateSubjectRequest,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("subject:manage")),
) -> SubjectResponse:
    """Update subject details."""
    res = await db.execute(
        select(Subject).where(Subject.tenant_id == current_user.tenant_id, Subject.id == subject_id)
    )
    s = res.scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    roles = await _get_user_role_names(current_user.tenant_id, current_user.id, db)
    if not (current_user.is_admin or "tenant_admin" in roles) and "teacher" in roles:
        ta_check = await db.execute(
            select(TeacherAssignment.id).where(
                TeacherAssignment.tenant_id == current_user.tenant_id,
                TeacherAssignment.user_id == current_user.id,
                TeacherAssignment.subject_id == subject_id,
            )
        )
        if ta_check.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    if body.name is not None:
        clean_name = body.name.strip()
        dup = await db.execute(
            select(Subject).where(
                Subject.tenant_id == current_user.tenant_id,
                func.lower(Subject.name) == clean_name.lower(),
                Subject.id != subject_id,
            )
        )
        if dup.scalar_one_or_none() is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "subject_exists", "message": f"Subject '{clean_name}' already exists."},
            )
        s.name = clean_name

    if body.code is not None:
        s.code = body.code.strip() if body.code else None

    if body.description is not None:
        s.description = body.description

    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="subject:update",
        target_type="subject",
        target_id=str(subject_id),
        details={"name": s.name, "code": s.code},
        request=request,
        db=db,
    )
    return _to_subject_response(s)


@router.delete("/subjects/{subject_id}", response_model=MessageResponse)
async def delete_subject(
    subject_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("subject:manage")),
) -> MessageResponse:
    """Delete a subject."""
    res = await db.execute(
        select(Subject).where(Subject.tenant_id == current_user.tenant_id, Subject.id == subject_id)
    )
    s = res.scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    roles = await _get_user_role_names(current_user.tenant_id, current_user.id, db)
    if not (current_user.is_admin or "tenant_admin" in roles) and "teacher" in roles:
        ta_check = await db.execute(
            select(TeacherAssignment.id).where(
                TeacherAssignment.tenant_id == current_user.tenant_id,
                TeacherAssignment.user_id == current_user.id,
                TeacherAssignment.subject_id == subject_id,
            )
        )
        if ta_check.scalar_one_or_none() is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    sub_name = s.name
    await db.delete(s)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="subject:delete",
        target_type="subject",
        target_id=str(subject_id),
        details={"name": sub_name},
        request=request,
        db=db,
    )
    return MessageResponse(message="Subject deleted successfully.")


# ── Class-Subject Links ───────────────────────────────────────────────────────

@router.post("/classes/{class_id}/subjects/{subject_id}", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def link_class_subject(
    class_id: uuid.UUID,
    subject_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("class:manage", Scope(ScopeType.CLASS))),
) -> MessageResponse:
    """Link a subject to a class."""
    # Verify subject exists in tenant
    sub_res = await db.execute(
        select(Subject.id).where(Subject.tenant_id == current_user.tenant_id, Subject.id == subject_id)
    )
    if sub_res.scalar_one_or_none() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    existing = await db.execute(
        select(ClassSubject).where(
            ClassSubject.tenant_id == current_user.tenant_id,
            ClassSubject.class_id == class_id,
            ClassSubject.subject_id == subject_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return MessageResponse(message="Subject is already linked to class.")

    link = ClassSubject(
        tenant_id=current_user.tenant_id,
        class_id=class_id,
        subject_id=subject_id,
    )
    db.add(link)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="class_subject:link",
        target_type="class_subject",
        target_id=f"{class_id}:{subject_id}",
        details={"class_id": str(class_id), "subject_id": str(subject_id)},
        request=request,
        db=db,
    )
    return MessageResponse(message="Subject linked to class successfully.")


@router.delete("/classes/{class_id}/subjects/{subject_id}", response_model=MessageResponse)
async def unlink_class_subject(
    class_id: uuid.UUID,
    subject_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("class:manage", Scope(ScopeType.CLASS))),
) -> MessageResponse:
    """Unlink a subject from a class."""
    res = await db.execute(
        select(ClassSubject).where(
            ClassSubject.tenant_id == current_user.tenant_id,
            ClassSubject.class_id == class_id,
            ClassSubject.subject_id == subject_id,
        )
    )
    link = res.scalar_one_or_none()
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    await db.delete(link)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="class_subject:unlink",
        target_type="class_subject",
        target_id=f"{class_id}:{subject_id}",
        details={"class_id": str(class_id), "subject_id": str(subject_id)},
        request=request,
        db=db,
    )
    return MessageResponse(message="Subject unlinked from class successfully.")


@router.get("/classes/{class_id}/subjects", response_model=list[SubjectResponse])
async def list_class_subjects(
    class_id: uuid.UUID,
    db: TenantDB,
    current_user: User = Depends(require("class:manage", Scope(ScopeType.CLASS))),
) -> list[SubjectResponse]:
    """List all subjects offered in a specific class."""
    stmt = (
        select(Subject)
        .join(
            ClassSubject,
            (ClassSubject.tenant_id == Subject.tenant_id) & (ClassSubject.subject_id == Subject.id),
        )
        .where(ClassSubject.tenant_id == current_user.tenant_id, ClassSubject.class_id == class_id)
        .order_by(Subject.name)
    )
    res = await db.execute(stmt)
    subjects = res.scalars().all()
    return [_to_subject_response(s) for s in subjects]


# ── Enrollments ───────────────────────────────────────────────────────────────

@router.post("/classes/{class_id}/students", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def enroll_student(
    class_id: uuid.UUID,
    body: EnrollStudentRequest,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("enrollment:manage", Scope(ScopeType.CLASS))),
) -> MessageResponse:
    """Enroll a student in a class."""
    # Verify user exists in tenant
    user_res = await db.execute(
        select(User).where(
            User.tenant_id == current_user.tenant_id,
            User.id == body.user_id,
            User.status == "active",
        )
    )
    student = user_res.scalar_one_or_none()
    if student is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "Student not found in this organization."},
        )

    # Check existing enrollment
    existing = await db.execute(
        select(Enrollment).where(
            Enrollment.tenant_id == current_user.tenant_id,
            Enrollment.class_id == class_id,
            Enrollment.user_id == body.user_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return MessageResponse(message="Student is already enrolled in this class.")

    enr = Enrollment(
        tenant_id=current_user.tenant_id,
        class_id=class_id,
        user_id=body.user_id,
    )
    db.add(enr)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="enrollment:create",
        target_type="enrollment",
        target_id=str(enr.id),
        details={"class_id": str(class_id), "user_id": str(body.user_id), "email": student.email},
        request=request,
        db=db,
    )
    return MessageResponse(message="Student enrolled successfully.")


@router.delete("/classes/{class_id}/students/{user_id}", response_model=MessageResponse)
async def unenroll_student(
    class_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("enrollment:manage", Scope(ScopeType.CLASS))),
) -> MessageResponse:
    """Unenroll a student from a class."""
    res = await db.execute(
        select(Enrollment).where(
            Enrollment.tenant_id == current_user.tenant_id,
            Enrollment.class_id == class_id,
            Enrollment.user_id == user_id,
        )
    )
    enr = res.scalar_one_or_none()
    if enr is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    await db.delete(enr)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="enrollment:delete",
        target_type="enrollment",
        target_id=str(enr.id),
        details={"class_id": str(class_id), "user_id": str(user_id)},
        request=request,
        db=db,
    )
    return MessageResponse(message="Student unenrolled successfully.")


@router.get("/classes/{class_id}/students", response_model=RosterResponse)
async def get_class_roster(
    class_id: uuid.UUID,
    db: TenantDB,
    current_user: User = Depends(require("enrollment:manage", Scope(ScopeType.CLASS))),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> RosterResponse:
    """Get roster of students enrolled in a class."""
    stmt = (
        select(Enrollment.id, Enrollment.user_id, User.email, User.full_name, Enrollment.enrolled_at)
        .join(User, (User.tenant_id == Enrollment.tenant_id) & (User.id == Enrollment.user_id))
        .where(Enrollment.tenant_id == current_user.tenant_id, Enrollment.class_id == class_id)
        .order_by(User.full_name)
    )

    total_res = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total = total_res.scalar_one()

    res = await db.execute(stmt.offset(skip).limit(limit))
    rows = res.all()

    items = [
        RosterStudentResponse(
            id=str(r[0]),
            user_id=str(r[1]),
            email=r[2],
            full_name=r[3],
            enrolled_at=r[4].isoformat() if hasattr(r[4], "isoformat") else str(r[4]),
        )
        for r in rows
    ]
    return RosterResponse(items=items, total=total)


@router.post("/classes/{class_id}/students/import", response_model=EnrollmentImportResponse)
async def bulk_import_enrollments(
    class_id: uuid.UUID,
    file: UploadFile = File(...),
    request: Request = None,
    db: TenantDB = None,
    current_user: User = Depends(require("enrollment:manage", Scope(ScopeType.CLASS))),
) -> EnrollmentImportResponse:
    """
    Bulk import student enrollments via CSV.
    - Limits file size to 2MB.
    - Limits row count to 500 rows.
    - Validates email and member status per row.
    - Returns detailed per-row status and errors.
    """
    MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB
    MAX_ROWS = 500

    content = await file.read()
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "file_too_large", "message": "CSV file exceeds maximum limit of 2MB."},
        )

    try:
        text_content = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_encoding", "message": "CSV must be UTF-8 encoded."},
        )

    reader = csv.DictReader(io.StringIO(text_content))
    if not reader.fieldnames or "email" not in [f.strip().lower() for f in reader.fieldnames]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "missing_header", "message": "CSV must contain an 'email' column."},
        )

    # Normalize header mapping
    email_header = next(h for h in reader.fieldnames if h.strip().lower() == "email")

    errors: list[RowErrorResponse] = []
    succeeded = 0
    row_count = 0

    for idx, row in enumerate(reader, start=2):
        row_count += 1
        if row_count > MAX_ROWS:
            errors.append(
                RowErrorResponse(row=idx, email="", error=f"Exceeded maximum limit of {MAX_ROWS} rows.")
            )
            break

        email = (row.get(email_header) or "").strip().lower()
        if not email or "@" not in email:
            errors.append(RowErrorResponse(row=idx, email=email, error="Invalid or missing email address."))
            continue

        # Look up student in tenant
        user_res = await db.execute(
            select(User).where(User.tenant_id == current_user.tenant_id, User.email == email)
        )
        student = user_res.scalar_one_or_none()
        if not student:
            errors.append(RowErrorResponse(row=idx, email=email, error="User does not exist in this organization."))
            continue

        if student.status != "active":
            errors.append(RowErrorResponse(row=idx, email=email, error=f"User is {student.status}."))
            continue

        # Check existing enrollment
        existing = await db.execute(
            select(Enrollment.id).where(
                Enrollment.tenant_id == current_user.tenant_id,
                Enrollment.class_id == class_id,
                Enrollment.user_id == student.id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            # Already enrolled is counted as succeeded or skipped
            succeeded += 1
            continue

        enr = Enrollment(
            tenant_id=current_user.tenant_id,
            class_id=class_id,
            user_id=student.id,
        )
        db.add(enr)
        succeeded += 1

    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="enrollment:bulk_import",
        target_type="class",
        target_id=str(class_id),
        details={"total_rows": row_count, "succeeded": succeeded, "failed": len(errors)},
        request=request,
        db=db,
    )

    return EnrollmentImportResponse(
        total_rows=row_count,
        succeeded=succeeded,
        failed=len(errors),
        errors=errors,
    )


# ── Teacher Assignments ───────────────────────────────────────────────────────

@router.post("/classes/{class_id}/subjects/{subject_id}/teachers", response_model=MessageResponse, status_code=status.HTTP_201_CREATED)
async def assign_teacher(
    class_id: uuid.UUID,
    subject_id: uuid.UUID,
    body: AssignTeacherRequest,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("assignment:manage", Scope(ScopeType.CLASS_SUBJECT))),
) -> MessageResponse:
    """Assign a teacher to teach a subject in a class."""
    # 1. Verify link exists in class_subjects
    cs_check = await db.execute(
        select(ClassSubject).where(
            ClassSubject.tenant_id == current_user.tenant_id,
            ClassSubject.class_id == class_id,
            ClassSubject.subject_id == subject_id,
        )
    )
    if cs_check.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "class_subject_not_found", "message": "Subject is not linked to this class."},
        )

    # 2. Verify target user exists and is active in tenant
    user_res = await db.execute(
        select(User).where(
            User.tenant_id == current_user.tenant_id,
            User.id == body.user_id,
            User.status == "active",
        )
    )
    teacher = user_res.scalar_one_or_none()
    if teacher is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "user_not_found", "message": "Teacher user not found."},
        )

    # 3. Check existing assignment
    existing = await db.execute(
        select(TeacherAssignment).where(
            TeacherAssignment.tenant_id == current_user.tenant_id,
            TeacherAssignment.class_id == class_id,
            TeacherAssignment.subject_id == subject_id,
            TeacherAssignment.user_id == body.user_id,
        )
    )
    if existing.scalar_one_or_none() is not None:
        return MessageResponse(message="Teacher is already assigned to this class and subject.")

    ta = TeacherAssignment(
        tenant_id=current_user.tenant_id,
        class_id=class_id,
        subject_id=subject_id,
        user_id=body.user_id,
    )
    db.add(ta)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="assignment:create",
        target_type="teacher_assignment",
        target_id=str(ta.id),
        details={
            "class_id": str(class_id),
            "subject_id": str(subject_id),
            "user_id": str(body.user_id),
            "teacher_email": teacher.email,
        },
        request=request,
        db=db,
    )
    return MessageResponse(message="Teacher assigned successfully.")


@router.delete("/classes/{class_id}/subjects/{subject_id}/teachers/{user_id}", response_model=MessageResponse)
async def remove_teacher_assignment(
    class_id: uuid.UUID,
    subject_id: uuid.UUID,
    user_id: uuid.UUID,
    request: Request,
    db: TenantDB,
    current_user: User = Depends(require("assignment:manage", Scope(ScopeType.CLASS_SUBJECT))),
) -> MessageResponse:
    """Remove a teacher assignment from a class-subject."""
    res = await db.execute(
        select(TeacherAssignment).where(
            TeacherAssignment.tenant_id == current_user.tenant_id,
            TeacherAssignment.class_id == class_id,
            TeacherAssignment.subject_id == subject_id,
            TeacherAssignment.user_id == user_id,
        )
    )
    ta = res.scalar_one_or_none()
    if ta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=GENERIC_NOT_FOUND)

    await db.delete(ta)
    await db.flush()

    await record_audit_event(
        tenant_id=current_user.tenant_id,
        actor_id=current_user.id,
        action="assignment:delete",
        target_type="teacher_assignment",
        target_id=str(ta.id),
        details={"class_id": str(class_id), "subject_id": str(subject_id), "user_id": str(user_id)},
        request=request,
        db=db,
    )
    return MessageResponse(message="Teacher assignment removed successfully.")


@router.get("/classes/{class_id}/subjects/{subject_id}/teachers", response_model=list[TeacherAssignmentResponse])
async def list_assigned_teachers(
    class_id: uuid.UUID,
    subject_id: uuid.UUID,
    db: TenantDB,
    current_user: User = Depends(require("assignment:manage", Scope(ScopeType.CLASS_SUBJECT))),
) -> list[TeacherAssignmentResponse]:
    """List teachers assigned to a specific class-subject."""
    stmt = (
        select(TeacherAssignment, User.full_name, User.email, Class.name, Subject.name)
        .join(User, (User.tenant_id == TeacherAssignment.tenant_id) & (User.id == TeacherAssignment.user_id))
        .join(Class, (Class.tenant_id == TeacherAssignment.tenant_id) & (Class.id == TeacherAssignment.class_id))
        .join(Subject, (Subject.tenant_id == TeacherAssignment.tenant_id) & (Subject.id == TeacherAssignment.subject_id))
        .where(
            TeacherAssignment.tenant_id == current_user.tenant_id,
            TeacherAssignment.class_id == class_id,
            TeacherAssignment.subject_id == subject_id,
        )
        .order_by(User.full_name)
    )
    res = await db.execute(stmt)
    rows = res.all()
    return [
        TeacherAssignmentResponse(
            id=str(ta.id),
            user_id=str(ta.user_id),
            teacher_name=full_name,
            teacher_email=email,
            class_id=str(ta.class_id),
            class_name=c_name,
            subject_id=str(ta.subject_id),
            subject_name=s_name,
            assigned_at=ta.assigned_at.isoformat() if hasattr(ta.assigned_at, "isoformat") else str(ta.assigned_at),
        )
        for ta, full_name, email, c_name, s_name in rows
    ]


@router.get("/teacher-assignments", response_model=TeacherAssignmentListResponse)
async def list_all_teacher_assignments(
    db: TenantDB,
    current_user: User = Depends(require("assignment:manage")),
    class_id: uuid.UUID | None = None,
    teacher_id: uuid.UUID | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> TeacherAssignmentListResponse:
    """List teacher assignments across the tenant (paginated and filterable)."""
    stmt = (
        select(TeacherAssignment, User.full_name, User.email, Class.name, Subject.name)
        .join(User, (User.tenant_id == TeacherAssignment.tenant_id) & (User.id == TeacherAssignment.user_id))
        .join(Class, (Class.tenant_id == TeacherAssignment.tenant_id) & (Class.id == TeacherAssignment.class_id))
        .join(Subject, (Subject.tenant_id == TeacherAssignment.tenant_id) & (Subject.id == TeacherAssignment.subject_id))
        .where(TeacherAssignment.tenant_id == current_user.tenant_id)
    )
    if class_id:
        stmt = stmt.where(TeacherAssignment.class_id == class_id)
    if teacher_id:
        stmt = stmt.where(TeacherAssignment.user_id == teacher_id)

    total_res = await db.execute(select(func.count()).select_from(stmt.subquery()))
    total = total_res.scalar_one()

    res = await db.execute(stmt.order_by(Class.name, Subject.name).offset(skip).limit(limit))
    rows = res.all()

    items = [
        TeacherAssignmentResponse(
            id=str(ta.id),
            user_id=str(ta.user_id),
            teacher_name=full_name,
            teacher_email=email,
            class_id=str(ta.class_id),
            class_name=c_name,
            subject_id=str(ta.subject_id),
            subject_name=s_name,
            assigned_at=ta.assigned_at.isoformat() if hasattr(ta.assigned_at, "isoformat") else str(ta.assigned_at),
        )
        for ta, full_name, email, c_name, s_name in rows
    ]
    return TeacherAssignmentListResponse(items=items, total=total)


# ── "My Classes" Endpoint ─────────────────────────────────────────────────────

@router.get("/me/classes", response_model=list[MyClassResponse])
async def get_my_classes(
    db: TenantDB,
    current_user: User = Depends(require("chat:use")),
) -> list[MyClassResponse]:
    """
    Returns the authenticated user's classes and subject pairings.
    - Teacher: assigned classes and assigned subjects.
    - Student: enrolled classes and subjects offered in those classes.
    - Admin: all classes and their subjects.
    """
    roles = await _get_user_role_names(current_user.tenant_id, current_user.id, db)
    is_admin_user = current_user.is_admin or ("tenant_admin" in roles)

    if is_admin_user:
        # All classes and their subjects
        classes_res = await db.execute(
            select(Class).where(Class.tenant_id == current_user.tenant_id).order_by(Class.name)
        )
        classes = classes_res.scalars().all()
        result: list[MyClassResponse] = []
        for c in classes:
            subs_res = await db.execute(
                select(Subject)
                .join(ClassSubject, (ClassSubject.tenant_id == Subject.tenant_id) & (ClassSubject.subject_id == Subject.id))
                .where(ClassSubject.tenant_id == current_user.tenant_id, ClassSubject.class_id == c.id)
                .order_by(Subject.name)
            )
            subs = subs_res.scalars().all()
            result.append(
                MyClassResponse(
                    id=str(c.id),
                    name=c.name,
                    kind=c.kind,
                    subjects=[MySubjectResponse(id=str(s.id), name=s.name, code=s.code) for s in subs],
                )
            )
        return result

    if "teacher" in roles:
        # Classes and subjects assigned to this teacher
        ta_stmt = (
            select(Class, Subject)
            .join(TeacherAssignment, (TeacherAssignment.tenant_id == Class.tenant_id) & (TeacherAssignment.class_id == Class.id))
            .join(Subject, (Subject.tenant_id == TeacherAssignment.tenant_id) & (Subject.id == TeacherAssignment.subject_id))
            .where(
                TeacherAssignment.tenant_id == current_user.tenant_id,
                TeacherAssignment.user_id == current_user.id,
            )
            .order_by(Class.name, Subject.name)
        )
        ta_res = await db.execute(ta_stmt)
        class_map: dict[uuid.UUID, tuple[Class, list[Subject]]] = {}
        for c, s in ta_res.all():
            if c.id not in class_map:
                class_map[c.id] = (c, [])
            class_map[c.id][1].append(s)

        return [
            MyClassResponse(
                id=str(c.id),
                name=c.name,
                kind=c.kind,
                subjects=[MySubjectResponse(id=str(s.id), name=s.name, code=s.code) for s in subs],
            )
            for c, subs in class_map.values()
        ]

    if "student" in roles:
        # Classes enrolled in by this student, plus all subjects in each class
        enr_stmt = (
            select(Class)
            .join(Enrollment, (Enrollment.tenant_id == Class.tenant_id) & (Enrollment.class_id == Class.id))
            .where(
                Enrollment.tenant_id == current_user.tenant_id,
                Enrollment.user_id == current_user.id,
            )
            .order_by(Class.name)
        )
        enr_res = await db.execute(enr_stmt)
        classes = enr_res.scalars().all()
        result: list[MyClassResponse] = []
        for c in classes:
            subs_res = await db.execute(
                select(Subject)
                .join(ClassSubject, (ClassSubject.tenant_id == Subject.tenant_id) & (ClassSubject.subject_id == Subject.id))
                .where(ClassSubject.tenant_id == current_user.tenant_id, ClassSubject.class_id == c.id)
                .order_by(Subject.name)
            )
            subs = subs_res.scalars().all()
            result.append(
                MyClassResponse(
                    id=str(c.id),
                    name=c.name,
                    kind=c.kind,
                    subjects=[MySubjectResponse(id=str(s.id), name=s.name, code=s.code) for s in subs],
                )
            )
        return result

    return []

