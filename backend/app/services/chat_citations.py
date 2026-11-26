"""
app/services/chat_citations.py
──────────────────────────────
Server-built citations (docs/phase6-plan.md 6.2, 6.4, 5.5; step 6a-10a). Pure: no database, no network.

Before the LLM call the prompt builder gives every source a label by position (S1..Sn, `PromptSource`); that tuple is
the turn's label table. The model's text only ever SELECTS a label (the stream filter checks it against the table);
title, location, heading, snippet and every id of a citation come from the server's hit, never from the model.

`build_snapshots` makes the `message_citations` rows of the cited labels (in the order of first citation);
`citation_items` makes the SSE `citations` event items, which hold the student fields only (8.2 CitationResponse): no
chunk, document or version id, no score, no flag. Every text field is untrusted plain text from an uploaded file.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

SNIPPET_CHARS: Final = 300  # plan 6.4 (and ck_message_citations_snippet)
TITLE_CHARS: Final = 200  # message_citations.document_title is VARCHAR(200)
FILE_KIND_CHARS: Final = 8  # message_citations.file_kind is VARCHAR(8)
UNKNOWN_FILE_KIND: Final = "unknown"

LocationKind = Literal["page", "slide", "section"]
# PPTX slides are stored as pages (F6); DOCX, TXT and MD have no real pages (their stored page is 1)
LOCATION_BY_FILE_KIND: Final[dict[str, LocationKind]] = {"pdf": "page", "pptx": "slide"}


class SourceHit(Protocol):
    """What a citation and the prompt read from a retrieval hit (app.services.retrieval.Hit)."""

    @property
    def chunk_id(self) -> uuid.UUID: ...
    @property
    def document_id(self) -> uuid.UUID: ...
    @property
    def version_id(self) -> uuid.UUID: ...
    @property
    def version_no(self) -> int: ...
    @property
    def document_title(self) -> str: ...
    @property
    def file_kind(self) -> str | None: ...
    @property
    def page_start(self) -> int | None: ...
    @property
    def page_end(self) -> int | None: ...
    @property
    def heading_path(self) -> str | None: ...
    @property
    def content(self) -> str: ...
    @property
    def vector_similarity(self) -> float | None: ...
    @property
    def injection_flags(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class PromptSource:
    label: str  # S1..Sn, the position in the prompt
    hit: SourceHit
    flagged: bool  # the chunk's ingest flags, or the title or heading scan at prompt-build time


@dataclass(frozen=True, slots=True)
class CitationSnapshot:
    """One message_citations row (the ids and tenant are added by the caller)."""

    label: str
    ordinal: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    document_title: str
    version_no: int
    file_kind: str
    location_kind: LocationKind
    page_start: int | None
    page_end: int | None
    heading_path: str | None
    snippet: str
    flagged: bool
    similarity: float | None


def location_kind(file_kind: str | None) -> LocationKind:
    return LOCATION_BY_FILE_KIND.get(file_kind or "", "section")


def location_text(file_kind: str | None, page_start: int | None, page_end: int | None) -> str:
    """The location as the prompt names it: "page 3", "pages 3-4", "slide 5", or "section"."""
    kind = location_kind(file_kind)
    if kind == "section":
        return "section"
    if page_start is None:
        return kind
    if page_end is None or page_end == page_start:
        return f"{kind} {page_start}"
    return f"{kind}s {page_start}-{page_end}"


def make_snippet(content: str) -> str:
    """The first SNIPPET_CHARS characters of the chunk, cut at a word boundary when there is one."""
    if len(content) <= SNIPPET_CHARS:
        return content
    if content[SNIPPET_CHARS].isspace():
        return content[:SNIPPET_CHARS].rstrip()
    head = content[:SNIPPET_CHARS]
    cut = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
    return head[:cut].rstrip() if cut > 0 else head


def build_snapshots(cited: Sequence[str], sources: Sequence[PromptSource]) -> tuple[CitationSnapshot, ...]:
    """One snapshot per cited label, in the order of first citation. A label outside the table is a bug upstream
    (the stream filter releases table labels only): refused loudly."""
    table = {s.label: s for s in sources}
    out: list[CitationSnapshot] = []
    seen: set[str] = set()
    for label in cited:
        if label in seen:
            continue
        source = table.get(label)
        if source is None:
            raise ValueError("a cited label is not in this turn's label table")
        seen.add(label)
        hit = source.hit
        kind = location_kind(hit.file_kind)
        file_kind = (hit.file_kind or UNKNOWN_FILE_KIND)[:FILE_KIND_CHARS]
        out.append(CitationSnapshot(
            label=label,
            ordinal=len(out) + 1,
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            version_id=hit.version_id,
            document_title=hit.document_title[:TITLE_CHARS],
            version_no=hit.version_no,
            file_kind=file_kind,
            location_kind=kind,
            page_start=None if kind == "section" else hit.page_start,
            page_end=None if kind == "section" else hit.page_end,
            heading_path=hit.heading_path,
            snippet=make_snippet(hit.content),
            flagged=source.flagged,
            similarity=hit.vector_similarity,
        ))
    return tuple(out)


def citation_items(snapshots: Sequence[CitationSnapshot]) -> list[dict[str, Any]]:
    """The SSE `citations` event items (plan 8.2 CitationResponse): student fields only."""
    return [
        {
            "label": s.label,
            "document_title": s.document_title,
            "location_kind": s.location_kind,
            "page_start": s.page_start,
            "page_end": s.page_end,
            "heading_path": s.heading_path,
            "snippet": s.snippet,
        }
        for s in snapshots
    ]


def needs_review(cited: Sequence[str], sources: Sequence[PromptSource]) -> bool:
    """Plan 5.5: an answer that cites ONLY flagged sources goes to the Phase 7 review queue."""
    flagged = {s.label: s.flagged for s in sources}
    return bool(cited) and all(flagged.get(label, False) for label in cited)
