"""
app/ml/lock.py
──────────────
The model registry: reads and validates `app/ml/models.lock.json` and verifies a model directory against it
(design A1, "Pinning and safety"). Standard library only, so the operator script `fetch_model` and the loader
can both import it without pulling in onnxruntime.

A lock entry says exactly which files make up a model (path, size, sha256), which repository revision they
come from, and how to use them (dimension, pooling, prefixes, maximum tokens). The loader reads ONLY local
files and refuses to start on anything that does not match: a missing file, a wrong size or hash, an extra
file, a symlink, or an entry marked UNVERIFIED. Nothing here ever opens a network connection.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_LOCK_PATH = Path(__file__).with_name("models.lock.json")

LOCK_VERSION = 1
STATUS_PINNED = "pinned"
STATUS_UNVERIFIED = "UNVERIFIED"
POOLINGS = ("mean", "cls")
# The embedder classes a lock entry may name. Each class fixes its own pooling and prefixes, and the entry must
# agree with them, so editing the lock can never silently drop the `query: ` / `passage: ` prefixes.
EMBEDDER_CLASSES: dict[str, tuple[str, dict[str, str]]] = {
    "OnnxE5Embedder": ("mean", {"query": "query: ", "passage": "passage: "}),
    "OnnxBgeM3Embedder": ("cls", {"query": "", "passage": ""}),
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_ENTRY_KEYS = {
    "repo", "revision", "embedder", "model_file", "tokenizer_file", "files", "dim", "max_tokens", "pooling",
    "pad_token", "prefixes", "license", "status", "notes",
}
_HASH_CHUNK = 1024 * 1024


class ModelLoadError(Exception):
    """The embedding model could not be loaded. The worker (and the API's query embedder) refuse to start."""


class ModelLockError(ModelLoadError):
    """`models.lock.json` is malformed, or names a model that does not exist."""


class ModelVerificationError(ModelLoadError):
    """The files on disk do not match the lock entry (or the entry is UNVERIFIED)."""


@dataclass(frozen=True)
class LockedFile:
    path: str  # posix, relative to the model directory
    sha256: str
    size: int


@dataclass(frozen=True)
class ModelSpec:
    name: str  # the short name, the key in the lock file ("e5-small")
    repo: str
    revision: str | None  # full commit SHA; None only while the entry is UNVERIFIED
    embedder: str
    model_file: str | None
    tokenizer_file: str | None
    files: tuple[LockedFile, ...]
    dim: int
    max_tokens: int
    pooling: str
    pad_token: str
    prefixes: dict[str, str]
    license: str
    status: str
    notes: str = ""

    @property
    def model_key(self) -> str:
        """The value stored in `chunk_embeddings.embedding_model`: repo and pinned commit (model_keys.py)."""
        if self.revision is None:
            raise ModelLockError(f"model {self.name!r} has no pinned revision (status {self.status})")
        return f"{self.repo}@{self.revision}"

    @property
    def directory_name(self) -> str:
        return self.name


@dataclass(frozen=True)
class ModelLock:
    models: dict[str, ModelSpec]

    def get(self, name_or_key: str) -> ModelSpec:
        """Find a model by its short name or by its stored key (`repo@revision`)."""
        if name_or_key in self.models:
            return self.models[name_or_key]
        for spec in self.models.values():
            if spec.revision is not None and spec.model_key == name_or_key:
                return spec
        raise ModelLockError(f"unknown model {name_or_key!r}; known: {', '.join(sorted(self.models))}")


# ── Parsing ──────────────────────────────────────────────────────────────────


def is_safe_relative_path(path: object) -> bool:
    """A plain relative posix path: no `..`, no absolute path, no drive letter, no backslash, no empty part."""
    if not isinstance(path, str) or not path or "\\" in path or "\0" in path:
        return False
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return False
    parts = path.split("/")
    return all(p not in ("", ".", "..") for p in parts)


def _fail(name: str, message: str) -> ModelLockError:
    return ModelLockError(f"models.lock.json, model {name!r}: {message}")


def _parse_files(name: str, raw: object) -> tuple[LockedFile, ...]:
    if not isinstance(raw, list):
        raise _fail(name, "'files' must be a list")
    out: list[LockedFile] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise _fail(name, "each file needs exactly path, sha256 and size")
        path, digest, size = item["path"], item["sha256"], item["size"]
        if not is_safe_relative_path(path):
            raise _fail(name, f"unsafe file path {path!r}")
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise _fail(name, f"sha256 of {path} must be 64 lower-case hex characters")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise _fail(name, f"size of {path} must be a positive integer")
        if path in seen:
            raise _fail(name, f"duplicate file path {path}")
        seen.add(path)
        out.append(LockedFile(path=path, sha256=digest, size=size))
    return tuple(out)


def _positive_int(name: str, key: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _fail(name, f"{key} must be a positive integer")
    return value


def parse_entry(name: str, raw: object) -> ModelSpec:
    if not isinstance(raw, dict):
        raise _fail(name, "the entry must be an object")
    unknown = set(raw) - _ENTRY_KEYS
    if unknown:
        raise _fail(name, f"unknown keys {sorted(unknown)}")
    required = {"repo", "embedder", "files", "dim", "max_tokens", "pooling", "prefixes", "license", "status"}
    if missing := required - set(raw):
        raise _fail(name, f"missing keys {sorted(missing)}")

    status = raw["status"]
    if status not in (STATUS_PINNED, STATUS_UNVERIFIED):
        raise _fail(name, f"status must be {STATUS_PINNED!r} or {STATUS_UNVERIFIED!r}")
    unverified = status == STATUS_UNVERIFIED

    repo = raw["repo"]
    if not isinstance(repo, str) or not re.match(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$", repo):
        raise _fail(name, "repo must look like owner/name")
    revision = raw.get("revision")
    if revision is None:
        if not unverified:
            raise _fail(name, "a pinned entry needs a full 40-character commit revision")
    elif not isinstance(revision, str) or not _REVISION.match(revision):
        raise _fail(name, "revision must be a full 40-character lower-case commit SHA, not a branch or tag")

    embedder = raw["embedder"]
    if embedder not in EMBEDDER_CLASSES:
        raise _fail(name, f"embedder must be one of {sorted(EMBEDDER_CLASSES)}")
    class_pooling, class_prefixes = EMBEDDER_CLASSES[embedder]
    pooling = raw["pooling"]
    if pooling not in POOLINGS:
        raise _fail(name, f"pooling must be one of {POOLINGS}")
    if pooling != class_pooling:
        raise _fail(name, f"{embedder} uses {class_pooling} pooling, the entry says {pooling}")
    prefixes = raw["prefixes"]
    if (not isinstance(prefixes, dict) or set(prefixes) != {"query", "passage"}
            or not all(isinstance(v, str) for v in prefixes.values())):
        raise _fail(name, "prefixes must be an object with string values 'query' and 'passage'")
    if prefixes != class_prefixes:
        raise _fail(name, f"the prefixes do not match what {embedder} adds: {class_prefixes!r}")

    files = _parse_files(name, raw["files"])
    model_file, tokenizer_file = raw.get("model_file"), raw.get("tokenizer_file")
    paths = {f.path for f in files}
    if unverified and not files:
        pass  # nothing to cross-check yet: the entry only records what is known
    else:
        for key, value in (("model_file", model_file), ("tokenizer_file", tokenizer_file)):
            if value not in paths:
                raise _fail(name, f"{key} {value!r} is not one of the listed files")
    if not isinstance(raw["license"], str) or not raw["license"]:
        raise _fail(name, "license must be a non-empty string")
    pad_token = raw.get("pad_token", "<pad>")
    if not isinstance(pad_token, str) or not pad_token:
        raise _fail(name, "pad_token must be a non-empty string")

    return ModelSpec(
        name=name, repo=repo, revision=revision, embedder=embedder, model_file=model_file,
        tokenizer_file=tokenizer_file, files=files, dim=_positive_int(name, "dim", raw["dim"]),
        max_tokens=_positive_int(name, "max_tokens", raw["max_tokens"]), pooling=pooling, pad_token=pad_token,
        prefixes=dict(prefixes), license=raw["license"], status=status, notes=str(raw.get("notes", "")),
    )


def load_lock(path: Path | None = None) -> ModelLock:
    """Read and validate the lock file (the packaged one by default)."""
    lock_path = path or DEFAULT_LOCK_PATH
    try:
        raw: Any = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModelLockError(f"cannot read {lock_path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != LOCK_VERSION or not isinstance(raw.get("models"), dict):
        raise ModelLockError(f"{lock_path}: expected an object with version {LOCK_VERSION} and a 'models' object")
    return ModelLock(models={name: parse_entry(name, entry) for name, entry in raw["models"].items()})


# ── Verification ─────────────────────────────────────────────────────────────


def hash_file(path: Path) -> tuple[int, str]:
    """Return (size, sha256 hex) of a file, reading it in 1 MB pieces."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_HASH_CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def _present_files(directory: Path) -> set[str]:
    """Every entry under `directory` as a posix path. A symlink anywhere is refused."""
    found: set[str] = set()
    for p in directory.rglob("*"):
        if p.is_symlink():
            raise ModelVerificationError(f"{p.relative_to(directory).as_posix()}: symlinks are not allowed")
        if p.is_file():
            found.add(p.relative_to(directory).as_posix())
    return found


def verify_model_dir(spec: ModelSpec, directory: Path) -> None:
    """Check that `directory` holds exactly the files of `spec`, with the locked sizes and sha256 values.

    Order: status, then the directory listing (missing, unexpected, symlinks), then every size (cheap), then
    every sha256. Raises ModelVerificationError at the first problem; returns None when everything matches.
    """
    if spec.status != STATUS_PINNED:
        raise ModelVerificationError(
            f"model {spec.name!r} is marked {spec.status} in models.lock.json and will not be loaded; "
            "review its hashes with `python -m app.scripts.fetch_model <name> --print-hashes` and pin them first"
        )
    if not directory.is_dir():
        raise ModelVerificationError(
            f"model directory {directory} is missing; fetch the model with "
            f"`python -m app.scripts.fetch_model {spec.name}`"
        )
    present = _present_files(directory)
    locked = {f.path: f for f in spec.files}
    missing = sorted(set(locked) - present)
    if missing:
        raise ModelVerificationError(f"model {spec.name!r}: missing files {missing} under {directory}")
    extra = sorted(present - set(locked))
    if extra:
        # Also what keeps pickles, custom-op libraries and undeclared ONNX external data out of the directory.
        raise ModelVerificationError(f"model {spec.name!r}: unexpected files {extra} under {directory}")
    for f in spec.files:
        actual = (directory / f.path).stat().st_size
        if actual != f.size:
            raise ModelVerificationError(f"{f.path}: size {actual} differs from the locked size {f.size}")
    for f in spec.files:
        _size, digest = hash_file(directory / f.path)
        if digest != f.sha256:
            raise ModelVerificationError(f"{f.path}: sha256 {digest} differs from the locked sha256 {f.sha256}")
