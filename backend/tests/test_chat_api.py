"""
tests/test_chat_api.py
──────────────────────
The chat API, conversation part (step 6a-9a; the message parts join in 6a-9b): create, list, get and delete under
/api/v1/chat/conversations (plan 8.1, 8.2, 8.4).

Rules checked here:
  - every route is the caller's own data only; another user's conversation is the generic 404 on every route, with
    the same body as an id that does not exist (no admin or teacher reading, Q4);
  - create checks the class and subject against the caller's READ scope (pairs, Q3/Q8), generic 404 otherwise, and
    a 404 probe never uses up the hourly creation window (C15);
  - 20 conversations per user per hour (429 conversation_rate_limit_exceeded + Retry-After), Redis down outside
    development is 503 service_unavailable (fail closed);
  - chat switched off for the tenant: create is 503 chat_disabled, history (list, get, delete) still works;
  - delete is a hard delete that cascades, audited with ids and counts only;
  - no route holds a pooled connection while it waits (B100), and nothing is logged with content.
"""

import base64
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from httpx import AsyncClient
from sqlalchemy import delete, event, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client, slots
from app.core.authz import GENERIC_NOT_FOUND
from app.core.config import settings
from app.db.engine import engine as app_engine
from app.db.models.academic import Enrollment
from app.db.models.audit import AuditLog
from app.db.models.chat import Conversation, Message
from app.db.models.user import User
from app.services import chat_conversations
from tests.fixtures.academic_world import AcademicWorld, WorldTenant
from tests.fixtures.documents import auth

BASE = "/api/v1/chat/conversations"
NOT_FOUND = {"detail": GENERIC_NOT_FOUND}
RESPONSE_KEYS = {"id", "class", "subject", "title", "created_at", "last_message_at", "message_count"}


def _body(w: WorldTenant, class_name: str, subject_name: str | None = None) -> dict[str, Any]:
    return {"class_id": str(w.classes[class_name].id),
            "subject_id": str(w.subjects[subject_name].id) if subject_name else None}


async def _create(client: AsyncClient, w: WorldTenant, user: User, class_name: str, subject_name: str | None = None) -> Any:
    return await client.post(BASE, headers=auth(w, user), json=_body(w, class_name, subject_name))


async def _row(
    raw_db: AsyncSession, w: WorldTenant, user: User, class_name: str, *, subject_name: str | None = None,
    last_message_at: datetime | None = None, title: str | None = None, message_count: int = 0,
) -> Conversation:
    conv = Conversation(
        tenant_id=w.id, user_id=user.id, class_id=w.classes[class_name].id,
        subject_id=w.subjects[subject_name].id if subject_name else None, title=title, message_count=message_count,
    )
    if last_message_at is not None:
        conv.last_message_at = last_message_at
    raw_db.add(conv)
    await raw_db.commit()
    await raw_db.refresh(conv)
    return conv


async def _count_rows(raw_db: AsyncSession, w: WorldTenant, user: User) -> int:
    rows = await raw_db.execute(select(Conversation.id).where(Conversation.tenant_id == w.id, Conversation.user_id == user.id))
    return len(rows.all())


@pytest.fixture
def small_window(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(settings, "CHAT_CONVERSATIONS_PER_USER_PER_HOUR", 3)
    return 3


async def _disable_chat(raw_db: AsyncSession, w: WorldTenant) -> None:
    await raw_db.execute(
        text("INSERT INTO app.tenant_settings (tenant_id, chat_enabled) VALUES (:t, false) "
             "ON CONFLICT (tenant_id) DO UPDATE SET chat_enabled = false"),
        {"t": w.id},
    )
    await raw_db.commit()


# ── Create ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("who", "class_name", "subject_name"), [
    ("admin", "10-A", "Math"), ("t1", "10-A", "Math"), ("t1", "10-B", None), ("s1", "10-A", "Physics"),
    ("s1", "11-A", None), ("s2", "10-B", "English"),
])
async def test_create_returns_201_with_the_exact_shape_and_the_callers_row(
    who: str, class_name: str, subject_name: str | None, client: AsyncClient, academic_world: AcademicWorld,
    raw_db: AsyncSession,
) -> None:
    w = academic_world.a
    user = getattr(w, who)
    resp = await _create(client, w, user, class_name, subject_name)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert set(data) == RESPONSE_KEYS
    assert data["class"] == {"id": str(w.classes[class_name].id), "name": class_name}
    assert data["subject"] == ({"id": str(w.subjects[subject_name].id), "name": subject_name} if subject_name else None)
    assert data["title"] is None and data["message_count"] == 0
    assert data["created_at"] and data["last_message_at"]
    row = (await raw_db.execute(select(Conversation).where(Conversation.id == uuid.UUID(data["id"])))).scalar_one()
    assert (row.tenant_id, row.user_id, row.class_id) == (w.id, user.id, w.classes[class_name].id)
    assert row.subject_id == (w.subjects[subject_name].id if subject_name else None)


@pytest.mark.parametrize(("who", "class_name", "subject_name"), [
    ("s2", "10-A", None),          # not enrolled in 10-A (Q3: 404, not 403)
    ("s1", "10-B", "Math"),        # not enrolled in 10-B
    ("s1", "11-A", "Physics"),     # enrolled, but 11-A does not offer Physics
    ("t2", "10-A", "Math"),        # teacher out of scope
    ("t2", "10-A", None),          # teaches nothing in 10-A
    ("t1", "10-A", "English"),     # offered in 10-A, but not T1's pair (Q8: own pairs only)
    ("t1", "11-A", None),
    ("admin", "11-A", "Physics"),  # not offered: even an admin has no such pair
])
async def test_create_outside_the_read_scope_is_the_generic_404_and_inserts_nothing(
    who: str, class_name: str, subject_name: str | None, client: AsyncClient, academic_world: AcademicWorld,
    raw_db: AsyncSession,
) -> None:
    w = academic_world.a
    user = getattr(w, who)
    resp = await _create(client, w, user, class_name, subject_name)
    assert resp.status_code == 404 and resp.json() == NOT_FOUND, resp.text
    assert await _count_rows(raw_db, w, user) == 0


async def test_create_with_unknown_or_other_tenant_ids_is_the_same_404(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    bodies = [
        {"class_id": str(uuid.uuid4()), "subject_id": None},
        {"class_id": str(b.classes["10-A"].id), "subject_id": None},
        {"class_id": str(b.classes["10-A"].id), "subject_id": str(b.subjects["Math"].id)},
        {"class_id": str(a.classes["10-A"].id), "subject_id": str(b.subjects["Math"].id)},
        {"class_id": str(a.classes["10-A"].id), "subject_id": str(uuid.uuid4())},
    ]
    for body in bodies:
        resp = await client.post(BASE, headers=auth(a, a.admin), json=body)
        assert resp.status_code == 404 and resp.json() == NOT_FOUND, (body, resp.text)
    assert await _count_rows(raw_db, a, a.admin) == 0


@pytest.mark.parametrize("body", [
    {},
    {"subject_id": None},
    {"class_id": "not-a-uuid"},
    {"class_id": None},
    {"class_id": "CLASS", "subject_id": None},
    "EXTRA",  # replaced below by a valid body plus an unknown field
    "USER",   # a client-chosen user id is refused
    "TENANT",  # a client-chosen tenant id is refused
])
async def test_create_body_is_strict(body: Any, client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    valid = _body(w, "10-A", "Math")
    extra = {"EXTRA": {**valid, "title": "x"}, "USER": {**valid, "user_id": str(w.s2.id)},
             "TENANT": {**valid, "tenant_id": str(academic_world.b.id)}}
    if isinstance(body, str):
        body = extra[body]
    elif body.get("class_id") == "CLASS":
        body = {**body, "class_id": str(w.classes["10-A"].id), "subject_id": "nope"}
    resp = await client.post(BASE, headers=auth(w, w.s1), json=body)
    assert resp.status_code == 422, resp.text
    assert await _count_rows(raw_db, w, w.s1) == 0


def test_the_hourly_creation_limit_defaults_to_20() -> None:
    assert type(settings).model_fields["CHAT_CONVERSATIONS_PER_USER_PER_HOUR"].default == 20


async def test_creation_limit_per_user_per_hour_and_404_probes_use_nothing(
    client: AsyncClient, academic_world: AcademicWorld, small_window: int, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    for _ in range(small_window + 2):  # probes of foreign ids never use the window (C15)
        assert (await _create(client, w, w.s1, "10-B")).status_code == 404
    for _ in range(small_window):
        assert (await _create(client, w, w.s1, "10-A")).status_code == 201
    over = await _create(client, w, w.s1, "10-A")
    assert over.status_code == 429, over.text
    detail = over.json()["detail"]
    assert set(detail) == {"code", "message", "retry_after_seconds"}
    assert detail["code"] == "conversation_rate_limit_exceeded"
    assert detail["message"] == chat_conversations.ERROR_MESSAGES["conversation_rate_limit_exceeded"]
    assert isinstance(detail["retry_after_seconds"], int) and 1 <= detail["retry_after_seconds"] <= 3600
    assert int(over.headers["Retry-After"]) == max(1, detail["retry_after_seconds"])
    assert await _count_rows(raw_db, w, w.s1) == small_window
    # The window is the user's own: another user of the same tenant is not affected
    assert (await _create(client, w, w.s2, "10-B")).status_code == 201


async def test_redis_down_outside_development_is_503_and_inserts_nothing(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = academic_world.a

    async def broken() -> Any:
        raise ConnectionError("redis is down")

    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(redis_client, "get_redis", broken)
    resp = await _create(client, w, w.s1, "10-A")
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["code"] == "service_unavailable"
    assert await _count_rows(raw_db, w, w.s1) == 0


async def test_chat_disabled_refuses_create_but_keeps_history(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, small_window: int
) -> None:
    w = academic_world.a
    conv = await _row(raw_db, w, w.s1, "10-A")
    await _disable_chat(raw_db, w)
    for _ in range(small_window + 1):
        resp = await _create(client, w, w.s1, "10-A")
        assert resp.status_code == 503, resp.text
        assert resp.json() == {"detail": {"code": "chat_disabled",
                                          "message": chat_conversations.ERROR_MESSAGES["chat_disabled"]}}
    assert await _count_rows(raw_db, w, w.s1) == 1
    headers = auth(w, w.s1)
    assert (await client.get(BASE, headers=headers)).status_code == 200
    assert (await client.get(f"{BASE}/{conv.id}", headers=headers)).status_code == 200
    assert (await client.delete(f"{BASE}/{conv.id}", headers=headers)).status_code == 204
    # Another tenant is not affected
    b = academic_world.b
    assert (await _create(client, b, b.s1, "10-A")).status_code == 201


async def test_chat_enabled_again_allows_create_and_the_refusals_used_no_window(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, small_window: int
) -> None:
    w = academic_world.a
    await _disable_chat(raw_db, w)
    for _ in range(small_window + 1):
        assert (await _create(client, w, w.s1, "10-A")).status_code == 503
    await raw_db.execute(text("UPDATE app.tenant_settings SET chat_enabled = true WHERE tenant_id = :t"), {"t": w.id})
    await raw_db.commit()
    for _ in range(small_window):
        assert (await _create(client, w, w.s1, "10-A")).status_code == 201


# ── Get ───────────────────────────────────────────────────────────────────────


async def test_get_own_conversation(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    conv = await _row(raw_db, w, w.s1, "10-A", subject_name="Math", title="My first question", message_count=4)
    resp = await client.get(f"{BASE}/{conv.id}", headers=auth(w, w.s1))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data) == RESPONSE_KEYS
    assert data["id"] == str(conv.id)
    assert data["class"] == {"id": str(w.classes["10-A"].id), "name": "10-A"}
    assert data["subject"] == {"id": str(w.subjects["Math"].id), "name": "Math"}
    assert data["title"] == "My first question" and data["message_count"] == 4
    assert datetime.fromisoformat(data["last_message_at"]) == conv.last_message_at


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_another_users_conversation_is_the_generic_404_on_every_route(
    method: str, client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    conv = await _row(raw_db, a, a.s1, "10-A", subject_name="Math")
    callers = [(a, a.admin), (a, a.t1), (a, a.t2), (a, a.s2), (b, b.admin), (b, b.s1)]
    for w, user in callers:
        resp = await client.request(method, f"{BASE}/{conv.id}", headers=auth(w, user))
        assert resp.status_code == 404 and resp.json() == NOT_FOUND, (user.email, resp.text)
        unknown = await client.request(method, f"{BASE}/{uuid.uuid4()}", headers=auth(w, user))
        assert unknown.status_code == 404 and unknown.json() == resp.json()
    # Nobody else's call removed it
    assert (await raw_db.execute(select(Conversation.id).where(Conversation.id == conv.id))).scalar_one_or_none() == conv.id


async def test_owner_keeps_history_after_the_class_left_their_scope(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    """Decision (6a-9a): get, list and delete are owner-only and do not re-check the class scope, so a student who
    left a class can still read and delete their own chats. The message route (6a-9b) re-checks scope (plan 4.2)."""
    w = academic_world.a
    conv = await _row(raw_db, w, w.s1, "10-A")
    await raw_db.execute(
        delete(Enrollment).where(Enrollment.tenant_id == w.id, Enrollment.user_id == w.s1.id,
                                 Enrollment.class_id == w.classes["10-A"].id)
    )
    await raw_db.commit()
    headers = auth(w, w.s1)
    assert (await _create(client, w, w.s1, "10-A")).status_code == 404
    assert (await client.get(f"{BASE}/{conv.id}", headers=headers)).status_code == 200
    assert [i["id"] for i in (await client.get(BASE, headers=headers)).json()["items"]] == [str(conv.id)]
    assert (await client.delete(f"{BASE}/{conv.id}", headers=headers)).status_code == 204


async def test_a_removed_subject_shows_as_null(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    conv = await _row(raw_db, w, w.s1, "10-A", subject_name="Physics")
    await raw_db.execute(update(Conversation).where(Conversation.id == conv.id).values(subject_id=None))
    await raw_db.commit()
    data = (await client.get(f"{BASE}/{conv.id}", headers=auth(w, w.s1))).json()
    assert data["subject"] is None and data["class"]["name"] == "10-A"


# ── List ──────────────────────────────────────────────────────────────────────


async def test_list_is_own_only_newest_activity_first_and_pages_without_gaps(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    base = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    mine = [await _row(raw_db, w, w.s1, "10-A", last_message_at=base + timedelta(minutes=i)) for i in range(5)]
    # Three with the SAME time: the id decides, and paging must neither repeat nor skip one
    tied = [await _row(raw_db, w, w.s1, "11-A", last_message_at=base + timedelta(hours=1)) for _ in range(3)]
    await _row(raw_db, w, w.s2, "10-B")
    await _row(raw_db, academic_world.b, academic_world.b.s1, "10-A")

    expected = [str(c.id) for c in sorted(tied, key=lambda c: c.id, reverse=True)]
    expected += [str(c.id) for c in reversed(mine)]
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get(BASE, headers=auth(w, w.s1), params=params)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert set(data) == {"items", "next_cursor"}
        assert all(set(i) == RESPONSE_KEYS for i in data["items"])
        seen += [i["id"] for i in data["items"]]
        pages += 1
        cursor = data["next_cursor"]
        if cursor is None:
            break
        assert pages < 10
    assert seen == expected
    assert pages == 4


async def test_list_default_limit_and_class_filter(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    in_10a = [await _row(raw_db, w, w.s1, "10-A") for _ in range(22)]
    in_11a = await _row(raw_db, w, w.s1, "11-A")
    first = (await client.get(BASE, headers=auth(w, w.s1))).json()
    assert len(first["items"]) == 20 and first["next_cursor"] is not None
    filtered = (await client.get(BASE, headers=auth(w, w.s1), params={"class_id": str(w.classes["11-A"].id), "limit": 50})).json()
    assert [i["id"] for i in filtered["items"]] == [str(in_11a.id)] and filtered["next_cursor"] is None
    only_10a = (await client.get(BASE, headers=auth(w, w.s1), params={"class_id": str(w.classes["10-A"].id), "limit": 50})).json()
    assert {i["id"] for i in only_10a["items"]} == {str(c.id) for c in in_10a}
    # A class of another user or another tenant only filters: the caller's own list is empty, never someone else's
    other = (await client.get(BASE, headers=auth(w, w.s1), params={"class_id": str(w.classes["10-B"].id)})).json()
    assert other == {"items": [], "next_cursor": None}


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


@pytest.mark.parametrize("cursor", [
    "garbage!",
    _b64("no-separator"),
    _b64("2026-11-01T10:00:00+00:00|not-a-uuid"),
    _b64(f"not-a-date|{uuid.uuid4()}"),
    _b64(f"2026-11-01T10:00:00|{uuid.uuid4()}"),  # no time zone
    _b64(f"2026-11-01T10:00:00+00:00|{uuid.uuid4()}|extra"),
    "A" * 500,
    "été",
])
async def test_an_invalid_cursor_is_422_invalid_cursor(cursor: str, client: AsyncClient, academic_world: AcademicWorld) -> None:
    w = academic_world.a
    resp = await client.get(BASE, headers=auth(w, w.s1), params={"cursor": cursor})
    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": {"code": "invalid_cursor",
                                      "message": chat_conversations.ERROR_MESSAGES["invalid_cursor"]}}


def test_a_long_cursor_is_refused_before_it_is_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real cursor is about 92 characters; a longer one is refused without decoding it (a cost bound)."""
    decoded: list[Any] = []
    real = base64.b64decode
    def spy(*args: Any, **kwargs: Any) -> bytes:
        decoded.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(chat_conversations.base64, "b64decode", spy)
    longest_real = chat_conversations.encode_cursor(datetime(2026, 11, 1, 10, 0, 0, 123456, tzinfo=UTC), uuid.uuid4())
    assert len(longest_real) < chat_conversations.MAX_CURSOR_CHARS
    assert chat_conversations.decode_cursor(longest_real)[0].microsecond == 123456 and len(decoded) == 1
    with pytest.raises(chat_conversations.CursorError):
        chat_conversations.decode_cursor(longest_real + "A" * chat_conversations.MAX_CURSOR_CHARS)
    assert len(decoded) == 1


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 51}, {"limit": "x"}, {"class_id": "nope"}])
async def test_list_query_is_validated(params: dict[str, Any], client: AsyncClient, academic_world: AcademicWorld) -> None:
    w = academic_world.a
    assert (await client.get(BASE, headers=auth(w, w.s1), params=params)).status_code == 422


# ── Delete ────────────────────────────────────────────────────────────────────


async def test_delete_is_a_hard_delete_that_cascades_and_is_audited_without_content(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    w = academic_world.a
    canary = f"CANARY-{uuid.uuid4().hex}"
    conv = await _row(raw_db, w, w.s1, "10-A", title=f"title {canary}", message_count=2)
    question = Message(tenant_id=w.id, conversation_id=conv.id, user_id=w.s1.id, role="user", content=f"q {canary}")
    raw_db.add(question)
    await raw_db.flush()
    raw_db.add(Message(tenant_id=w.id, conversation_id=conv.id, user_id=w.s1.id, role="assistant",
                       content=f"a {canary}", reply_to_id=question.id, outcome="answered", route="rag"))
    await raw_db.commit()

    resp = await client.delete(f"{BASE}/{conv.id}", headers=auth(w, w.s1))
    assert resp.status_code == 204 and resp.content == b""
    assert (await raw_db.execute(select(Conversation.id).where(Conversation.id == conv.id))).first() is None
    assert (await raw_db.execute(select(Message.id).where(Message.conversation_id == conv.id))).first() is None

    audits = (await raw_db.execute(
        select(AuditLog).where(AuditLog.tenant_id == w.id, AuditLog.action == "chat:conversation_delete")
    )).scalars().all()
    assert len(audits) == 1
    entry = audits[0]
    assert entry.actor_id == w.s1.id and entry.target_type == "conversation" and entry.target_id == str(conv.id)
    assert entry.details == {"conversation_id": str(conv.id), "message_count": 2}
    assert canary not in str(entry.details)

    again = await client.delete(f"{BASE}/{conv.id}", headers=auth(w, w.s1))
    assert again.status_code == 404 and again.json() == NOT_FOUND


async def test_a_refused_delete_writes_no_audit_row(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    w = academic_world.a
    conv = await _row(raw_db, w, w.s1, "10-A")
    assert (await client.delete(f"{BASE}/{conv.id}", headers=auth(w, w.s2))).status_code == 404
    audits = (await raw_db.execute(
        select(AuditLog.id).where(AuditLog.tenant_id == w.id, AuditLog.action == "chat:conversation_delete")
    )).all()
    assert audits == []


# ── Connections and logs ──────────────────────────────────────────────────────


class _PoolWatch:
    """Counts the app_tenant pool's checked-out connections at every checkout and checkin."""

    def __init__(self) -> None:
        self.current = 0
        self.peak = 0

    def checkout(self, *_a: Any) -> None:
        self.current += 1
        self.peak = max(self.peak, self.current)

    def checkin(self, *_a: Any) -> None:
        self.current -= 1


@pytest.fixture
def pool_watch() -> Iterator[_PoolWatch]:
    watch = _PoolWatch()
    event.listen(app_engine.sync_engine, "checkout", watch.checkout)
    event.listen(app_engine.sync_engine, "checkin", watch.checkin)
    try:
        yield watch
    finally:
        event.remove(app_engine.sync_engine, "checkout", watch.checkout)
        event.remove(app_engine.sync_engine, "checkin", watch.checkin)


async def test_no_route_holds_a_pooled_connection_while_it_waits(
    client: AsyncClient, academic_world: AcademicWorld, pool_watch: _PoolWatch, monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = academic_world.a
    seen_during_wait: list[int] = []
    real = slots.default_manager

    class SpySlots:
        async def acquire(self, limits: Any, **kwargs: Any) -> Any:
            seen_during_wait.append(int(app_engine.pool.checkedout()))  # type: ignore[attr-defined]
            return await real.acquire(limits, **kwargs)

    monkeypatch.setattr(chat_conversations, "_slots", lambda: SpySlots())
    assert int(app_engine.pool.checkedout()) == 0  # type: ignore[attr-defined]
    headers = auth(w, w.s1)

    created = await _create(client, w, w.s1, "10-A", "Math")
    assert created.status_code == 201, created.text
    assert seen_during_wait == [0], "the creation window was taken while a connection was checked out"
    conv_id = created.json()["id"]
    calls = [("GET", BASE), ("GET", f"{BASE}/{conv_id}"), ("DELETE", f"{BASE}/{conv_id}")]
    for method, url in calls:
        resp = await client.request(method, url, headers=headers)
        assert resp.status_code in (200, 204), resp.text
    # The permission check's connection is released before the route opens its own: never two at once
    assert pool_watch.peak == 1, f"{pool_watch.peak} connections were checked out at the same time"
    assert int(app_engine.pool.checkedout()) == 0  # type: ignore[attr-defined]


async def test_no_title_or_message_text_reaches_the_logs(
    client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession, caplog: pytest.LogCaptureFixture,
    small_window: int,
) -> None:
    w = academic_world.a
    canary = f"CANARY-{uuid.uuid4().hex}"
    conv = await _row(raw_db, w, w.s1, "10-A", title=canary)
    headers = auth(w, w.s1)
    caplog.set_level("DEBUG")
    with structlog.testing.capture_logs() as captured:
        await client.get(BASE, headers=headers)
        await client.get(f"{BASE}/{conv.id}", headers=headers)
        for _ in range(small_window + 1):
            await _create(client, w, w.s1, "10-A")
        await client.delete(f"{BASE}/{conv.id}", headers=headers)
    assert canary not in str(captured) and canary not in caplog.text
