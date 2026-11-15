"""
app/services/document_store.py
──────────────────────────────
The PRIVATE object store for uploaded documents (original files).

Not the branding store in `services/storage.py`:
- it is a separate instance with its own root directory (`DOCUMENT_STORE_DIR`);
- it has no `get_url` method, so no code can ever build a public URL for a document. Files leave
  only through an authenticated route that streams them (design A4);
- every call names the caller's tenant, and a key is accepted only in the exact shape
  `t/{tenant_id}/d/{random 32 hex}` under THAT tenant. Anything else (another tenant's key,
  `..`, absolute paths, backslashes, extra segments) raises InvalidStorageKey before any file
  system call. The resolved path is checked once more against the root (defence in depth).

Keys are random and never shown to clients; the route finds the key through the database row
(which RLS scopes to the tenant), never from client input.

Later the local backend is replaced by Supabase Storage (a private `documents` bucket, backend
service key) behind the same `PrivateObjectStore` interface: the key format, the tenant check and
the "no URL" rule stay, only the I/O inside the class changes. The storage outbox
(`storage_deletions`) already assumes object deletes are idempotent, as `delete` is here.
"""

import asyncio
import contextlib
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Protocol

from app.core.config import settings

DEFAULT_CHUNK_SIZE = 64 * 1024

_KEY_RE = re.compile(r"^t/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/d/[0-9a-f]{32}$")


class InvalidStorageKey(ValueError):
    """The key is malformed or does not belong to the caller's tenant. The key is never echoed."""

    def __init__(self) -> None:
        super().__init__("Invalid storage key.")


class ObjectNotFound(Exception):
    """The key is valid but no object exists under it."""


class PrivateObjectStore(Protocol):
    """Interface of the private document store. Deliberately has NO url method."""

    async def put(self, tenant_id: uuid.UUID, key: str, data: bytes) -> str: ...

    async def get(self, tenant_id: uuid.UUID, key: str) -> bytes | None: ...

    async def delete(self, tenant_id: uuid.UUID, key: str) -> None: ...

    async def stream(
        self, tenant_id: uuid.UUID, key: str, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> AsyncIterator[bytes]: ...


def new_document_key(tenant_id: uuid.UUID) -> str:
    """A fresh random key under the tenant prefix: t/{tenant_id}/d/{32 hex}."""
    return f"t/{tenant_id}/d/{secrets.token_hex(16)}"


def _check_shape(key: str) -> None:
    if not isinstance(key, str) or _KEY_RE.fullmatch(key) is None:
        raise InvalidStorageKey()


def _check_tenant_prefix(tenant_id: uuid.UUID, key: str) -> None:
    """The key must live under the caller's own tenant prefix."""
    if not key.startswith(f"t/{tenant_id}/"):
        raise InvalidStorageKey()


def validate_key(tenant_id: uuid.UUID, key: str) -> None:
    _check_shape(key)
    _check_tenant_prefix(tenant_id, key)


class LocalPrivateStore:
    """Local filesystem backend. One file per key under `root`, created on first write."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def _path(self, tenant_id: uuid.UUID, key: str) -> Path:
        validate_key(tenant_id, key)
        target = (self.root / key).resolve()
        if not target.is_relative_to(self.root):
            raise InvalidStorageKey()
        return target

    async def put(self, tenant_id: uuid.UUID, key: str, data: bytes) -> str:
        target = self._path(tenant_id, key)
        await asyncio.to_thread(self._write_new, target, data)
        return key

    @staticmethod
    def _write_new(target: Path, data: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        # "xb": a key is written once; a collision of two random keys must fail, not overwrite
        with open(target, "xb") as fh:
            fh.write(data)

    async def get(self, tenant_id: uuid.UUID, key: str) -> bytes | None:
        target = self._path(tenant_id, key)
        try:
            return await asyncio.to_thread(target.read_bytes)
        except (FileNotFoundError, IsADirectoryError):
            return None

    async def delete(self, tenant_id: uuid.UUID, key: str) -> None:
        """Idempotent: deleting a missing object is not an error."""
        target = self._path(tenant_id, key)
        with contextlib.suppress(FileNotFoundError):
            await asyncio.to_thread(target.unlink)

    async def stream(
        self, tenant_id: uuid.UUID, key: str, chunk_size: int = DEFAULT_CHUNK_SIZE
    ) -> AsyncIterator[bytes]:
        """
        Open the object and return an async iterator over its chunks. A missing object raises
        ObjectNotFound HERE (before any response header is sent), not while iterating.
        The file is closed when the iterator finishes or is closed.
        """
        target = self._path(tenant_id, key)
        try:
            fh = await asyncio.to_thread(open, target, "rb")
        except (FileNotFoundError, IsADirectoryError):
            raise ObjectNotFound() from None
        return self._iterate(fh, chunk_size)

    @staticmethod
    async def _iterate(fh: BinaryIO, chunk_size: int) -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await asyncio.to_thread(fh.read, chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            fh.close()


def _assert_not_public_root(root: Path) -> None:
    """The private root must not be, contain or sit inside the public branding upload root."""
    public = Path(settings.UPLOAD_DIR).resolve()
    if root == public or root.is_relative_to(public) or public.is_relative_to(root):
        raise RuntimeError("DOCUMENT_STORE_DIR must be separate from UPLOAD_DIR (the public branding store).")


@lru_cache(maxsize=1)
def get_document_store() -> PrivateObjectStore:
    """The process-wide document store. The directory is created on the first write, not here."""
    root = Path(settings.DOCUMENT_STORE_DIR).resolve()
    _assert_not_public_root(root)
    return LocalPrivateStore(root)
