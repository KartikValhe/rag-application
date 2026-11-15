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

Writes are atomic: the bytes go to a temp file in the SAME directory (created with O_CREAT|O_EXCL,
mode 0600), are fsynced, and only then renamed onto the key with os.replace. A reader therefore sees
the whole object or no object, and any failure (disk full, interrupt) removes the temp file.
Directories are created with mode 0700. File modes are a POSIX feature: on Windows (development
only) os.mkdir and os.open ignore them, so there they are best effort and the real protection is the
user profile's ACL.
"""

import asyncio
import contextlib
import errno
import os
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


_DOCUMENT_KEY_PREFIX_RE = re.compile(r"^t/[0-9a-fA-F-]{36}/d/")


def is_document_key(key: str) -> bool:
    """
    True when `key` looks like a document-store key (t/{tenant}/d/...), however it is spelled
    (backslashes, a leading slash). Public stores use it to refuse to build a URL for one.
    """
    return _DOCUMENT_KEY_PREFIX_RE.match(key.replace("\\", "/").lstrip("/")) is not None


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


def _open_for_read(path: Path) -> BinaryIO:
    return open(path, "rb")


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
    def _ensure_private_dirs(directory: Path) -> None:
        """Create every missing directory down to `directory` with mode 0700 (mkdir(parents=True) only applies the mode to the last one)."""
        missing: list[Path] = []
        current = directory
        while not current.exists():
            missing.append(current)
            current = current.parent
        for path in reversed(missing):
            with contextlib.suppress(FileExistsError):  # a concurrent writer created it first
                os.mkdir(path, 0o700)

    @staticmethod
    def _write_new(target: Path, data: bytes) -> None:
        """
        Write `data` to a temp file next to `target`, fsync it, then rename it onto `target`.
        Never overwrites: an existing object raises FileExistsError (keys are 128 random bits, so a
        collision between the check and the rename is not a practical concern). On ANY failure,
        including KeyboardInterrupt, the temp file is removed and `target` does not exist.
        """
        LocalPrivateStore._ensure_private_dirs(target.parent)
        if target.exists():
            raise FileExistsError(errno.EEXIST, "object already exists")
        temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        fd = os.open(temp, flags, 0o600)
        try:
            try:
                fh = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp)
            raise
        LocalPrivateStore._sync_directory(target.parent)

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        """Make the rename durable. POSIX only; best effort (Windows cannot open a directory like this)."""
        if os.name != "posix":
            return
        with contextlib.suppress(OSError):
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

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
        Return an async iterator over the object's chunks. A missing object raises ObjectNotFound
        HERE (before any response header is sent), not while iterating. Only the existence is
        checked here: the file is opened INSIDE the iterator when iteration starts and closed in its
        `finally`, so an iterator that is dropped before it ever starts holds no file handle.
        """
        target = self._path(tenant_id, key)
        if not await asyncio.to_thread(target.is_file):
            raise ObjectNotFound()
        return self._iterate(target, chunk_size)

    @staticmethod
    async def _iterate(target: Path, chunk_size: int) -> AsyncIterator[bytes]:
        try:
            fh = await asyncio.to_thread(_open_for_read, target)
        except (FileNotFoundError, IsADirectoryError):
            # Deleted between the existence check and the first read
            raise ObjectNotFound() from None
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
