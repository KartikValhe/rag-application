"""
Prove-it-fails for the upload protections of step 5a-3.

Every security assertion is a helper (tests/security/upload_assertions.py) that the real tests
also use. Here each protection is switched off with monkeypatch and the helper must RAISE, which
shows the helper really checks the protection and does not pass by accident.

Test database only: nothing here touches Postgres. The documents probe uses Redis DB 1 (per-IP upload slots).
"""

import dataclasses
import re
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core import body_limit, upload_security
from app.core.body_limit import BodyLimit, BodyLimitMiddleware
from app.core.security import create_access_token
from app.services import document_store as ds
from app.services.document_store import LocalPrivateStore
from tests.security.upload_assertions import (
    assert_documents_limits_enforced,
    assert_hostile_pdfs_rejected,
    assert_store_enforces_tenant_prefix,
)

DOCS_URL = "/api/v1/documents"


def _auth() -> dict[str, str]:
    token = create_access_token(tenant_id=str(uuid.uuid4()), user_id=str(uuid.uuid4()))
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def probe_client() -> AsyncGenerator[AsyncClient, None]:
    app = FastAPI()

    async def probe(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    app.add_api_route(DOCS_URL, probe, methods=["POST"])
    app.add_middleware(BodyLimitMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://probe") as c:
        yield c


def _replace_documents_rule(monkeypatch: pytest.MonkeyPatch, limit: BodyLimit | None) -> None:
    """Swap the POST /api/v1/documents rule for one with `limit`, or drop it when limit is None."""
    routes = []
    for rule in body_limit.UPLOAD_ROUTES:
        if rule.method == "POST" and rule.pattern.pattern == r"^/api/v1/documents$":
            if limit is not None:
                routes.append(dataclasses.replace(rule, limit=limit))
        else:
            routes.append(rule)
    monkeypatch.setattr(body_limit, "UPLOAD_ROUTES", tuple(routes))


# ─── PDF forbidden-name regex ────────────────────────────────────────────────

def test_baseline_hostile_pdf_helper_passes() -> None:
    assert_hostile_pdfs_rejected()


def test_hostile_pdf_helper_fails_when_the_forbidden_regex_is_emptied(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_security, "PDF_FORBIDDEN_RE", re.compile(rb"(?!)"))  # matches nothing
    with pytest.raises(AssertionError, match="hostile PDF"):
        assert_hostile_pdfs_rejected()


def test_hostile_pdf_helper_fails_when_hex_escapes_are_not_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upload_security, "PDF_HEX_ESCAPE_RE", re.compile(rb"(?!)"))
    with pytest.raises(AssertionError):
        assert_hostile_pdfs_rejected()


# ─── Documents upload route limits ───────────────────────────────────────────

async def test_baseline_documents_limit_helper_passes(probe_client: AsyncClient) -> None:
    await assert_documents_limits_enforced(probe_client, DOCS_URL, _auth())


async def test_documents_limit_helper_fails_when_the_route_is_removed(
    monkeypatch: pytest.MonkeyPatch, probe_client: AsyncClient
) -> None:
    _replace_documents_rule(monkeypatch, None)
    with pytest.raises(AssertionError, match="20 MB body"):
        await assert_documents_limits_enforced(probe_client, DOCS_URL, _auth())


async def test_documents_limit_helper_fails_when_the_ceiling_is_lifted(
    monkeypatch: pytest.MonkeyPatch, probe_client: AsyncClient
) -> None:
    _replace_documents_rule(monkeypatch, BodyLimit(10**12, 6))
    with pytest.raises(AssertionError, match="not rejected"):
        await assert_documents_limits_enforced(probe_client, DOCS_URL, _auth())


async def test_documents_limit_helper_fails_when_the_part_cap_is_lifted(
    monkeypatch: pytest.MonkeyPatch, probe_client: AsyncClient
) -> None:
    _replace_documents_rule(monkeypatch, BodyLimit(body_limit.DOCUMENTS_LIMIT.max_body_bytes, 10**6))
    with pytest.raises(AssertionError, match="7th part"):
        await assert_documents_limits_enforced(probe_client, DOCS_URL, _auth())


# ─── Tenant prefix check of the private store ────────────────────────────────

async def test_baseline_store_helper_passes(tmp_path: Path) -> None:
    await assert_store_enforces_tenant_prefix(LocalPrivateStore(tmp_path), uuid.uuid4(), uuid.uuid4())


async def test_store_helper_fails_when_the_tenant_prefix_check_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ds, "_check_tenant_prefix", lambda tenant_id, key: None)
    with pytest.raises(AssertionError, match="store accepted"):
        await assert_store_enforces_tenant_prefix(LocalPrivateStore(tmp_path), uuid.uuid4(), uuid.uuid4())


async def test_store_helper_fails_when_the_key_shape_check_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only the shape check is removed; the tenant-prefix check stays in place. The helper must catch it by itself."""
    monkeypatch.setattr(ds, "_check_shape", lambda key: None)
    with pytest.raises(AssertionError, match="store accepted"):
        await assert_store_enforces_tenant_prefix(LocalPrivateStore(tmp_path), uuid.uuid4(), uuid.uuid4())
