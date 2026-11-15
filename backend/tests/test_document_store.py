"""
Step 5a-3: the private document store (app/services/document_store.py).

Local files only (tmp_path). No database, no Redis.
"""

import re
import uuid
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Mount

from app.core.config import settings
from app.main import app
from app.services import document_store as ds
from app.services.document_store import InvalidStorageKey, LocalPrivateStore, ObjectNotFound
from app.services.storage import LocalStorage
from tests.security.upload_assertions import assert_store_enforces_tenant_prefix

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()


@pytest.fixture
def store(tmp_path: Path) -> LocalPrivateStore:
    return LocalPrivateStore(tmp_path / "private")


def test_keys_are_tenant_prefixed_and_random() -> None:
    keys = {ds.new_document_key(TENANT_A) for _ in range(50)}
    assert len(keys) == 50
    for key in keys:
        assert re.fullmatch(rf"t/{TENANT_A}/d/[0-9a-f]{{32}}", key)
        ds.validate_key(TENANT_A, key)


async def test_put_get_roundtrip_and_layout(store: LocalPrivateStore, tmp_path: Path) -> None:
    key = ds.new_document_key(TENANT_A)
    assert await store.put(TENANT_A, key, b"hello") == key
    assert await store.get(TENANT_A, key) == b"hello"
    assert (tmp_path / "private" / key).read_bytes() == b"hello"


async def test_get_missing_returns_none(store: LocalPrivateStore) -> None:
    assert await store.get(TENANT_A, ds.new_document_key(TENANT_A)) is None


async def test_put_never_overwrites(store: LocalPrivateStore) -> None:
    key = ds.new_document_key(TENANT_A)
    await store.put(TENANT_A, key, b"one")
    with pytest.raises(FileExistsError):
        await store.put(TENANT_A, key, b"two")
    assert await store.get(TENANT_A, key) == b"one"


async def test_delete_is_idempotent(store: LocalPrivateStore) -> None:
    key = ds.new_document_key(TENANT_A)
    await store.put(TENANT_A, key, b"x")
    await store.delete(TENANT_A, key)
    assert await store.get(TENANT_A, key) is None
    await store.delete(TENANT_A, key)  # second delete: no error
    await store.delete(TENANT_A, ds.new_document_key(TENANT_A))  # never existed: no error


async def test_stream_yields_chunks_and_closes(store: LocalPrivateStore) -> None:
    key = ds.new_document_key(TENANT_A)
    data = bytes(range(256)) * 1000  # 256,000 bytes
    await store.put(TENANT_A, key, data)
    it = await store.stream(TENANT_A, key, chunk_size=100_000)
    chunks = [c async for c in it]
    assert [len(c) for c in chunks] == [100_000, 100_000, 56_000]
    assert b"".join(chunks) == data
    await store.delete(TENANT_A, key)  # file handle was closed: Windows would refuse otherwise


async def test_stream_missing_raises_before_iteration(store: LocalPrivateStore) -> None:
    with pytest.raises(ObjectNotFound):
        await store.stream(TENANT_A, ds.new_document_key(TENANT_A))


async def test_tenant_prefix_and_traversal_are_enforced(store: LocalPrivateStore) -> None:
    await assert_store_enforces_tenant_prefix(store, TENANT_A, TENANT_B)


async def test_traversal_never_touches_files_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "private"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"do not touch")
    store = LocalPrivateStore(root)
    for bad in ("../outside.txt", f"t/{TENANT_A}/d/../../../outside.txt", str(outside), "..\\outside.txt"):
        with pytest.raises(InvalidStorageKey):
            await store.get(TENANT_A, bad)
        with pytest.raises(InvalidStorageKey):
            await store.delete(TENANT_A, bad)
        with pytest.raises(InvalidStorageKey):
            await store.put(TENANT_A, bad, b"x")
    assert outside.read_bytes() == b"do not touch"


async def test_invalid_key_error_does_not_echo_the_key(store: LocalPrivateStore) -> None:
    with pytest.raises(InvalidStorageKey) as exc:
        await store.get(TENANT_A, "../super-secret-name")
    assert "super-secret-name" not in str(exc.value)


async def test_non_string_key_rejected(store: LocalPrivateStore) -> None:
    with pytest.raises(InvalidStorageKey):
        await store.get(TENANT_A, None)  # type: ignore[arg-type]


# ─── No public route can serve it ────────────────────────────────────────────

def test_store_has_no_url_method(store: LocalPrivateStore) -> None:
    for name in dir(store):
        assert "url" not in name.lower(), name
    assert not hasattr(store, "get_url")
    assert not hasattr(ds.PrivateObjectStore, "get_url")


def test_document_root_is_separate_from_the_public_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ds.get_document_store.cache_clear()
    try:
        public = tmp_path / "uploads"
        for private in (public, public / "docs", tmp_path):
            monkeypatch.setattr(settings, "UPLOAD_DIR", str(public))
            monkeypatch.setattr(settings, "DOCUMENT_STORE_DIR", str(private))
            ds.get_document_store.cache_clear()
            with pytest.raises(RuntimeError):
                ds.get_document_store()
        monkeypatch.setattr(settings, "DOCUMENT_STORE_DIR", str(tmp_path / "document_store"))
        ds.get_document_store.cache_clear()
        assert isinstance(ds.get_document_store(), LocalPrivateStore)
    finally:
        ds.get_document_store.cache_clear()


async def test_public_store_cannot_read_document_keys(tmp_path: Path) -> None:
    private = LocalPrivateStore(tmp_path / "document_store")
    public = LocalStorage(tmp_path / "uploads")
    key = ds.new_document_key(TENANT_A)
    await private.put(TENANT_A, key, b"original upload")
    assert await public.get(key) is None
    assert await private.get(TENANT_A, key) == b"original upload"


def test_no_route_or_mount_serves_the_document_root() -> None:
    """The app has no static mount, and no route path mentions documents' storage or keys."""
    assert not [r for r in app.routes if isinstance(r, Mount)]
    for route in app.routes:
        if isinstance(route, APIRoute):
            assert "document_store" not in route.path
            assert "/d/{" not in route.path


async def test_public_media_route_cannot_reach_a_document_key(client, tenant_a) -> None:  # type: ignore[no-untyped-def]
    key = ds.new_document_key(tenant_a.id)
    random_part = key.rsplit("/", 1)[-1]
    for url in (
        f"/api/v1/public/branding/media/{key}",
        f"/api/v1/public/branding/media/{tenant_a.id}/d/{random_part}",
        f"/api/v1/public/branding/media/{tenant_a.id}/logo/{random_part}.png",
        f"/api/v1/public/branding/media/..%2Fdocument_store%2F{key}",
    ):
        res = await client.get(url, headers={"X-Tenant-Slug": tenant_a.slug})
        assert res.status_code in (404, 422), (url, res.status_code)
        assert b"original" not in res.content
