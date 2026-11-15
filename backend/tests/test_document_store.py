"""
Step 5a-3: the private document store (app/services/document_store.py).

Local files only (tmp_path). No database, no Redis.
"""

import errno
import gc
import importlib
import inspect
import os
import pkgutil
import re
import secrets
import stat
import sys
import uuid
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from starlette.convertors import PathConvertor
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

import app as app_pkg
from app.api.v1 import branding as branding_api
from app.core.config import settings
from app.db.models.tenant import Tenant
from app.main import app
from app.services import document_store as ds
from app.services.document_store import InvalidStorageKey, LocalPrivateStore, ObjectNotFound
from app.services.storage import LocalStorage
from tests.security.upload_assertions import (
    assert_failed_write_leaves_nothing,
    assert_store_enforces_tenant_prefix,
)

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


# ─── Roots ───────────────────────────────────────────────────────────────────

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


# ─── Atomic writes (review of 5a-3, finding 10) ──────────────────────────────

def _files_under(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


async def test_a_failed_write_leaves_no_partial_file_and_no_temp_file(tmp_path: Path) -> None:
    await assert_failed_write_leaves_nothing(LocalPrivateStore(tmp_path / "private"), tmp_path / "private", TENANT_A)


async def test_disk_full_while_writing_leaves_nothing(
    store: LocalPrivateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fdopen = os.fdopen

    class HalfWriter:
        """Writes half of the data, then the disk is full."""

        def __init__(self, fh: Any) -> None:
            self._fh = fh

        def write(self, data: bytes) -> int:
            self._fh.write(data[: len(data) // 2])
            self._fh.flush()
            raise OSError(errno.ENOSPC, "No space left on device")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._fh, name)

        def __enter__(self) -> "HalfWriter":
            return self

        def __exit__(self, *exc: object) -> None:
            self._fh.close()

    monkeypatch.setattr(ds.os, "fdopen", lambda *a, **k: HalfWriter(real_fdopen(*a, **k)))
    key = ds.new_document_key(TENANT_A)
    with pytest.raises(OSError, match="No space"):
        await store.put(TENANT_A, key, b"x" * 1000)
    assert _files_under(tmp_path / "private") == []
    assert await store.get(TENANT_A, key) is None


@pytest.mark.parametrize("failing", ["fsync", "replace"])
async def test_fsync_or_replace_failing_leaves_nothing(
    store: LocalPrivateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(ds.os, failing, boom)
    key = ds.new_document_key(TENANT_A)
    with pytest.raises(OSError, match="I/O error"):
        await store.put(TENANT_A, key, b"data")
    assert _files_under(tmp_path / "private") == []
    assert await store.get(TENANT_A, key) is None


@pytest.mark.parametrize("failing", ["fsync", "replace"])
def test_an_interrupted_write_leaves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """KeyboardInterrupt is not an Exception: the cleanup must not depend on `except Exception`."""

    def interrupt(*_a: Any, **_k: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ds.os, failing, interrupt)
    target = tmp_path / "private" / "t" / "x" / "d" / "obj"
    with pytest.raises(KeyboardInterrupt):
        LocalPrivateStore._write_new(target, b"data")
    assert _files_under(tmp_path / "private") == []


async def test_a_successful_write_leaves_exactly_one_file(store: LocalPrivateStore, tmp_path: Path) -> None:
    key = ds.new_document_key(TENANT_A)
    await store.put(TENANT_A, key, b"payload")
    assert _files_under(tmp_path / "private") == [tmp_path / "private" / key]


async def test_the_temp_file_is_created_exclusively_with_mode_0600_and_synced_before_the_rename(
    store: LocalPrivateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_open, real_fsync, real_replace = os.open, os.fsync, os.replace

    def spy_open(path: Any, flags: int, mode: int = 0o777, **kw: Any) -> int:
        if str(path).endswith(".tmp"):
            assert flags & os.O_CREAT and flags & os.O_EXCL, "the temp file must be created with O_CREAT|O_EXCL"
            assert mode == 0o600, oct(mode)
            events.append("open-temp")
        return real_open(path, flags, mode, **kw)

    def spy_fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def spy_replace(src: Any, dst: Any) -> None:
        assert str(src).endswith(".tmp") and not str(dst).endswith(".tmp")
        events.append("replace")
        real_replace(src, dst)

    monkeypatch.setattr(ds.os, "open", spy_open)
    monkeypatch.setattr(ds.os, "fsync", spy_fsync)
    monkeypatch.setattr(ds.os, "replace", spy_replace)
    await store.put(TENANT_A, ds.new_document_key(TENANT_A), b"payload")
    assert events[0] == "open-temp"
    assert events.index("fsync") < events.index("replace")


async def test_the_temp_file_sits_in_the_same_directory_as_the_target(
    store: LocalPrivateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.replace is atomic only inside one file system."""
    seen: list[tuple[Path, Path]] = []
    real_replace = os.replace
    monkeypatch.setattr(ds.os, "replace", lambda s, d: (seen.append((Path(s), Path(d))), real_replace(s, d))[1])
    await store.put(TENANT_A, ds.new_document_key(TENANT_A), b"payload")
    (src, dst), = seen
    assert src.parent == dst.parent


async def test_put_still_never_overwrites_and_leaves_no_temp_file(store: LocalPrivateStore, tmp_path: Path) -> None:
    key = ds.new_document_key(TENANT_A)
    await store.put(TENANT_A, key, b"one")
    with pytest.raises(FileExistsError):
        await store.put(TENANT_A, key, b"two")
    assert _files_under(tmp_path / "private") == [tmp_path / "private" / key]
    assert await store.get(TENANT_A, key) == b"one"


async def test_directories_are_created_with_mode_0700(
    store: LocalPrivateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode is requested for every directory the store creates (the root, t, the tenant, d)."""
    requested: list[int] = []
    real_mkdir = os.mkdir

    def spy_mkdir(path: Any, mode: int = 0o777, **kw: Any) -> None:
        requested.append(mode)
        real_mkdir(path, mode, **kw)

    monkeypatch.setattr(ds.os, "mkdir", spy_mkdir)
    key = ds.new_document_key(TENANT_A)
    await store.put(TENANT_A, key, b"payload")
    assert requested and set(requested) == {0o700}
    assert len(requested) == 4  # private, t, {tenant}, d
    if sys.platform != "win32":  # Windows ignores POSIX modes: there it is best effort only
        for d in (tmp_path / "private", tmp_path / "private" / "t", (tmp_path / "private" / key).parent):
            assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
        assert stat.S_IMODE((tmp_path / "private" / key).stat().st_mode) == 0o600


# ─── stream() never leaks a handle (finding 11) ──────────────────────────────

@pytest.fixture
def open_handles(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Every file object the store opens for reading, so a test can see which ones are still open."""
    handles: list[Any] = []
    real = ds._open_for_read

    def tracking(path: Path) -> Any:
        fh = real(path)
        handles.append(fh)
        return fh

    monkeypatch.setattr(ds, "_open_for_read", tracking)
    return handles


async def test_an_iterator_that_is_never_started_opens_nothing(
    store: LocalPrivateStore, open_handles: list[Any]
) -> None:
    key = ds.new_document_key(TENANT_A)
    await store.put(TENANT_A, key, b"data" * 1000)
    it = await store.stream(TENANT_A, key)
    assert open_handles == [], "stream() must not open the file before iteration starts"
    del it


async def test_abandoning_an_unstarted_iterator_leaks_no_resource_warning(
    store: LocalPrivateStore, open_handles: list[Any]
) -> None:
    key = ds.new_document_key(TENANT_A)
    await store.put(TENANT_A, key, b"data" * 1000)
    leaked: list[object] = []
    old_hook = sys.unraisablehook
    sys.unraisablehook = lambda u: leaked.append(u.exc_value)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            for _ in range(5):
                await store.stream(TENANT_A, key)  # dropped at once, never iterated
                gc.collect()
    finally:
        sys.unraisablehook = old_hook
    assert not [e for e in leaked if isinstance(e, ResourceWarning)], leaked
    assert all(h.closed for h in open_handles)
    await store.delete(TENANT_A, key)  # Windows refuses to delete a file that is still open


async def test_closing_a_started_iterator_closes_the_file(store: LocalPrivateStore, open_handles: list[Any]) -> None:
    key = ds.new_document_key(TENANT_A)
    await store.put(TENANT_A, key, b"x" * 300_000)
    it = await store.stream(TENANT_A, key, chunk_size=100_000)
    assert len(await anext(it)) == 100_000  # type: ignore[call-overload]
    assert [h.closed for h in open_handles] == [False]
    await it.aclose()  # type: ignore[attr-defined]
    assert [h.closed for h in open_handles] == [True]
    await store.delete(TENANT_A, key)


async def test_a_fully_read_iterator_closes_the_file(store: LocalPrivateStore, open_handles: list[Any]) -> None:
    key = ds.new_document_key(TENANT_A)
    await store.put(TENANT_A, key, b"x" * 250_000)
    chunks = [c async for c in await store.stream(TENANT_A, key, chunk_size=100_000)]
    assert sum(len(c) for c in chunks) == 250_000
    assert [h.closed for h in open_handles] == [True]


async def test_stream_of_a_missing_key_raises_before_anything_is_opened(
    store: LocalPrivateStore, open_handles: list[Any]
) -> None:
    with pytest.raises(ObjectNotFound):
        await store.stream(TENANT_A, ds.new_document_key(TENANT_A))
    assert open_handles == []


async def test_stream_of_a_directory_is_not_found(store: LocalPrivateStore, tmp_path: Path) -> None:
    key = ds.new_document_key(TENANT_A)
    (tmp_path / "private" / key).mkdir(parents=True)
    with pytest.raises(ObjectNotFound):
        await store.stream(TENANT_A, key)


# ─── No public route can serve it ────────────────────────────────────────────

def all_app_routes(application: Any) -> list[Any]:
    """
    Every route of the real app, flattened: plain routes, the children of mounts, and the routes
    of included routers (FastAPI 0.16x hides those behind an _IncludedRouter, which a plain loop
    over app.routes never enters).
    """
    found: list[Any] = []

    def walk(routes: Any) -> None:
        for route in routes:
            if isinstance(route, Mount):
                found.append(route)
                walk(getattr(route, "routes", []))
            elif hasattr(route, "effective_route_contexts"):
                found.extend(route.effective_route_contexts())
            else:
                found.append(route)

    walk(application.routes)
    return found


def test_route_inspection_really_reaches_the_included_routers() -> None:
    """Guard: the old version of the next test saw 6 routes and none of the 83 under /api/v1."""
    paths = [getattr(r, "path", "") for r in all_app_routes(app)]
    assert len(paths) > 60, len(paths)
    assert "/api/v1/auth/register" in paths
    assert "/health" in paths


def test_no_route_or_mount_can_serve_a_directory() -> None:
    """No static mount and no mount at all; no `{x:path}` route; no route path that names the document store."""
    routes = all_app_routes(app)
    assert not [r for r in routes if isinstance(r, Mount)], "a mount can serve files"
    for r in routes:
        assert not isinstance(getattr(r, "app", None), StaticFiles)
        convertors = getattr(r, "param_convertors", {}) or {}
        greedy = [n for n, c in convertors.items() if isinstance(c, PathConvertor)]
        assert not greedy, f"{r.path}: a path converter ({greedy}) can match '/' and so a storage key"
        path = getattr(r, "path", "")
        assert "document_store" not in path and "/d/{" not in path, path


async def test_real_requests_never_return_a_stored_document(
    client: AsyncClient, tenant_a: Tenant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Write a real object to the real document store, then GET every route of the app with the key
    (and its parts) in every path parameter, and a list of static-looking paths. Nothing may return
    the object's bytes.
    """
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "DOCUMENT_STORE_DIR", str(tmp_path / "document_store"))
    ds.get_document_store.cache_clear()
    try:
        sentinel = b"ORIGINAL-UPLOAD-SENTINEL-" + uuid.uuid4().hex.encode()
        key = ds.new_document_key(tenant_a.id)
        await ds.get_document_store().put(tenant_a.id, key, sentinel)
        assert (tmp_path / "document_store" / key).read_bytes() == sentinel  # it is really there
        random_part = key.rsplit("/", 1)[-1]

        urls = [
            "/", f"/{key}", f"/document_store/{key}", f"/static/{key}", f"/files/{key}", f"/uploads/{key}",
            f"/media/{key}", f"/api/v1/{key}", f"/api/v1/documents/{key}", f"/api/v1/public/{key}",
            f"/api/v1/public/branding/media/{key}", f"/api/v1/public/branding/media/{tenant_a.id}/d/{random_part}",
            f"/api/v1/public/branding/media/..%2Fdocument_store%2F{key}", f"/%2e%2e/document_store/{key}",
            f"/api/v1/public/branding/media/%2e%2e/%2e%2e/document_store/{key}",
        ]
        values = {"tenant_id": str(tenant_a.id), "document_id": random_part, "filename": random_part,
                  "asset_type": "d", "key": key}
        for r in all_app_routes(app):
            path = getattr(r, "path", None)
            if not path or "GET" not in (getattr(r, "methods", None) or set()):
                continue
            for candidate in (key, random_part, str(tenant_a.id)):
                urls.append(re.sub(r"\{(\w+)(?::\w+)?\}", lambda m, c=candidate: quote(values.get(m.group(1), c), safe=""), path))
        assert len(urls) > 50

        for url in dict.fromkeys(urls):
            res = await client.get(url, headers={"X-Tenant-Slug": tenant_a.slug})
            assert sentinel not in res.content, f"GET {url} returned the stored document"
            assert res.status_code != 200 or res.headers.get("content-type", "").startswith(("application/json", "text/html")), url
    finally:
        ds.get_document_store.cache_clear()


async def test_public_media_route_does_not_leak_a_document_into_a_response_or_listing(
    client: AsyncClient, tenant_a: Tenant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The object is written for real. A positive control first proves the media route DOES serve a
    file that sits in the public store, so the negative checks below can fail.
    """
    public = LocalStorage(tmp_path / "uploads")
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(settings, "DOCUMENT_STORE_DIR", str(tmp_path / "document_store"))
    monkeypatch.setattr(branding_api, "get_storage", lambda: public)
    ds.get_document_store.cache_clear()
    try:
        logo_key = f"{tenant_a.id}/logo/{uuid.uuid4().hex}.png"
        await public.put(logo_key, b"PUBLIC-LOGO-BYTES", "image/png")
        control = await client.get(f"/api/v1/public/branding/media/{logo_key}", headers={"X-Tenant-Slug": tenant_a.slug})
        assert control.status_code == 200 and control.content == b"PUBLIC-LOGO-BYTES"

        sentinel = b"ORIGINAL-UPLOAD-SENTINEL"
        key = ds.new_document_key(tenant_a.id)
        await ds.get_document_store().put(tenant_a.id, key, sentinel)
        random_part = key.rsplit("/", 1)[-1]

        for url in (
            f"/api/v1/public/branding/media/{key}",
            f"/api/v1/public/branding/media/{tenant_a.id}/d/{random_part}",
            f"/api/v1/public/branding/media/{tenant_a.id}/logo/{random_part}.png",
            f"/api/v1/public/branding/media/..%2Fdocument_store%2F{key}",
            f"/api/v1/public/branding/media/{tenant_a.id}/logo/..%2F..%2F..%2Fdocument_store%2F{key}",
        ):
            res = await client.get(url, headers={"X-Tenant-Slug": tenant_a.slug})
            assert res.status_code in (404, 422), (url, res.status_code)
            assert sentinel not in res.content, url

        # The public branding document lists the public store's keys: the document key is not in it
        listing = await client.get("/api/v1/public/branding", headers={"X-Tenant-Slug": tenant_a.slug})
        assert key.encode() not in listing.content and random_part.encode() not in listing.content
        # The public store's own directory does not contain the document, and its API cannot read it
        public_files = [p.name for p in (tmp_path / "uploads").rglob("*") if p.is_file()]
        assert public_files == [logo_key.rsplit("/", 1)[-1]]
        assert await public.get(key) is None
    finally:
        ds.get_document_store.cache_clear()


# ─── No public URL can be built for a document (replaces the by-construction hasattr test) ───

# Every function or method of the app that builds a URL for a stored object. A new one must be
# added here AND must pass test_every_public_url_builder_rejects_a_document_store_key.
PUBLIC_URL_BUILDERS = {"LocalStorage.get_url"}


def _discover_url_builders() -> set[str]:
    found: set[str] = set()
    for mod in pkgutil.walk_packages(app_pkg.__path__, "app."):
        module = importlib.import_module(mod.name)
        for name, member in vars(module).items():
            if getattr(member, "__module__", None) != module.__name__:
                continue
            if inspect.isfunction(member) and name.endswith("url") and "storage" in module.__name__:
                found.add(name)
            if inspect.isclass(member):
                for meth_name, meth in vars(member).items():
                    if inspect.isfunction(meth) and meth_name.endswith("url") and "storage" in module.__name__:
                        found.add(f"{name}.{meth_name}")
    return found


def test_every_public_url_builder_of_the_storage_layer_is_known_to_this_test() -> None:
    """A new URL builder in a storage module fails here until it is listed and covered below."""
    found = _discover_url_builders()
    unexpected = found - PUBLIC_URL_BUILDERS - {"ObjectStore.get_url"}
    assert not unexpected, f"new URL builders need a document-key test: {sorted(unexpected)}"
    assert "LocalStorage.get_url" in found


def test_every_public_url_builder_rejects_a_document_store_key(tmp_path: Path) -> None:
    keys = [
        ds.new_document_key(TENANT_A),
        "/" + ds.new_document_key(TENANT_A),  # leading slash is stripped by the builder
        ds.new_document_key(TENANT_A).replace("/", "\\"),
        f"t/{TENANT_A}/d/{secrets.token_hex(16)}",
    ]
    public = LocalStorage(tmp_path / "uploads")
    for key in keys:
        with pytest.raises((ValueError, HTTPException)):
            public.get_url(key)
    # A real branding key still gets its URL
    assert public.get_url(f"{TENANT_A}/logo/{secrets.token_hex(16)}.png").startswith("/api/v1/public/branding/media/")
    # Nor does the private store offer one
    assert not [n for n in dir(LocalPrivateStore) if "url" in n.lower()]
    assert not [n for n in dir(ds.PrivateObjectStore) if "url" in n.lower()]
