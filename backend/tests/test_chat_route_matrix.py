"""
tests/test_chat_route_matrix.py
───────────────────────────────
Route x role matrix for every chat route (plan 11.1, step 6a-9a), GENERATED from the real route table: a route under
/api/v1/chat or /api/v1/admin/chat without a row in EXPECTED fails the guard test, so the routes of later steps
(messages 6a-9b, feedback and settings 6a-12) join the matrix as soon as they exist.

Roles: admin; teacher in scope (T1, who teaches 10-A Math); teacher out of scope (T2); student enrolled (S1, in 10-A);
student not enrolled (S2); a user of another tenant (tenant b's S1, calling with tenant a's ids); a custom role
without chat:use (the world's document:view-only viewer); anonymous.

The conversation routes act on S1's own 10-A Math conversation: nobody else may see it, not even an in-scope teacher
or the admin (Q4: no reading of other users' chats in Phase 6), and every refusal is the generic 404.
Every chat route must use require_released (no pooled connection held, B100) and none may depend on the
request-long tenant session.
"""

import re
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core import tenant_db
from app.core.authz import GENERIC_NOT_FOUND, get_route_declared_permission
from app.db.models.chat import Conversation
from app.main import app
from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.documents import auth
from tests.test_document_store import all_app_routes

ROLES = ("admin", "teacher_in", "teacher_out", "student_in", "student_out", "other_tenant", "no_chat_use", "anonymous")
CONV = "/api/v1/chat/conversations"

# (method, path) -> (permission, expected status per role, in the order of ROLES)
EXPECTED: dict[tuple[str, str], tuple[str, tuple[int, ...]]] = {
    ("POST", CONV): ("chat:use", (201, 201, 404, 201, 404, 404, 403, 401)),
    ("GET", CONV): ("chat:use", (200, 200, 200, 200, 200, 200, 403, 401)),
    ("GET", f"{CONV}/{{conversation_id}}"): ("chat:use", (404, 404, 404, 200, 404, 404, 403, 401)),
    ("DELETE", f"{CONV}/{{conversation_id}}"): ("chat:use", (404, 404, 404, 204, 404, 404, 403, 401)),
}


def _chat_routes() -> dict[tuple[str, str], Any]:
    found: dict[tuple[str, str], Any] = {}
    for route in all_app_routes(app):
        path = getattr(route, "path", "")
        if not re.match(r"^/api/v1/(admin/)?chat(/|$)", path):
            continue
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            found[(method, path)] = route
    return found


def _calls(dependant: Any) -> list[Any]:
    out: list[Any] = []
    for sub in getattr(dependant, "dependencies", []):
        out.append(getattr(sub, "call", None))
        out += _calls(sub)
    return out


def test_every_chat_route_is_in_the_matrix_and_declares_its_permission() -> None:
    routes = _chat_routes()
    assert routes, "no chat route found: the matrix would check nothing"
    assert set(routes) == set(EXPECTED), (
        f"routes without a matrix row: {sorted(set(routes) - set(EXPECTED))}; "
        f"matrix rows without a route: {sorted(set(EXPECTED) - set(routes))}"
    )
    for key, route in routes.items():
        assert get_route_declared_permission(route) == EXPECTED[key][0], key


def test_every_chat_route_uses_the_released_check_and_never_the_request_long_session() -> None:
    for key, route in _chat_routes().items():
        calls = _calls(route.dependant)
        held = {deps.get_tenant_session, tenant_db.get_tenant_session, deps.get_current_user}
        assert not held & set(calls), f"{key} holds a tenant session for the whole request (B100)"
        tagged = [c for c in calls if getattr(c, "__declared_permission__", None)]
        assert tagged, key
        assert all("require_released" in getattr(c, "__qualname__", "") for c in tagged), (key, tagged)


async def _prepare(key: tuple[str, str], world: AcademicWorld, raw_db: AsyncSession) -> tuple[str, dict[str, Any]]:
    """A fresh state for one call: the concrete path and the request arguments."""
    w = world.a
    method, path = key
    if key == ("POST", CONV):
        return path, {"json": {"class_id": str(w.classes["10-A"].id), "subject_id": str(w.subjects["Math"].id)}}
    if "{conversation_id}" not in path:
        return path, {}
    conv = Conversation(tenant_id=w.id, user_id=w.s1.id, class_id=w.classes["10-A"].id, subject_id=w.subjects["Math"].id)
    raw_db.add(conv)
    await raw_db.commit()
    return path.replace("{conversation_id}", str(conv.id)), {}


@pytest.mark.parametrize("key", list(EXPECTED), ids=[f"{m} {p}" for m, p in EXPECTED])
async def test_route_role_matrix(
    key: tuple[str, str], client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession
) -> None:
    a, b = academic_world.a, academic_world.b
    callers = {
        "admin": auth(a, a.admin), "teacher_in": auth(a, a.t1), "teacher_out": auth(a, a.t2),
        "student_in": auth(a, a.s1), "student_out": auth(a, a.s2), "other_tenant": auth(b, b.s1),
        "no_chat_use": auth(a, a.viewer), "anonymous": {"X-Tenant-Slug": a.tenant.slug},
    }
    method = key[0]
    got: dict[str, int] = {}
    for role, expected in zip(ROLES, EXPECTED[key][1], strict=True):
        path, kwargs = await _prepare(key, academic_world, raw_db)
        resp = await client.request(method, path, headers=callers[role], **kwargs)
        got[role] = resp.status_code
        assert resp.status_code == expected, f"{method} {key[1]} as {role}: {resp.status_code} {resp.text}"
        if expected == 404:
            assert resp.json() == {"detail": GENERIC_NOT_FOUND}, (role, resp.text)
        if expected == 403:
            assert resp.json()["detail"]["code"] == "permission_denied", (role, resp.text)
    assert tuple(got[r] for r in ROLES) == EXPECTED[key][1]


async def test_list_shows_each_caller_only_their_own(client: AsyncClient, academic_world: AcademicWorld, raw_db: AsyncSession) -> None:
    a, b = academic_world.a, academic_world.b
    owners = {"s1": (a, a.s1), "t1": (a, a.t1), "admin": (a, a.admin), "b_s1": (b, b.s1)}
    made: dict[str, uuid.UUID] = {}
    for name, (w, user) in owners.items():
        conv = Conversation(tenant_id=w.id, user_id=user.id, class_id=w.classes["10-A"].id)
        raw_db.add(conv)
        await raw_db.commit()
        made[name] = conv.id
    for name, (w, user) in owners.items():
        items = (await client.get(CONV, headers=auth(w, user))).json()["items"]
        assert [i["id"] for i in items] == [str(made[name])], name
