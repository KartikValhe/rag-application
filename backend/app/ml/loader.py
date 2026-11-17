"""
app/ml/loader.py
────────────────
Loads an embedder from LOCAL, verified files (design A1) and keeps one instance per process for the API.

`load_embedder` reads only files under `<model root>/<model name>/`. Before onnxruntime or the tokenizer sees a
byte it re-verifies every file against `models.lock.json` (exact file list, size, sha256, no symlinks, status
pinned): a missing or changed file, an extra file or an UNVERIFIED entry stops the start-up with
`ModelLoadError`. It never opens a network connection, and it loads only the ONNX protobuf and
`tokenizer.json`: no pickle, no `.bin` or `.pt`, no custom-op library, no `trust_remote_code`. ONNX external
data (weights in a side file) is refused unless the lock entry lists that file with its hash; since every file
must be listed, an undeclared side file is simply an unexpected file, and a model that points at a missing one
fails when onnxruntime opens it.

Known limit: the files are verified and then opened by path, so someone who can write to the model directory
between the two steps could swap a file. That person already controls the host; the directory should be
read-only for the service user (production images bake it in, see the README).

The API embeds queries lazily (design A5): nothing is loaded at import or at start-up. `get_embedder()` builds
the instance on first use under a lock, once per process; a failed build is not cached, so a later call can
retry. `get_embedder_async()` and `embed_query_async()` run the blocking work (hash verification of 470 MB,
loading, inference) on a one-thread executor so the event loop stays free. Memory: about 0.5 GB per API
process for e5-small, so run few API workers (design A5, C3).
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import structlog

from app.core.config import settings
from app.ml.embedder import Embedder, OnnxBgeM3Embedder, OnnxE5Embedder, OnnxEmbedderBase
from app.ml.lock import DEFAULT_LOCK_PATH, ModelLoadError, load_lock, verify_model_dir

logger = structlog.get_logger(__name__)

_CLASSES: dict[str, type[OnnxEmbedderBase]] = {
    "OnnxE5Embedder": OnnxE5Embedder,
    "OnnxBgeM3Embedder": OnnxBgeM3Embedder,
}


def load_embedder(
    name: str,
    model_root: Path,
    *,
    lock_path: Path | None = None,
    batch_size: int = 16,
    threads: int = 1,
) -> Embedder:
    """Verify `<model_root>/<name>/` against the lock and return a ready embedder (blocking)."""
    spec = load_lock(lock_path or DEFAULT_LOCK_PATH).get(name)
    directory = Path(model_root) / spec.directory_name
    verify_model_dir(spec, directory)
    if spec.model_file is None or spec.tokenizer_file is None:  # cannot happen for a pinned entry
        raise ModelLoadError(f"model {spec.name!r} does not name its model and tokenizer files")
    cls = _CLASSES[spec.embedder]
    try:
        embedder = cls(
            directory / spec.model_file,
            directory / spec.tokenizer_file,
            model_id=spec.model_key,
            dim=spec.dim,
            max_tokens=spec.max_tokens,
            batch_size=batch_size,
            intra_op_threads=threads,
            pad_token=spec.pad_token,
        )
        probe = embedder.embed_query("start-up check")  # catches a wrong dimension before any real work
    except Exception as exc:
        raise ModelLoadError(
            f"model {spec.name!r} failed to load or to pass its start-up check (locked dim {spec.dim}): {exc}"
        ) from exc
    if len(probe) != spec.dim:
        raise ModelLoadError(f"model {spec.name!r} returned dim {len(probe)}, the lock says {spec.dim}")
    logger.info("embedder_loaded", model=spec.name, model_key=spec.model_key, dim=spec.dim, batch_size=batch_size)
    return embedder


# ── One lazy instance per process (the API's query embedder) ──────────────────

_lock = threading.Lock()
_instance: Embedder | None = None
# One worker thread: queries are short, and a single thread serialises the model load and the first calls.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="embedder")


def _build_embedder() -> Embedder:
    """Build the process-wide embedder from the settings. Blocking; call through `get_embedder`."""
    if not settings.EMBEDDING_MODEL_DIR:
        raise ModelLoadError(
            "EMBEDDING_MODEL_DIR is not set: point it at the directory that holds the verified model files "
            "(python -m app.scripts.fetch_model e5-small --out <dir>)"
        )
    return load_embedder(
        settings.EMBEDDING_MODEL_ID,
        Path(settings.EMBEDDING_MODEL_DIR),
        lock_path=DEFAULT_LOCK_PATH,
        batch_size=settings.EMBEDDING_BATCH_SIZE,
        threads=settings.EMBEDDING_THREADS,
    )


def get_embedder() -> Embedder:
    """The process-wide embedder, built on first use (thread safe, once per process, failures not cached)."""
    global _instance
    if _instance is not None:
        return _instance
    with _lock:
        if _instance is None:
            _instance = _build_embedder()
        return _instance


async def get_embedder_async() -> Embedder:
    return await asyncio.get_running_loop().run_in_executor(_executor, get_embedder)


async def embed_query_async(text: str) -> list[float]:
    """Embed one query off the event loop (loads the model first if this is the first call)."""
    return await asyncio.get_running_loop().run_in_executor(_executor, lambda: get_embedder().embed_query(text))


def embedder_is_loaded() -> bool:
    return _instance is not None


def reset_embedder_for_tests() -> None:
    global _instance
    with _lock:
        _instance = None
