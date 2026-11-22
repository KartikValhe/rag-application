"""
`updated_at` on DocumentVersionResponse (docs/backend-small-plan.md item 1).

The column app.document_versions.updated_at already exists (migration 0008) and every writer stamps it
explicitly; there is no trigger on purpose (the sweeper reads it as the age of a queued version). These
fast tests cover the response field and a source guard, so a future writer that forgets the stamp fails here.
The database tests are in test_document_version_updated_at_db.py.
"""

from __future__ import annotations

import ast
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.api.v1.documents import _version_response
from app.db.models.document import DocumentVersion
from app.schemas.document import DocumentVersionResponse

APP_DIR = Path(__file__).resolve().parents[1] / "app"
SQL_UPDATE = re.compile(r"\bUPDATE\s+(?:app\.)?document_versions\b", re.IGNORECASE)


def test_the_version_response_has_a_required_updated_at_datetime() -> None:
    field = DocumentVersionResponse.model_fields["updated_at"]
    assert field.annotation is datetime
    assert field.is_required()


def test_the_response_builder_copies_the_row_value() -> None:
    created = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    updated = datetime(2026, 11, 2, 10, 30, 5, 123456, tzinfo=UTC)
    row = DocumentVersion(
        id=uuid.uuid4(), tenant_id=uuid.uuid4(), document_id=uuid.uuid4(), version_no=1, status="queued",
        stage=None, error_code=None, file_kind="txt", original_filename="a.txt", size_bytes=1, page_count=None,
        chunk_count=None, flagged_chunk_count=None, created_at=created, updated_at=updated, published_at=None,
    )
    body = _version_response(row)
    assert body.updated_at == updated
    assert body.model_dump(mode="json")["updated_at"] == "2026-11-02T10:30:05.123456Z"


# ── Source guard: every writer of document_versions stamps updated_at ────────────────────────────────


def _is_update_call_on_version(node: ast.AST) -> bool:
    """`update(DocumentVersion)` or `sa.update(DocumentVersion)`."""
    if not (isinstance(node, ast.Call) and len(node.args) == 1):
        return False
    func = node.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    arg = node.args[0]
    return name == "update" and isinstance(arg, ast.Name) and arg.id == "DocumentVersion"


def find_unstamped_updates(source: str, filename: str = "<source>") -> tuple[list[str], int]:
    """
    (problems, writers_seen) for one source file. A writer is a string constant holding
    `UPDATE app.document_versions` (adjacent literals and f-strings are one constant to `ast`) or an
    `update(DocumentVersion)` statement. Each must mention `updated_at`.
    """
    tree = ast.parse(source, filename)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    problems: list[str] = []
    seen = 0
    for node in ast.walk(tree):
        text: str | None = None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if isinstance(parents.get(node), ast.JoinedStr):
                continue  # a piece of an f-string: the JoinedStr itself is checked as a whole
            text = node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(p.value for p in node.values if isinstance(p, ast.Constant) and isinstance(p.value, str))
        if text is not None and SQL_UPDATE.search(text):
            seen += 1
            if "updated_at" not in text:
                problems.append(f"{filename}:{node.lineno}: UPDATE app.document_versions without updated_at")
        if _is_update_call_on_version(node):
            seen += 1
            statement: ast.AST = node
            while statement in parents and not isinstance(statement, ast.stmt):
                statement = parents[statement]
            segment = ast.get_source_segment(source, statement) or ""
            if "updated_at" not in segment:
                problems.append(f"{filename}:{node.lineno}: update(DocumentVersion) without updated_at")
    return problems, seen


def test_every_update_of_document_versions_in_app_sets_updated_at() -> None:
    problems: list[str] = []
    writers = 0
    for path in sorted(APP_DIR.rglob("*.py")):
        found, seen = find_unstamped_updates(path.read_text(encoding="utf-8"), str(path.relative_to(APP_DIR)))
        problems += found
        writers += seen
    assert problems == []
    # Not vacuous: 8 raw UPDATE strings (lifecycle 2, ingest 3, pipeline, sweeper, rescan) and 2 ORM updates today
    assert writers >= 10, writers


def test_the_source_guard_notices_a_writer_without_updated_at() -> None:
    bad_sql = 'await db.execute(text("UPDATE app.document_versions SET status = \'x\' WHERE id = :v"))\n'
    bad_fstring = 'sql = f"UPDATE app.document_versions SET status = :s WHERE {cond}"\n'
    bad_orm = "await db.execute(\n    update(DocumentVersion)\n    .where(DocumentVersion.id == v)\n    .values(status='x')\n)\n"
    for source in (bad_sql, bad_fstring, bad_orm):
        problems, seen = find_unstamped_updates(source)
        assert seen == 1 and len(problems) == 1, (source, problems)

    good_adjacent = (
        'sql = ("UPDATE app.document_versions SET status = :s, "\n'
        '       "updated_at = now() WHERE id = :v")\n'
        'sql2 = f"UPDATE app.document_versions SET a = :a, updated_at = now() WHERE {cond}"\n'
        "stmt = update(DocumentVersion).where(x).values(status='x', updated_at=func.now())\n"
    )
    problems, seen = find_unstamped_updates(good_adjacent)
    assert problems == [] and seen == 3
