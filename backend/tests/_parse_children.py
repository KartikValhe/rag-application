"""
Test-only child modules for the parse stage (step 5b-3): `python -P -m tests._parse_children <mode>`.

Started by the REAL sandbox through the production spawn path and the shared child bootstrap, so the
parent side (app.workers.stages.parse_in_sandbox) is exercised exactly as in production.

  corrupt-<variant>   a "compromised" child: answers success with a document that breaks the contract
  no_page_recheck     the real parser with the parser's page/slide re-check switched off
  no_hidden_skip      the real parser with hidden-run skipping switched off (hidden text extracted)
  real                the real parser
"""

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
}


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
    if mode == "no_page_recheck":
        document_extractors.check_page_count = lambda *_a, **_k: None  # type: ignore[assignment]
    elif mode == "no_hidden_skip":
        document_extractors.run_is_hidden = lambda *_a, **_k: False  # type: ignore[assignment]
    return document_parse_child.handle(request, data)


if __name__ == "__main__":
    run(_handle)
