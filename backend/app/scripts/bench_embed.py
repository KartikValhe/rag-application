"""
python -m app.scripts.bench_embed [--model e5-small] [--model-dir DIR] [--texts 64] [--tokens 380] [--batch-sizes 1,8,16,32]

A MANUAL benchmark of the embedder (design C3: the numbers there are estimates until this runs; not a test, not
part of CI). For each batch size it embeds the same N passages of about `--tokens` tokens (built from
tests/fixtures/parity_sentences.txt) and prints texts per second, tokens per second and the process's peak
resident memory so far (peak RSS only grows, so read the last row as the worst case). One warm-up run is
excluded from the timings. Exit code 2 with instructions when the model files are missing.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ml.embedder import Embedder
from app.ml.lock import DEFAULT_LOCK_PATH, ModelLoadError
from app.scripts.check_onnx_parity import DEFAULT_SENTENCES, missing_model_help, read_sentences

DEFAULT_BATCH_SIZES = (1, 8, 16, 32)


def peak_rss_bytes() -> int:
    """Peak resident memory of this process in bytes (Windows: peak working set; Linux and macOS: ru_maxrss)."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = Counters()
        counters.cb = ctypes.sizeof(Counters)
        kernel32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError("GetProcessMemoryInfo failed")
        return int(counters.PeakWorkingSetSize)
    import resource

    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return peak if sys.platform == "darwin" else peak * 1024  # macOS reports bytes, Linux kilobytes


@dataclass(frozen=True)
class BenchRow:
    batch_size: int
    texts: int
    tokens: int
    seconds: float
    peak_rss_bytes: int

    @property
    def texts_per_second(self) -> float:
        return self.texts / self.seconds

    @property
    def tokens_per_second(self) -> float:
        return self.tokens / self.seconds


def run_bench(embedder: Embedder, texts: list[str], batch_sizes: tuple[int, ...] = DEFAULT_BATCH_SIZES) -> list[BenchRow]:
    """Time `embed_passages` over `texts` for each batch size (the embedder's own batch size is restored)."""
    target: Any = embedder
    original = target.batch_size
    tokens = sum(embedder.count_tokens(t) for t in texts)
    rows: list[BenchRow] = []
    try:
        embedder.embed_passages(texts[:1])  # warm-up: first-run allocations and thread pools
        for size in batch_sizes:
            target.batch_size = size
            started = time.perf_counter()
            embedder.embed_passages(texts)
            seconds = max(time.perf_counter() - started, 1e-9)
            rows.append(BenchRow(size, len(texts), tokens, seconds, peak_rss_bytes()))
    finally:
        target.batch_size = original
    return rows


def format_table(rows: list[BenchRow]) -> str:
    header = f"{'batch':>6} {'texts':>6} {'seconds':>9} {'texts/s':>9} {'tokens/s':>10} {'peak RSS (MB)':>14}"
    lines = [header, "-" * len(header)]
    for r in rows:
        lines.append(
            f"{r.batch_size:>6} {r.texts:>6} {r.seconds:>9.2f} {r.texts_per_second:>9.1f} "
            f"{r.tokens_per_second:>10.0f} {r.peak_rss_bytes / 1024 / 1024:>14.0f}"
        )
    return "\n".join(lines)


def make_texts(embedder: Embedder, count: int, target_tokens: int, sentences: Path) -> list[str]:
    pool = [t for _, t in read_sentences(sentences)]
    texts: list[str] = []
    for i in range(count):
        parts: list[str] = []
        j = i
        while embedder.count_tokens(" ".join(parts)) < target_tokens:
            parts.append(pool[j % len(pool)])
            j += 1
        texts.append(" ".join(parts))
    return texts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.scripts.bench_embed", description=__doc__.split("\n\n")[0])
    p.add_argument("--model", default="e5-small")
    p.add_argument("--lock")
    p.add_argument("--model-dir", help="model root directory (default: EMBEDDING_MODEL_DIR)")
    p.add_argument("--texts", type=int, default=64)
    p.add_argument("--tokens", type=int, default=380, help="target tokens per text (chunks are 300-450)")
    p.add_argument("--batch-sizes", default=",".join(str(b) for b in DEFAULT_BATCH_SIZES))
    p.add_argument("--sentences", default=str(DEFAULT_SENTENCES))
    args = p.parse_args(argv)

    model_dir = args.model_dir
    if not model_dir:
        from app.core.config import settings

        model_dir = settings.EMBEDDING_MODEL_DIR
    if not model_dir:
        print(missing_model_help(args.model, "no model directory (--model-dir or EMBEDDING_MODEL_DIR)"), file=sys.stderr)
        return 2
    try:
        from app.ml.loader import load_embedder

        embedder = load_embedder(args.model, Path(model_dir), lock_path=Path(args.lock) if args.lock else DEFAULT_LOCK_PATH)
    except ModelLoadError as exc:
        print(missing_model_help(args.model, str(exc)), file=sys.stderr)
        return 2
    batch_sizes = tuple(int(x) for x in args.batch_sizes.split(",") if x.strip())
    texts = make_texts(embedder, args.texts, args.tokens, Path(args.sentences))
    print(f"model {embedder.model_id}, {len(texts)} texts of about {args.tokens} tokens")
    print(format_table(run_bench(embedder, texts, batch_sizes)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
