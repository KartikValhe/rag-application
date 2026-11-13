"""
Timetable status lifecycle: draft -> published -> archived.

Regression for the bug where publishing a draft turned the previous published timetable
back into a draft, so an old version showed up in the editor and could be republished.
"""

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.db.models.academic import Class, ClassSubject, Enrollment, Subject, TeacherAssignment
from app.db.models.audit import AuditLog
from app.db.models.rbac import Role, UserRole
from app.db.models.tenant import Tenant
from app.db.models.timetable import Timetable, TimetableEntry
from app.db.models.user import User
from app.services.timetable_lifecycle import ARCHIVE_RETENTION

pytestmark = pytest.mark.asyncio


def _headers(token: str, slug: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": slug}


async def _user_with_role(tenant: Tenant, raw_db: AsyncSession, role: str) -> tuple[User, dict[str, str]]:
    u = User(
        tenant_id=tenant.id,
        email=f"{role}-{uuid.uuid4().hex}@example.com",
        full_name=f"Lifecycle {role}",
        is_admin=False,
        status="active",
    )
    raw_db.add(u)
    await raw_db.flush()
    role_id = (
        await raw_db.execute(select(Role.id).where(Role.tenant_id == tenant.id, Role.name == role))
    ).scalar_one()
    raw_db.add(UserRole(tenant_id=tenant.id, user_id=u.id, role_id=role_id))
    await raw_db.commit()
    token = create_access_token(tenant_id=str(tenant.id), user_id=str(u.id), is_admin=False)
    return u, _headers(token, tenant.slug)


async def _setup(tenant: Tenant, raw_db: AsyncSession) -> dict[str, Any]:
    """A class with one subject and an assigned teacher, plus an enrolled student."""
    c = Class(tenant_id=tenant.id, name=f"LC-{uuid.uuid4().hex[:6]}")
    sub = Subject(tenant_id=tenant.id, name=f"LCSub-{uuid.uuid4().hex[:6]}")
    raw_db.add_all([c, sub])
    await raw_db.flush()
    raw_db.add(ClassSubject(tenant_id=tenant.id, class_id=c.id, subject_id=sub.id))
    await raw_db.commit()

    teacher, teacher_hdr = await _user_with_role(tenant, raw_db, "teacher")
    raw_db.add(TeacherAssignment(tenant_id=tenant.id, user_id=teacher.id, class_id=c.id, subject_id=sub.id))
    student, student_hdr = await _user_with_role(tenant, raw_db, "student")
    raw_db.add(Enrollment(tenant_id=tenant.id, user_id=student.id, class_id=c.id))
    await raw_db.commit()
    return {
        "class": c,
        "subject": sub,
        "teacher": teacher,
        "hdr": teacher_hdr,
        "student_hdr": student_hdr,
        "base": f"/api/v1/classes/{c.id}/timetables",
    }


async def _add_lecture(client: AsyncClient, s: dict[str, Any], day: int, room: str) -> None:
    res = await client.post(
        f"{s['base']}/draft/entries",
        headers=s["hdr"],
        json={
            "day_of_week": day,
            "start_time": "09:00",
            "end_time": "10:00",
            "subject_id": str(s["subject"].id),
            "room": room,
        },
    )
    assert res.status_code == 200, res.text


async def _publish(client: AsyncClient, s: dict[str, Any]) -> dict[str, Any]:
    res = await client.post(f"{s['base']}/publish", headers=s["hdr"], json={"confirm_conflicts": True})
    assert res.status_code == 200, res.text
    body: dict[str, Any] = res.json()
    return body


async def _clone(client: AsyncClient, s: dict[str, Any]) -> dict[str, Any]:
    res = await client.post(f"{s['base']}/draft/clone", headers=s["hdr"])
    assert res.status_code == 201, res.text
    body: dict[str, Any] = res.json()
    return body


async def _statuses(raw_db: AsyncSession, tenant: Tenant, class_id: uuid.UUID) -> list[str]:
    res = await raw_db.execute(
        select(Timetable.status).where(Timetable.tenant_id == tenant.id, Timetable.class_id == class_id)
    )
    return sorted(res.scalars().all())


async def test_publish_twice_leaves_one_published_one_archived_no_draft(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)
    await _add_lecture(client, s, 0, "R1")
    first = await _publish(client, s)
    assert first["version"] == 1

    await _clone(client, s)
    await _add_lecture(client, s, 1, "R2")
    second = await _publish(client, s)
    assert second["version"] == 2
    assert second["status"] == "published"

    assert await _statuses(raw_db, tenant_a, s["class"].id) == ["archived", "published"]

    # The old version is history, never the draft.
    draft = (await client.get(f"{s['base']}/draft", headers=s["hdr"])).json()
    assert draft == {"state": "no_draft", "draft": None, "published_version": 2}

    listed = (await client.get(s["base"], headers=s["hdr"])).json()
    by_status = {t["status"]: t for t in listed}
    assert by_status["archived"]["id"] == first["id"]
    assert by_status["archived"]["version"] == 1


async def test_get_draft_is_empty_state_after_publish_and_has_no_side_effects(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)

    # Nothing yet: empty state, and nothing is created by reading.
    res = await client.get(f"{s['base']}/draft", headers=s["hdr"])
    assert res.status_code == 200
    assert res.json() == {"state": "no_draft", "draft": None, "published_version": None}
    assert await _statuses(raw_db, tenant_a, s["class"].id) == []

    await _add_lecture(client, s, 0, "R1")
    res = await client.get(f"{s['base']}/draft", headers=s["hdr"])
    assert res.json()["state"] == "draft"
    assert len(res.json()["draft"]["entries"]) == 1

    await _publish(client, s)
    for _ in range(2):  # repeated reads stay empty
        res = await client.get(f"{s['base']}/draft", headers=s["hdr"])
        assert res.status_code == 200
        assert res.json() == {"state": "no_draft", "draft": None, "published_version": 1}
    assert await _statuses(raw_db, tenant_a, s["class"].id) == ["published"]


async def test_adding_lecture_after_publish_requires_clone(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    """A blank draft must not be created next to a published one (it would drop every lecture)."""
    s = await _setup(tenant_a, raw_db)
    await _add_lecture(client, s, 0, "R1")
    await _publish(client, s)

    res = await client.post(
        f"{s['base']}/draft/entries",
        headers=s["hdr"],
        json={"day_of_week": 1, "start_time": "09:00", "end_time": "10:00", "subject_id": str(s["subject"].id)},
    )
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "no_draft"
    assert await _statuses(raw_db, tenant_a, s["class"].id) == ["published"]


async def test_clone_creates_draft_equal_to_published(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)
    await _add_lecture(client, s, 0, "R1")
    await _add_lecture(client, s, 3, "R4")
    published = await _publish(client, s)

    draft = await _clone(client, s)
    assert draft["status"] == "draft"
    assert draft["id"] != published["id"]

    def lectures(tt: dict[str, Any]) -> list[tuple[Any, ...]]:
        return sorted(
            (e["day_of_week"], e["start_time"], e["end_time"], e["subject_id"], e["teacher_id"], e["room"], e["note"])
            for e in tt["entries"]
        )

    assert lectures(draft) == lectures(published)
    assert (draft["academic_year"], draft["term"]) == (published["academic_year"], published["term"])

    got = (await client.get(f"{s['base']}/draft", headers=s["hdr"])).json()
    assert got["state"] == "draft" and got["draft"]["id"] == draft["id"]

    # A second clone is refused with the stable code.
    again = await client.post(f"{s['base']}/draft/clone", headers=s["hdr"])
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "timetable_draft_exists"


async def test_clone_without_published_is_404(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)
    res = await client.post(f"{s['base']}/draft/clone", headers=s["hdr"])
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "not_published"


async def test_clone_is_scoped_to_assigned_classes(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)
    await _add_lecture(client, s, 0, "R1")
    await _publish(client, s)

    _, other_teacher_hdr = await _user_with_role(tenant_a, raw_db, "teacher")
    res = await client.post(f"{s['base']}/draft/clone", headers=other_teacher_hdr)
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "not_found"

    res = await client.post(f"{s['base']}/draft/clone", headers=s["student_hdr"])
    assert res.status_code == 403
    assert await _statuses(raw_db, tenant_a, s["class"].id) == ["published"]


async def test_unpublish_with_existing_draft_returns_409(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)
    await _add_lecture(client, s, 0, "R1")
    await _publish(client, s)
    await _clone(client, s)

    res = await client.post(f"{s['base']}/unpublish", headers=s["hdr"])
    assert res.status_code == 409
    assert res.json()["detail"]["code"] == "timetable_draft_exists"
    assert await _statuses(raw_db, tenant_a, s["class"].id) == ["draft", "published"]

    # After deleting the draft, unpublish works and the published one becomes the draft.
    assert (await client.delete(f"{s['base']}/draft", headers=s["hdr"])).status_code == 204
    res = await client.post(f"{s['base']}/unpublish", headers=s["hdr"])
    assert res.status_code == 200
    assert res.json()["status"] == "draft"
    assert await _statuses(raw_db, tenant_a, s["class"].id) == ["draft"]


async def test_unpublish_never_restores_archived(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)
    await _add_lecture(client, s, 0, "R1")
    await _publish(client, s)
    await _clone(client, s)
    await _publish(client, s)

    res = await client.post(f"{s['base']}/unpublish", headers=s["hdr"])
    assert res.status_code == 200
    # The newest version becomes the draft; the archived one stays archived and nothing is published.
    assert res.json()["version"] == 2
    assert await _statuses(raw_db, tenant_a, s["class"].id) == ["archived", "draft"]

    me = (await client.get("/api/v1/me/timetable", headers=s["student_hdr"])).json()
    assert me["classes"] == []


async def test_student_sees_only_newest_published(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)
    await _add_lecture(client, s, 0, "OLD")
    await _publish(client, s)
    await _clone(client, s)
    draft = (await client.get(f"{s['base']}/draft", headers=s["hdr"])).json()["draft"]
    # Replace the lecture so old and new versions are distinguishable.
    entry_id = draft["entries"][0]["id"]
    res = await client.put(
        f"{s['base']}/draft/entries/{entry_id}",
        headers=s["hdr"],
        json={
            "day_of_week": 2, "start_time": "11:00", "end_time": "12:00",
            "subject_id": str(s["subject"].id), "room": "NEW",
        },
    )
    assert res.status_code == 200
    newest = await _publish(client, s)

    # An extra draft must stay invisible to students too.
    await _clone(client, s)

    me = (await client.get("/api/v1/me/timetable", headers=s["student_hdr"])).json()
    assert [t["id"] for t in me["classes"]] == [newest["id"]]
    assert me["classes"][0]["status"] == "published"
    assert [e["room"] for e in me["classes"][0]["entries"]] == ["NEW"]


async def test_archive_retention_keeps_last_five(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)
    await _add_lecture(client, s, 0, "R1")
    await _publish(client, s)
    total_publishes = ARCHIVE_RETENTION + 3
    for _ in range(total_publishes - 1):
        await _clone(client, s)
        await _publish(client, s)

    rows = (
        await raw_db.execute(
            select(Timetable.status, Timetable.version).where(
                Timetable.tenant_id == tenant_a.id, Timetable.class_id == s["class"].id
            )
        )
    ).all()
    archived_versions = sorted(v for st, v in rows if st == "archived")
    published_versions = [v for st, v in rows if st == "published"]
    assert published_versions == [total_publishes]
    # The newest five archived versions survive; older ones are deleted with their entries.
    assert archived_versions == list(range(total_publishes - ARCHIVE_RETENTION, total_publishes))

    kept_ids = {
        i
        for i in (
            await raw_db.execute(
                select(Timetable.id).where(Timetable.tenant_id == tenant_a.id, Timetable.class_id == s["class"].id)
            )
        ).scalars()
    }
    entry_tt_ids = set(
        (
            await raw_db.execute(
                select(TimetableEntry.timetable_id).where(
                    TimetableEntry.tenant_id == tenant_a.id, TimetableEntry.class_id == s["class"].id
                )
            )
        ).scalars()
    )
    assert entry_tt_ids <= kept_ids

    actions = set(
        (
            await raw_db.execute(
                select(AuditLog.action).where(
                    AuditLog.tenant_id == tenant_a.id, AuditLog.target_id == str(s["class"].id)
                )
            )
        ).scalars()
    )
    assert "timetable:prune_archived" in actions


async def test_archived_timetables_are_read_only(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)
    await _add_lecture(client, s, 0, "R1")
    first = await _publish(client, s)
    await _clone(client, s)
    await _publish(client, s)
    # Remove the remaining draft path: there is none, and the archived entry is not editable.
    archived_entry_id = first["entries"][0]["id"]
    payload = {"day_of_week": 4, "start_time": "09:00", "end_time": "10:00", "subject_id": str(s["subject"].id)}
    res = await client.put(f"{s['base']}/draft/entries/{archived_entry_id}", headers=s["hdr"], json=payload)
    assert res.status_code == 404
    res = await client.delete(f"{s['base']}/draft/entries/{archived_entry_id}", headers=s["hdr"])
    assert res.status_code == 404
    # Publishing has nothing to publish: the archived one is never treated as a draft.
    res = await client.post(f"{s['base']}/publish", headers=s["hdr"], json={"confirm_conflicts": True})
    assert res.status_code == 404
    assert res.json()["detail"]["code"] == "no_draft"
    assert await _statuses(raw_db, tenant_a, s["class"].id) == ["archived", "published"]


async def test_lifecycle_actions_are_audited(
    client: AsyncClient, tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    s = await _setup(tenant_a, raw_db)
    await _add_lecture(client, s, 0, "R1")
    first = await _publish(client, s)
    clone = await _clone(client, s)
    second = await _publish(client, s)
    await client.post(f"{s['base']}/unpublish", headers=s["hdr"])

    rows = (
        await raw_db.execute(
            select(AuditLog.action, AuditLog.target_id).where(AuditLog.tenant_id == tenant_a.id)
        )
    ).all()
    got = set(rows)
    assert ("timetable:publish", first["id"]) in got
    assert ("timetable:clone", clone["id"]) in got
    assert ("timetable:archive", first["id"]) in got
    assert ("timetable:publish", second["id"]) in got
    assert ("timetable:unpublish", second["id"]) in got


async def test_partial_unique_index_prevents_two_drafts_or_two_published(
    tenant_a: Tenant, raw_db: AsyncSession
) -> None:
    c = Class(tenant_id=tenant_a.id, name=f"UQ-{uuid.uuid4().hex[:6]}")
    raw_db.add(c)
    await raw_db.commit()
    tid, cid = tenant_a.id, c.id  # read before any rollback expires the objects

    for st in ("draft", "published"):
        raw_db.add(Timetable(tenant_id=tid, class_id=cid, status=st, version=1))
        await raw_db.commit()
        raw_db.add(Timetable(tenant_id=tid, class_id=cid, status=st, version=2))
        with pytest.raises(IntegrityError):
            await raw_db.commit()
        await raw_db.rollback()

    # Archived history is not limited by the index.
    raw_db.add_all([Timetable(tenant_id=tid, class_id=cid, status="archived", version=v) for v in (3, 4)])
    await raw_db.commit()

    # Unknown statuses are rejected by the check constraint.
    raw_db.add(Timetable(tenant_id=tid, class_id=cid, status="retired", version=9))
    with pytest.raises(IntegrityError):
        await raw_db.commit()
    await raw_db.rollback()
