"""
tests/security/document_assertions.py
─────────────────────────────────────
Shared security assertions for the document API (design C2 "prove-it-fails"). Every check is a
helper so tests/security/test_prove_it_fails_documents.py can switch a protection off and show that
the SAME helper then raises.
"""

import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from fastapi import HTTPException

GENERIC_404_BODY = {"detail": {"code": "not_found", "message": "Resource not found."}}


def _status_and_detail(result: Any) -> tuple[int, Any]:
    """Accept an HTTPException or an httpx Response."""
    if isinstance(result, HTTPException):
        return result.status_code, result.detail
    return int(result.status_code), result.json().get("detail")


def assert_generic_404(result: Any) -> None:
    """The one generic 404 used for both "not found" and "not yours" (no hint which one)."""
    status, detail = _status_and_detail(result)
    assert status == 404, f"expected the generic 404, got {status}: {detail}"
    assert detail == GENERIC_404_BODY["detail"], f"404 body must be the generic one, got {detail}"


def assert_manage_blocked(result: Any) -> None:
    """403 document_manage_requires_all_classes: visible, but not changeable by this caller."""
    status, detail = _status_and_detail(result)
    assert status == 403, f"expected 403, got {status}: {detail}"
    assert isinstance(detail, dict) and detail.get("code") == "document_manage_requires_all_classes", detail


def assert_error_code(response: Any, status: int, code: str) -> None:
    got_status, detail = _status_and_detail(response)
    assert got_status == status, f"expected {status} {code}, got {got_status}: {detail}"
    assert isinstance(detail, dict) and detail.get("code") == code, f"expected code {code}, got {detail}"


def assert_no_foreign_documents(results: Iterable[Any], allowed: Iterable[uuid.UUID | str]) -> None:
    """
    Every document id in `results` (ids, or dicts with an "id") must be in `allowed`.
    Used for list results and per-user visibility: a foreign id means a scope leak.
    """
    allowed_ids = {str(a) for a in allowed}
    seen = [str(r["id"]) if isinstance(r, dict) else str(r) for r in results]
    leaked = [s for s in seen if s not in allowed_ids]
    assert not leaked, f"documents outside the caller's scope were returned: {leaked}"


def stored_files(root: Path) -> list[Path]:
    """Every object file under the private store root (temp files included)."""
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def assert_no_stored_files(root: Path, tenant_id: uuid.UUID | None = None) -> None:
    """Nothing was left in the private store (optionally: under one tenant's prefix)."""
    base = root / "t" / str(tenant_id) if tenant_id is not None else root
    files = stored_files(base)
    assert not files, f"files were left in the document store: {[str(f.relative_to(root)) for f in files]}"


def assert_store_matches_rows(root: Path, storage_keys: Iterable[str]) -> None:
    """The store holds exactly the files the database knows about: no orphan, no missing file."""
    on_disk = {p.relative_to(root).as_posix() for p in stored_files(root)}
    in_db = set(storage_keys)
    assert on_disk == in_db, f"orphan files: {sorted(on_disk - in_db)}; missing files: {sorted(in_db - on_disk)}"


def assert_quota_refused(response: Any, code: str) -> None:
    """An upload over a quota is a 422 with the stable quota code."""
    assert_error_code(response, 422, code)


def assert_key_refused_for_other_tenant(exc: BaseException | None) -> None:
    """The private store must refuse a key that is not under the caller's tenant prefix."""
    from app.services.document_store import InvalidStorageKey

    assert isinstance(exc, InvalidStorageKey), f"a foreign tenant key was not refused (got {exc!r})"
