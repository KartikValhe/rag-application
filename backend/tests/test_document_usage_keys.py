"""
tests/test_document_usage_keys.py
─────────────────────────────────
Fast unit test (no Redis): the two Redis windows read by `uploads_last_hour` are built from the authenticated
user's tenant id and user id ONLY (docs/backend-small-plan.md item 3, test 6). Redis has no row-level security,
so the key is the whole isolation. The key text is the one `check_upload_rate` writes (same prefixes), so the
meter reads the very windows the limiter enforces.
"""

import uuid
from typing import Any

import pytest

from app.core import slots
from app.services import document_upload


class FakeManager:
    """Records every key it is asked to count and answers from a dict."""

    def __init__(self, counts: dict[str, int] | None = None, error: Exception | None = None) -> None:
        self.counts = counts or {}
        self.error = error
        self.asked: list[str] = []

    async def count(self, key: str, **_: Any) -> int:
        self.asked.append(key)
        if self.error is not None:
            raise self.error
        return self.counts.get(key, 0)


def _install(monkeypatch: pytest.MonkeyPatch, manager: FakeManager) -> None:
    monkeypatch.setattr(slots, "default_manager", manager)


def test_the_two_keys_are_built_from_the_tenant_id_and_the_user_id_only() -> None:
    user_id, tenant_id = uuid.uuid4(), uuid.uuid4()
    caller_key, tenant_key = document_upload.upload_window_keys(user_id, tenant_id)
    assert caller_key == f"ratelimit:doc_upload:user:{user_id}"
    assert tenant_key == f"ratelimit:doc_upload:tenant:{tenant_id}"
    # The same prefixes the limiter writes
    assert caller_key == f"{document_upload.USER_RATE_KEY_PREFIX}{user_id}"
    assert tenant_key == f"{document_upload.TENANT_RATE_KEY_PREFIX}{tenant_id}"


async def test_the_meter_asks_for_exactly_those_two_keys_and_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, tenant_id, other_user, other_tenant = (uuid.uuid4() for _ in range(4))
    manager = FakeManager(
        {
            f"ratelimit:doc_upload:user:{user_id}": 3,
            f"ratelimit:doc_upload:tenant:{tenant_id}": 14,
            # Someone else's windows exist too and must never be read
            f"ratelimit:doc_upload:user:{other_user}": 99,
            f"ratelimit:doc_upload:tenant:{other_tenant}": 98,
        }
    )
    _install(monkeypatch, manager)
    counts = await document_upload.uploads_last_hour(user_id, tenant_id)
    assert counts == document_upload.UploadWindowCounts(tenant=14, caller=3)
    assert sorted(manager.asked) == sorted(
        [f"ratelimit:doc_upload:user:{user_id}", f"ratelimit:doc_upload:tenant:{tenant_id}"]
    )
    assert not any(str(other_user) in k or str(other_tenant) in k for k in manager.asked)


@pytest.mark.parametrize("error", [ConnectionError("down"), RuntimeError("boom")])
async def test_a_failing_manager_gives_none_and_never_a_zero(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, FakeManager(error=error))
    assert await document_upload.uploads_last_hour(uuid.uuid4(), uuid.uuid4()) is None
