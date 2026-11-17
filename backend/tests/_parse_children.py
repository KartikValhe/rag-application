"""
Test-only child modules for the parse stage (step 5b-3): `python -P -m tests._parse_children <mode>`.

Started by the REAL sandbox through the production spawn path and the shared child bootstrap, so the
parent side (app.workers.stages.parse_in_sandbox) is exercised exactly as in production.

  corrupt-<variant>   a "compromised" child: answers success with a document that breaks the contract
  raw-<variant>       a "compromised" child that writes hostile RAW bytes (duplicate keys, NaN, deep
                      nesting ...) instead of the JSON the bootstrap would write
  die                 a child killed by a signal (no answer at all: a genuine crash)
  silent              a child that exits 0 without any answer (a genuine crash)
  raise_value_error   the real child whose extractor raises (the child must answer parse_failed)
  raise_no_guard      the same with the child's catch-all switched off (prove-it)
  no_page_recheck     the real parser with the parser's page/slide re-check switched off
  no_hidden_skip      the real parser with hidden-run skipping switched off (hidden text extracted)
  real                the real parser
"""

import json
import os
import signal
import sys
from typing import Any

from app.core.sandbox_child import run

GOOD_BLOCK = {"t": "para", "level": None, "text": "Unit 1 Algebra"}


def _doc(**overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "file_kind": "txt",
        "page_count": 1,
        "pages": [{"n": 1, "blocks": [dict(GOOD_BLOCK)]}],
        "stats": {"chars": 14, "blocks": 1, "hidden_text_chars": 0, "script_guess": "latn"},
    }
    doc.update(overrides)
    return doc


CORRUPT: dict[str, Any] = {
    "control": _doc(pages=[{"n": 1, "blocks": [{"t": "para", "level": None, "text": "Unit\x001 Algebra"}]}]),
    # An invisible format character: valid JSON, so only the parent's clean-text rule catches it
    "zero_width": _doc(pages=[{"n": 1, "blocks": [{"t": "para", "level": None, "text": "Unit​1 Algebra"}]}]),
    "extra_key": {**_doc(), "debug": "x"},
    "wrong_type": _doc(page_count="1"),
    "negative_page": _doc(pages=[{"n": -1, "blocks": [dict(GOOD_BLOCK)]}]),
    "no_document": None,
    "not_a_dict": ["pages"],
    "stats_lie": _doc(stats={"chars": 1, "blocks": 1, "hidden_text_chars": 0, "script_guess": "latn"}),
    "too_many_blocks": _doc(
        pages=[{"n": 1, "blocks": [dict(GOOD_BLOCK)] * 50_001}],
        stats={"chars": 14 * 50_001, "blocks": 50_001, "hidden_text_chars": 0, "script_guess": "latn"},
    ),
    "page_count_lie": _doc(file_kind="pdf", page_count=500),
    "kind_lie": _doc(file_kind="md"),
    "unknown_code": "fail",
    # A compromised child that claims a valid-looking document with more pages than the tenant's cap.
    # The text of the one page it sends is short, so only the page_count says anything (review 5b-3).
    "pdf_page_count_500": _doc(file_kind="pdf", page_count=500),
    "pdf_page_count_200": _doc(file_kind="pdf", page_count=200),
    "pptx_page_count_101": _doc(file_kind="pptx", page_count=101),
    "pptx_page_count_100": _doc(file_kind="pptx", page_count=100),
}


def _good_json(**extra: Any) -> bytes:
    answer = {"success": True, "document": _doc(), "peak_memory_kb": 1000, **extra}
    return json.dumps(answer).encode()


def _with_junk(junk: bytes) -> bytes:
    """A good answer plus one ignored top-level key whose RAW value is `junk`."""
    good = _good_json()
    assert good.endswith(b"}")
    return good[:-1] + b',"junk":' + junk + b"}"


def raw_answer(variant: str) -> bytes:
    """The raw bytes a hostile child writes; each variant breaks exactly one rule of the parent's parser."""
    good = _good_json()
    doc = json.dumps(_doc()).encode()
    table: dict[str, bytes] = {
        "duplicate_key": b'{"success":false,"success":true,"document":' + doc + b"}",
        "nan": good[:-1] + b',"peak_memory_kb":NaN}',
        "infinity": _with_junk(b"-Infinity"),
        "float": _with_junk(b"1.5"),
        "deep_100": _with_junk(b"[" * 100 + b"]" * 100),
        "deep_10000": _with_junk(b"[" * 10_000 + b"]" * 10_000),
        "many_objects": _with_junk(b"[" + b"{}," * 4_000_000 + b"{}]"),  # 12 MB, 4 million objects
        "many_numbers": _with_junk(b"[" + b"1," * 6_000_000 + b"1]"),  # 12 MB, 6 million numbers
        "huge_int_5000": _with_junk(b"9" * 5000),
        "big_int_100": _with_junk(b"9" * 100),
        "lone_surrogate": _with_junk(b'"\\ud800"'),
        "nul": _with_junk(b'"a\\u0000b"'),
        "surrogate_key": good[:-1] + b',"\\udc00":1}',
        "bom": b"\xef\xbb\xbf" + good,
        "invalid_utf8": _with_junk(b'"\xff\xfe"'),
        "utf16": good.decode().encode("utf-16"),
        "trailing_garbage": good + b" x",
        "not_an_object": b"[1,2]",
    }
    return table[variant]


def _handle(request: dict[str, Any], data: bytes) -> dict[str, Any]:
    from app.services import document_extractors, document_parse_child

    mode = sys.argv[1] if len(sys.argv) > 1 else "real"
    if mode.startswith("corrupt-"):
        variant = mode.removeprefix("corrupt-")
        if variant == "unknown_code":
            return {"success": False, "error_code": "C:/tenant/secret/path.pdf"}
        result: dict[str, Any] = {"success": True}
        if variant != "no_document":
            result["document"] = CORRUPT[variant]
        return result
    if mode.startswith("raw-"):
        out = sys.__stdout__.buffer  # type: ignore[union-attr]  # the bootstrap redirected sys.stdout only
        out.write(raw_answer(mode.removeprefix("raw-")))
        out.flush()
        os._exit(0)
    if mode == "die":
        os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))
        os._exit(137)  # not reached on POSIX
    if mode == "silent":
        os._exit(0)
    if mode in ("raise_value_error", "raise_no_guard"):

        def boom(*_a: Any, **_k: Any) -> Any:
            raise ValueError("CANARY-EXCEPTION-TEXT with /secret/path.docx")

        document_extractors.extract_document = boom  # type: ignore[assignment]
        if mode == "raise_no_guard":

            def reraise(exc: BaseException) -> Any:
                raise exc

            document_parse_child._unexpected_failure = reraise  # type: ignore[attr-defined]
    elif mode == "no_page_recheck":
        document_extractors.check_page_count = lambda *_a, **_k: None  # type: ignore[assignment]
    elif mode == "no_hidden_skip":
        document_extractors.run_is_hidden = lambda *_a, **_k: False  # type: ignore[assignment]
    return document_parse_child.handle(request, data)


if __name__ == "__main__":
    run(_handle)
