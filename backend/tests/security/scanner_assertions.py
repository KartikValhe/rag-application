"""
tests/security/scanner_assertions.py
────────────────────────────────────
Shared assertions for step 5d-2 (the injection scanner, the index-stage flag write, test search and the flags
route). The normal tests use them with every protection ON; tests/security/test_prove_it_fails_scanner.py
switches one protection off at a time and shows that the SAME helper then raises.
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.chunker import TextChunk
from tests.security.document_assertions import assert_generic_404


async def chunk_rows(db: AsyncSession, version_id: uuid.UUID) -> list[tuple[int, str | None, str, list[str]]]:
    """(ordinal, heading_path, content, injection_flags) of a version's chunks, in ordinal order."""
    rows = (
        await db.execute(
            text(
                "SELECT ordinal, heading_path, content, injection_flags FROM app.chunks "
                "WHERE document_version_id = :v ORDER BY ordinal"
            ),
            {"v": version_id},
        )
    ).all()
    return [(r[0], r[1], r[2], list(r[3])) for r in rows]


def snapshot_chunks(chunks: Sequence[TextChunk]) -> list[tuple[int, bytes, bytes]]:
    """What the chunker produced, as bytes, taken BEFORE the index stage runs."""
    return [(c.ordinal, c.heading_path.encode("utf-8"), c.content.encode("utf-8")) for c in chunks]


def assert_chunk_text_unchanged(
    rows: Sequence[tuple[int, str | None, str, list[str]]], chunker_output: Sequence[tuple[int, bytes, bytes]]
) -> None:
    """The stored chunks are byte for byte the chunker's output (the scanner flags, it never edits)."""
    assert len(rows) == len(chunker_output), f"chunk text changed: {len(rows)} rows for {len(chunker_output)} chunks"
    for (ordinal, heading, content, _flags), (want_ordinal, want_heading, want_content) in zip(
        rows, chunker_output, strict=True
    ):
        assert ordinal == want_ordinal, "chunk text changed: the ordinals moved"
        assert (heading or "").encode("utf-8") == want_heading, f"chunk text changed: heading of chunk {ordinal}"
        assert content.encode("utf-8") == want_content, f"chunk text changed: content of chunk {ordinal}"


def assert_flags_written(
    rows: Sequence[tuple[int, str | None, str, list[str]]],
    flagged_chunk_count: int | None,
    expected: Mapping[int, Iterable[str]],
) -> None:
    """Every chunk in `expected` (ordinal -> codes it must carry) has those codes, every other chunk has none,
    and the version's flagged_chunk_count is the number of chunks with at least one code."""
    by_ordinal = {r[0]: r[3] for r in rows}
    for ordinal, codes in expected.items():
        missing = set(codes) - set(by_ordinal.get(ordinal, []))
        assert not missing, f"flags missing on chunk {ordinal}: {sorted(missing)} (stored {by_ordinal.get(ordinal)})"
    unexpected = {o: f for o, f in by_ordinal.items() if f and o not in expected}
    assert not unexpected, f"flags on clean chunks: {unexpected}"
    flagged = sum(1 for f in by_ordinal.values() if f)
    assert flagged_chunk_count == flagged, f"flagged_chunk_count is {flagged_chunk_count}, {flagged} chunks are flagged"
    assert flagged >= len(expected), "flags missing: fewer flagged chunks than expected"


# ── Routes ───────────────────────────────────────────────────────────────────


def assert_no_foreign_canary(response: Any, canary: str) -> None:
    """A test-search answer for a pair the caller does not own: the generic 404, and no hit with the canary."""
    body = response.json()
    hits = body.get("hits", []) if isinstance(body, dict) else []
    leaked = [h for h in hits if canary in str(h.get("content", ""))]
    assert not leaked, f"test search leaked {len(leaked)} foreign chunk(s) with {canary!r}"
    assert_generic_404(response)


def assert_rate_limited(statuses: Sequence[int], limit: int) -> None:
    """With a limit of `limit` searches per window, the first `limit` pass and the next one is a 429."""
    assert len(statuses) == limit + 1
    assert all(s == 200 for s in statuses[:limit]), f"searches within the limit failed: {statuses}"
    assert statuses[limit] == 429, f"search {limit + 1} was not rate limited: {statuses}"


def assert_foreign_version_hidden(response: Any) -> None:
    """The flags of a version the caller may not see: the generic 404, no flagged excerpt."""
    assert "items" not in (response.json() or {}), "the flags route returned a foreign version's flagged chunks"
    assert_generic_404(response)


def assert_no_material(response: Any, canary: str) -> None:
    """A test search by someone who manages no pair (a student, even with document:view): either the generic
    404 or a 200 with an empty scope, and never a hit with the canary."""
    body = response.json()
    hits = body.get("hits", []) if isinstance(body, dict) else []
    leaked = [h for h in hits if canary in str(h.get("content", ""))]
    assert not leaked, f"test search leaked {len(leaked)} chunk(s) with {canary!r} to a caller without a pair"
    if response.status_code == 404:
        assert_generic_404(response)
        return
    assert response.status_code == 200, f"expected 200 (empty scope) or the generic 404, got {response.status_code}"
    assert body["reason"] == "empty_scope", f"material reached a caller without a pair: reason {body['reason']}"


def assert_embed_cap_respected(max_in_flight: int, cap: int) -> None:
    """At no moment were more than `cap` query embeddings running."""
    assert max_in_flight <= cap, f"embedding cap exceeded: {max_in_flight} embeddings at once, the cap is {cap}"


def assert_publish_needs_acknowledgement(response: Any) -> None:
    """Publishing a version that needs a teacher's acknowledgement, without it: 409 flags_acknowledgement_required."""
    detail = response.json().get("detail")
    code = detail.get("code") if isinstance(detail, dict) else None
    assert (response.status_code, code) == (409, "flags_acknowledgement_required"), (
        f"published without acknowledgement: {response.status_code} {code}"
    )


def assert_not_echoed(response: Any, secret: str) -> None:
    """A 422 that does not repeat what the client sent (a password, a token, a query)."""
    assert response.status_code == 422, f"expected 422, got {response.status_code}"
    assert secret not in response.text, "the 422 body echoed the request input"
    for error in response.json().get("detail", []):
        assert "input" not in error, "the 422 body echoed the request input"
