"""
app/scripts/bench_parse.py
──────────────────────────
Manual benchmark of the sandboxed document parser (step 5b-3). NOT a test and not run in CI.

    python -m app.scripts.bench_parse path/to/folder [--max-pdf-pages 200] [--max-slides 100]
                                                     [--timeout 120] [--memory-mb 1024]

Parses every .pdf, .docx, .pptx, .txt and .md file in the folder (and its subfolders) exactly as the
worker does: the same child module in the same sandbox, the same parameters, and the same parent-side
validation of the answer. Prints one line per file:

    file, format, pages, chars, script_guess, gate result (ok or the error code), seconds, peak memory

Put real syllabus files in backend/tests/fixtures/real_corpus/ (ignored by git: real files are never
committed). The script needs no database, no Redis and no settings: it does not import app.core.config.
Peak memory is the child's own peak resident memory (Linux VmHWM, Windows peak working set). The
memory limit applies on Linux only.
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

from app.core import sandbox
from app.core.upload_security import (
    DOC_MAX_PDF_PAGES,
    DOC_MAX_PPTX_SLIDES,
    DOC_UPLOAD_CEILING_BYTES,
)
from app.services.parsed_document import (
    DEFAULT_MAX_TEXT_CHARS,
    ParsedDocumentError,
    validate_parsed_document,
)

CHILD = "app.services.document_parse_child"
KINDS = {".pdf": "pdf", ".docx": "docx", ".pptx": "pptx", ".txt": "txt", ".md": "md"}


async def bench_file(path: Path, args: argparse.Namespace) -> tuple[str, ...]:
    kind = KINDS[path.suffix.lower()]
    data = path.read_bytes()
    limits = sandbox.SandboxLimits(
        timeout_seconds=float(args.timeout),
        max_output_bytes=20 * 1024 * 1024,
        memory_limit_bytes=args.memory_mb * 1024 * 1024,
        cpu_seconds=int(args.timeout),
    )
    params = {
        "mode": "parse",
        "file_kind": kind,
        "max_file_bytes": DOC_UPLOAD_CEILING_BYTES,
        "max_pdf_pages": args.max_pdf_pages,
        "max_pptx_slides": args.max_slides,
        "max_text_chars": DEFAULT_MAX_TEXT_CHARS,
        "time_budget_seconds": args.timeout * 0.8,
    }
    started = time.perf_counter()
    pages = chars = "-"
    script = "-"
    peak = "-"
    try:
        raw = await sandbox.run_child(CHILD, data, params=params, limits=limits, log_event="bench_child_abnormal_exit")
        result = sandbox.parse_child_json(raw)
        if result.get("success") is True:
            cap = args.max_pdf_pages if kind == "pdf" else args.max_slides if kind == "pptx" else 1
            doc = validate_parsed_document(result.get("document"), max_pages=cap, expected_kind=kind)
            pages, chars, script, gate = str(doc.page_count), str(doc.stats.chars), doc.stats.script_guess, "ok"
            if doc.stats.hidden_text_chars:
                gate = f"ok (hidden {doc.stats.hidden_text_chars})"
        else:
            gate = str(result.get("error_code", "parse_failed"))
        if isinstance(result.get("peak_memory_kb"), int):
            peak = f"{result['peak_memory_kb'] // 1024} MiB"
    except sandbox.SandboxError as exc:
        gate = exc.code
    except ParsedDocumentError as exc:
        gate = exc.code
    seconds = f"{time.perf_counter() - started:.2f}"
    return (path.name, kind, pages, chars, script, gate, seconds, peak)


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark the sandboxed document parser on a folder.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--max-pdf-pages", type=int, default=DOC_MAX_PDF_PAGES)
    parser.add_argument("--max-slides", type=int, default=DOC_MAX_PPTX_SLIDES)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--memory-mb", type=int, default=1024)
    args = parser.parse_args(argv)
    if not args.folder.is_dir():
        print("Not a folder.", file=sys.stderr)
        return 2
    files = sorted(p for p in args.folder.rglob("*") if p.is_file() and p.suffix.lower() in KINDS)
    if not files:
        print("No .pdf, .docx, .pptx, .txt or .md files found.", file=sys.stderr)
        return 1
    header = ("file", "format", "pages", "chars", "script", "result", "seconds", "peak")
    rows = [header] + [await bench_file(path, args) for path in files]
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths, strict=True)))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
