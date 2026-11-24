"""
tests/test_auth_released_session.py
───────────────────────────────────
Step 6a-1a (backlog B100): `require_released(permission)` checks the permission in its own short tenant
transaction, closes it (the connection goes back to the pool) and hands the handler a detached user.

What is proven here, on a test-only app (no existing route uses require_released yet):
  1. Pool introspection: while the handler awaits and while a StreamingResponse body is sent, a
     require_released route holds NO checked-out connection; the same route with require(...) holds one
     (so the test can see the B100 problem).
  2. Same permission semantics as require(...): the same 401/403 bodies, a deactivated user is refused,
     a permission removed from a role takes effect on the next request.
  3. Tenant context: the permission transaction sets app.tenant_id with set_config(..., true), and a fresh
     session on the same (one-connection) pool sees no setting afterwards.
  4. The default-deny guard and the route matrix accept a require_released route; a route with neither
     dependency still fails the guard.
  5. The returned user is detached and fully loaded: touching it runs no SQL.
  F12: on the installed FastAPI, when the exit code of a `yield` dependency runs relative to a
     StreamingResponse body (pinned for both dependency scopes).
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, event, select, text, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.core.tenant_db as tenant_db
from app.core.authz import get_route_declared_permission, require, require_released
from app.core.config import settings
from app.core.middleware import TenantResolutionMiddleware
from app.core.security import create_access_token
from app.db.engine import engine as app_engine
from app.db.models.rbac import Role, RolePermission, UserRole
from app.db.models.tenant import Tenant
from app.db.models.user import User
from tests.test_document_store import all_app_routes

# ── The test-only app ─────────────────────────────────────────────────────────

KINDS: dict[str, Callable[[str], Any]] = {"held": require, "released": require_released}
PERMISSIONS = ("chat:use", "user:manage")
USER_FIELDS = ("tenant_id", "id", "email", "full_name", "password_hash", "is_admin", "status", "created_at", "updated_at")


@dataclass
class Gate:
    """Lets a test pause a handler (or a streamed body) and look at the pool meanwhile."""

    entered: asyncio.Event = field(default_factory=asyncio.Event)
    proceed: asyncio.Event = field(default_factory=asyncio.Event)
    checked_out_in_body: list[int] = field(default_factory=list)
    sql_in_handler: int = -1


GATE = Gate()


def _user_view(user: User) -> dict[str, str]:
    return {name: str(getattr(user, name)) for name in USER_FIELDS}


def _path(kind: str, permission: str) -> str:
    return f"/t/{kind}/{permission.replace(':', '-')}"


def _build_test_app() -> FastAPI:
    test_app = FastAPI()
    test_app.add_middleware(TenantResolutionMiddleware)
    router = APIRouter()

    for kind, factory in KINDS.items():
        for permission in PERMISSIONS:

            def _plain(user: User = Depends(factory(permission))) -> dict[str, str]:  # noqa: B008
                return _user_view(user)

            router.add_api_route(_path(kind, permission), _plain, methods=["GET"])

        async def _hold(user: User = Depends(factory("chat:use"))) -> dict[str, str]:  # noqa: B008
            GATE.entered.set()
            await GATE.proceed.wait()
            # Count the SQL that touching the user causes after the permission check
            with _count_sql(app_engine) as counter:
                view = _user_view(user)
            GATE.sql_in_handler = counter.count
            return view

        router.add_api_route(f"/t/{kind}/hold", _hold, methods=["GET"])

        def _stream(user: User = Depends(factory("chat:use"))) -> StreamingResponse:  # noqa: B008
            async def body() -> AsyncIterator[bytes]:
                for i in range(3):
                    GATE.checked_out_in_body.append(app_engine.pool.checkedout())  # type: ignore[attr-defined]
                    if i == 1:
                        GATE.entered.set()
                        await GATE.proceed.wait()
                    yield f"chunk-{i}-{user.email}\n".encode()

            return StreamingResponse(body(), media_type="text/plain")

        router.add_api_route(f"/t/{kind}/stream", _stream, methods=["GET"])

    test_app.include_router(router)
    return test_app


TEST_APP = _build_test_app()


# ── Helpers ───────────────────────────────────────────────────────────────────


class _SqlCounter:
    def __init__(self) -> None:
        self.count = 0
        self.statements: list[str] = []


class _count_sql:  # noqa: N801  (used like a function in a with statement)
    """Counts every statement sent on an engine while the block runs."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.counter = _SqlCounter()

    def _on_execute(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        self.counter.count += 1
        self.counter.statements.append(statement)

    def __enter__(self) -> _SqlCounter:
        event.listen(self.engine.sync_engine, "before_cursor_execute", self._on_execute)
        return self.counter

    def __exit__(self, *_exc: object) -> None:
        event.remove(self.engine.sync_engine, "before_cursor_execute", self._on_execute)


def _checked_out() -> int:
    return int(app_engine.pool.checkedout())  # type: ignore[attr-defined]


def _auth(tenant: Tenant, user: User) -> dict[str, str]:
    token = create_access_token(tenant_id=str(tenant.id), user_id=str(user.id), is_admin=user.is_admin)
    return {"Authorization": f"Bearer {token}", "X-Tenant-Slug": tenant.slug}


async def _make_user(raw_db: AsyncSession, tenant: Tenant, role_name: str | None) -> User:
    user = User(
        tenant_id=tenant.id,
        email=f"released-{uuid.uuid4().hex[:10]}@example.com",
        full_name="Released Test",
        status="active",
        is_admin=False,
    )
    raw_db.add(user)
    await raw_db.flush()
    if role_name is not None:
        role_id = (
            await raw_db.execute(select(Role.id).where(Role.tenant_id == tenant.id, Role.name == role_name))
        ).scalar_one()
        raw_db.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role_id))
    await raw_db.commit()
    await raw_db.refresh(user)
    return user


@pytest.fixture(autouse=True)
def _fresh_gate() -> None:
    global GATE
    GATE = Gate()


@pytest_asyncio.fixture
async def tclient() -> AsyncGenerator[AsyncClient, None]:
    # Host "localhost": the middleware then reads X-Tenant-Slug, so a tenant mismatch is visible
    async with AsyncClient(transport=ASGITransport(app=TEST_APP), base_url="http://localhost") as c:
        yield c


# ── 1. Pool introspection ─────────────────────────────────────────────────────


@pytest.mark.parametrize(("kind", "expected"), [("released", 0), ("held", 1)])
async def test_checked_out_connections_while_the_handler_awaits(
    kind: str, expected: int, tclient: AsyncClient, raw_db: AsyncSession, tenant_a: Tenant
) -> None:
    student = await _make_user(raw_db, tenant_a, "student")
    assert _checked_out() == 0, "a connection was already checked out before the request"
    task = asyncio.create_task(tclient.get(f"/t/{kind}/hold", headers=_auth(tenant_a, student)))
    await asyncio.wait_for(GATE.entered.wait(), 10)
    during = _checked_out()
    GATE.proceed.set()
    resp = await task
    assert resp.status_code == 200, resp.text
    assert during == expected, f"{kind}: {during} checked-out connection(s) while the handler awaited"
    assert _checked_out() == 0


@pytest.mark.parametrize(("kind", "expected"), [("released", 0), ("held", 1)])
async def test_checked_out_connections_while_a_streamed_body_is_sent(
    kind: str, expected: int, tclient: AsyncClient, raw_db: AsyncSession, tenant_a: Tenant
) -> None:
    student = await _make_user(raw_db, tenant_a, "student")
    assert _checked_out() == 0
    task = asyncio.create_task(tclient.get(f"/t/{kind}/stream", headers=_auth(tenant_a, student)))
    await asyncio.wait_for(GATE.entered.wait(), 10)
    during = _checked_out()  # the body is paused between chunk 1 and chunk 2
    GATE.proceed.set()
    resp = await task
    assert resp.status_code == 200, resp.text
    assert resp.text.count("chunk-") == 3
    assert during == expected, f"{kind}: {during} checked-out connection(s) during the body"
    assert GATE.checked_out_in_body == [expected] * 3
    assert _checked_out() == 0


# ── 2. Same permission semantics as require(...) ──────────────────────────────


async def _both(tclient: AsyncClient, permission: str, headers: dict[str, str]) -> dict[str, tuple[int, Any]]:
    out: dict[str, tuple[int, Any]] = {}
    for kind in KINDS:
        resp = await tclient.get(_path(kind, permission), headers=headers)
        out[kind] = (resp.status_code, resp.json())
    assert out["released"] == out["held"], out
    return out


async def test_same_401_and_403_bodies_as_require(
    tclient: AsyncClient, raw_db: AsyncSession, tenant_a: Tenant, tenant_b: Tenant
) -> None:
    student = await _make_user(raw_db, tenant_a, "student")
    headers = _auth(tenant_a, student)

    no_token = await _both(tclient, "chat:use", {"X-Tenant-Slug": tenant_a.slug})
    assert no_token["released"] == (401, {"detail": {"code": "missing_token", "message": "Authentication required."}})

    bad = await _both(tclient, "chat:use", {"Authorization": "Bearer not-a-jwt", "X-Tenant-Slug": tenant_a.slug})
    assert bad["released"][0] == 401 and bad["released"][1]["detail"]["code"] == "invalid_token"

    mismatch = await _both(tclient, "chat:use", {**headers, "X-Tenant-Slug": tenant_b.slug})
    assert mismatch["released"][0] == 403 and mismatch["released"][1]["detail"]["code"] == "tenant_mismatch"

    denied = await _both(tclient, "user:manage", headers)
    assert denied["released"] == (
        403, {"detail": {"code": "permission_denied", "message": "Missing required permission: 'user:manage'."}}
    )

    allowed = await _both(tclient, "chat:use", headers)
    assert allowed["released"][0] == 200
    assert allowed["released"][1]["id"] == str(student.id)


async def test_deactivated_user_is_rejected(tclient: AsyncClient, raw_db: AsyncSession, tenant_a: Tenant) -> None:
    student = await _make_user(raw_db, tenant_a, "student")
    headers = _auth(tenant_a, student)
    assert (await _both(tclient, "chat:use", headers))["released"][0] == 200

    await raw_db.execute(update(User).where(User.tenant_id == tenant_a.id, User.id == student.id).values(status="deactivated"))
    await raw_db.commit()
    gone = await _both(tclient, "chat:use", headers)
    assert gone["released"] == (401, {"detail": {"code": "user_not_found"}})


async def test_revoked_permission_takes_effect_on_the_next_request(
    tclient: AsyncClient, raw_db: AsyncSession, tenant_a: Tenant
) -> None:
    role = Role(tenant_id=tenant_a.id, name=f"chatter-{uuid.uuid4().hex[:8]}", is_system=False)
    raw_db.add(role)
    await raw_db.flush()
    raw_db.add(RolePermission(tenant_id=tenant_a.id, role_id=role.id, permission_id="chat:use"))
    await raw_db.commit()
    user = await _make_user(raw_db, tenant_a, role.name)
    headers = _auth(tenant_a, user)

    assert (await _both(tclient, "chat:use", headers))["released"][0] == 200

    await raw_db.execute(
        delete(RolePermission).where(RolePermission.tenant_id == tenant_a.id, RolePermission.role_id == role.id)
    )
    await raw_db.commit()
    revoked = await _both(tclient, "chat:use", headers)
    assert revoked["released"][0] == 403
    assert revoked["released"][1]["detail"]["code"] == "permission_denied"


@pytest.mark.parametrize("permission", PERMISSIONS)
async def test_route_role_matrix_is_the_same_for_both_dependencies(
    permission: str, tclient: AsyncClient, raw_db: AsyncSession, tenant_a: Tenant, admin_a: User
) -> None:
    teacher = await _make_user(raw_db, tenant_a, "teacher")
    student = await _make_user(raw_db, tenant_a, "student")
    roles = {"admin": _auth(tenant_a, admin_a), "teacher": _auth(tenant_a, teacher), "student": _auth(tenant_a, student),
             "anonymous": {"X-Tenant-Slug": tenant_a.slug}}
    expected = {"chat:use": (200, 200, 200, 401), "user:manage": (200, 403, 403, 401)}[permission]
    got = []
    for headers in roles.values():
        got.append((await _both(tclient, permission, headers))["released"][0])
    assert tuple(got) == expected


# ── 3. Tenant context of the permission transaction ───────────────────────────


@pytest_asyncio.fixture
async def one_connection_pool(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Any, None]:
    """Every tenant session (also the JWT tenant check) runs on ONE pooled connection for this test."""
    one = create_async_engine(
        settings.DATABASE_URL, pool_size=1, max_overflow=0, connect_args={"statement_cache_size": 0}
    )
    monkeypatch.setattr(
        tenant_db, "SessionLocal", async_sessionmaker(bind=one, expire_on_commit=False, autoflush=False)
    )
    try:
        yield one
    finally:
        await one.dispose()


async def test_permission_transaction_sets_tenant_locally_and_it_is_gone_after_release(
    tclient: AsyncClient, raw_db: AsyncSession, tenant_a: Tenant, one_connection_pool: Any
) -> None:
    student = await _make_user(raw_db, tenant_a, "student")
    log: list[tuple[str, str, Any]] = []

    def on_execute(_c: Any, _cur: Any, statement: str, parameters: Any, *_a: Any) -> None:
        log.append(("sql", statement, parameters))

    def on_begin(_c: Any) -> None:
        log.append(("begin", "", None))

    def on_commit(_c: Any) -> None:
        log.append(("commit", "", None))

    def on_rollback(_c: Any) -> None:
        log.append(("rollback", "", None))

    sync = one_connection_pool.sync_engine
    listeners = (("before_cursor_execute", on_execute), ("begin", on_begin), ("commit", on_commit),
                 ("rollback", on_rollback))
    for name, fn in listeners:
        event.listen(sync, name, fn)
    try:
        resp = await tclient.get(_path("released", "chat:use"), headers=_auth(tenant_a, student))
    finally:
        for name, fn in listeners:
            event.remove(sync, name, fn)
    assert resp.status_code == 200, resp.text

    # Split the log into transactions: the JWT tenant check, then the permission check
    transactions: list[list[tuple[str, str, Any]]] = []
    for entry in log:
        if entry[0] == "begin":
            transactions.append([])
        elif entry[0] == "sql":
            transactions[-1].append(entry)
    assert [e[0] for e in log].count("begin") == 2 == [e[0] for e in log].count("commit"), log
    permission_tx = transactions[1]
    first = permission_tx[0]
    assert "set_config('app.tenant_id'" in first[1] and ", true)" in first[1], first
    assert str(tenant_a.id) in str(first[2]), first
    assert any("app.users" in s for _k, s, _p in permission_tx[1:]), permission_tx
    assert any("app.user_roles" in s for _k, s, _p in permission_tx[1:]), permission_tx

    # The connection is back in the pool, and a fresh session on it sees no tenant
    pool = one_connection_pool.pool
    assert pool.checkedout() == 0 and pool.checkedin() == 1
    async with tenant_db.SessionLocal() as fresh:
        leftover = (await fresh.execute(text("SELECT current_setting('app.tenant_id', true)"))).scalar_one_or_none()
    assert leftover in (None, ""), leftover


# ── 4. Default-deny guard and route matrix ────────────────────────────────────


def _undeclared_routes(application: FastAPI) -> list[str]:
    """The default-deny check of tests/test_authz_rbac.py, over any app."""
    docs_paths = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    missing = []
    for route in all_app_routes(application):
        path = getattr(route, "path", "")
        if path in docs_paths or not hasattr(route, "endpoint"):
            continue
        if not get_route_declared_permission(route):
            missing.append(f"{sorted(getattr(route, 'methods', []))} {path}")
    return missing


def test_released_dependency_carries_the_declared_permission_tag() -> None:
    for permission in PERMISSIONS:
        assert getattr(require_released(permission), "__declared_permission__", None) == permission
        assert getattr(require(permission), "__declared_permission__", None) == permission


def test_default_deny_guard_accepts_require_released_and_rejects_an_undeclared_route() -> None:
    guarded = FastAPI()

    @guarded.get("/param")
    async def param(user: User = Depends(require_released("chat:use"))) -> None:  # noqa: B008
        return None

    @guarded.get("/route-level", dependencies=[Depends(require_released("chat:use"))])
    async def route_level() -> None:
        return None

    assert _undeclared_routes(guarded) == []
    by_path = {getattr(r, "path", ""): r for r in all_app_routes(guarded)}
    # The route matrices compare get_route_declared_permission(route) with the expected permission
    assert get_route_declared_permission(by_path["/param"]) == "chat:use"
    assert get_route_declared_permission(by_path["/route-level"]) == "chat:use"

    @guarded.get("/open")
    async def open_route() -> None:
        return None

    assert _undeclared_routes(guarded) == ["['GET'] /open"]
    # The whole test app is declared too
    assert _undeclared_routes(TEST_APP) == []


# ── 5. Detached user ──────────────────────────────────────────────────────────


async def test_returned_user_is_detached_and_touching_it_runs_no_sql(raw_db: AsyncSession, tenant_a: Tenant) -> None:
    student = await _make_user(raw_db, tenant_a, "student")
    token = create_access_token(tenant_id=str(tenant_a.id), user_id=str(student.id))
    dep = require_released("chat:use")

    user = await dep(tenant_id=str(tenant_a.id), token=token)
    assert _checked_out() == 0

    state = sa_inspect(user)
    assert state.detached, "the user must not belong to any session after release"
    assert not state.expired_attributes and not state.unloaded, state.unloaded
    with _count_sql(app_engine) as counter:
        view = _user_view(user)
    assert counter.count == 0, counter.statements
    assert view == _user_view(student)


async def test_handler_touching_the_user_runs_no_sql(
    tclient: AsyncClient, raw_db: AsyncSession, tenant_a: Tenant
) -> None:
    student = await _make_user(raw_db, tenant_a, "student")
    GATE.proceed.set()
    resp = await tclient.get("/t/released/hold", headers=_auth(tenant_a, student))
    assert resp.status_code == 200, resp.text
    assert GATE.sql_in_handler == 0
    assert resp.json()["email"] == student.email


# ── F12: yield-dependency exit relative to a StreamingResponse body ───────────


def _f12_app(log: list[str]) -> FastAPI:
    pinned = FastAPI()

    async def dep_request_scope() -> AsyncIterator[None]:
        log.append("dep_enter")
        yield
        log.append("dep_exit")

    async def body() -> AsyncIterator[bytes]:
        for i in range(2):
            log.append(f"body_{i}")
            yield b"x"

    @pinned.get("/default")
    async def default_scope(_: None = Depends(dep_request_scope)) -> StreamingResponse:  # noqa: B008
        log.append("handler")
        return StreamingResponse(body())

    @pinned.get("/function")
    async def function_scope(_: None = Depends(dep_request_scope, scope="function")) -> StreamingResponse:  # noqa: B008
        log.append("handler")
        return StreamingResponse(body())

    return pinned


@pytest.mark.parametrize(
    ("path", "order"),
    [
        # Default ("request") scope on the installed FastAPI: the exit code runs AFTER the whole body is sent,
        # so a session dependency would stay checked out for the entire stream (B100)
        ("/default", ["dep_enter", "handler", "body_0", "body_1", "dep_exit"]),
        # scope="function": the exit code runs when the handler returns, BEFORE the body is sent
        ("/function", ["dep_enter", "handler", "dep_exit", "body_0", "body_1"]),
    ],
)
async def test_f12_yield_dependency_exit_order_with_streaming_response(path: str, order: list[str]) -> None:
    log: list[str] = []
    async with AsyncClient(transport=ASGITransport(app=_f12_app(log)), base_url="http://localhost") as c:
        resp = await c.get(path)
    assert resp.status_code == 200 and resp.content == b"xx"
    assert log == order
