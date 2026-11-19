"""
The app-wide 422 answer (app/main.py; review of 5d-2, finding 8): FastAPI's error list without the echoed
input. The default handler repeated what the client sent ("input"), and for a missing field that is the WHOLE
body, so a login request without an email echoed the password back. Kept: type, loc, msg and, only when it
holds the schema's own limits, ctx.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from tests.fixtures.academic_world import AcademicWorld
from tests.fixtures.documents import auth
from tests.security.scanner_assertions import assert_not_echoed

PASSWORD = "Sup3r-Secret-Passw0rd-for-422"

LOGIN_BODIES: list[tuple[str, Any]] = [
    ("missing tenant slug", {"email": "a@b.example", "password": PASSWORD}),
    ("missing email", {"password": PASSWORD, "tenant_slug": "acme"}),
    ("email of the wrong type", {"email": 12345, "password": PASSWORD, "tenant_slug": "acme"}),
    ("password of the wrong type", {"email": "a@b.example", "password": [PASSWORD], "tenant_slug": "acme"}),
    ("invalid email", {"email": f"{PASSWORD}@", "password": PASSWORD, "tenant_slug": "acme"}),
    ("not an object", [PASSWORD]),
]


@pytest.mark.parametrize(("label", "body"), LOGIN_BODIES, ids=[label for label, _b in LOGIN_BODIES])
async def test_a_failing_login_never_echoes_the_password(
    label: str, body: Any, client: AsyncClient, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    resp = await client.post("/api/v1/auth/login", headers={"X-Tenant-Slug": w.tenant.slug}, json=body)
    assert_not_echoed(resp, PASSWORD)
    for error in resp.json()["detail"]:
        assert set(error) <= {"type", "loc", "msg", "ctx"}, error


async def test_the_error_list_keeps_type_location_message_and_the_schemas_limits(
    client: AsyncClient, academic_world: AcademicWorld
) -> None:
    w = academic_world.a
    resp = await client.post(
        "/api/v1/documents/test-search", headers=auth(w, w.t1), json={"query": "waves", "k": 99}
    )
    assert resp.status_code == 422
    (error,) = resp.json()["detail"]
    assert (error["type"], error["loc"], error["ctx"]) == ("less_than_equal", ["body", "k"], {"le": 10})
    assert "99" not in resp.text and "input" not in error
