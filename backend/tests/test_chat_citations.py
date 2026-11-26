"""
Step 6a-10a: server-built citations (docs/phase6-plan.md 6.2, 6.4, 5.5).

The model's text only ever selects a label; every field of a citation comes from the server's hit. Pure module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.db.models.chat import CITATION_LOCATION_KINDS, CITATION_SNIPPET_MAX
from app.services import chat_citations as cc
from tests.fixtures.chat_hits import make_hit

MODULE = Path(cc.__file__)


def sources(*hits: object, flagged: tuple[bool, ...] | None = None) -> tuple[cc.PromptSource, ...]:
    marks = flagged or tuple(False for _ in hits)
    return tuple(cc.PromptSource(label=f"S{i}", hit=h, flagged=f) for i, (h, f) in enumerate(zip(hits, marks, strict=True), 1))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("file_kind", "kind"),
    [("pdf", "page"), ("pptx", "slide"), ("docx", "section"), ("txt", "section"), ("md", "section"), (None, "section"),
     ("xyz", "section")],
)
def test_location_kind_by_file_kind(file_kind: str | None, kind: str) -> None:
    assert cc.location_kind(file_kind) == kind
    assert kind in CITATION_LOCATION_KINDS


@pytest.mark.parametrize(
    ("file_kind", "start", "end", "text"),
    [
        ("pdf", 3, 3, "page 3"),
        ("pdf", 3, 4, "pages 3-4"),
        ("pptx", 5, 5, "slide 5"),
        ("pptx", 5, 7, "slides 5-7"),
        ("pdf", None, None, "page"),
        ("pdf", 4, None, "page 4"),
        ("docx", 1, 1, "section"),
        ("md", 1, 2, "section"),
        (None, 9, 9, "section"),
    ],
)
def test_location_text_for_the_prompt(file_kind: str | None, start: int | None, end: int | None, text: str) -> None:
    assert cc.location_text(file_kind, start, end) == text


def test_a_short_snippet_is_the_whole_chunk() -> None:
    assert cc.make_snippet("Props are read-only.") == "Props are read-only."
    assert cc.SNIPPET_CHARS == 300


def test_a_long_snippet_is_cut_at_a_word_boundary() -> None:
    words = " ".join(f"word{i:03d}" for i in range(100))  # 799 characters
    snip = cc.make_snippet(words)
    assert len(snip) <= 300
    assert words.startswith(snip)
    assert not snip.endswith(" ")
    assert words[len(snip)] == " "  # cut before a space: no half word
    assert len(snip) > 250


def test_a_snippet_with_no_space_is_cut_hard_at_300() -> None:
    assert cc.make_snippet("x" * 1000) == "x" * 300


def test_a_devanagari_snippet_is_cut_between_words() -> None:
    text = "प्रॉप्स पालक घटकाकडून बाल घटकाकडे माहिती पाठवतात " * 20
    snip = cc.make_snippet(text)
    assert len(snip) <= 300 and text.startswith(snip) and text[len(snip)] == " "


def test_snapshots_come_from_the_hit_only_in_order_of_first_citation() -> None:
    h1, h2, h3 = make_hit(1), make_hit(2, file_kind="pptx", page_start=5, page_end=6), make_hit(3, file_kind="docx")
    srcs = sources(h1, h2, h3, flagged=(False, True, False))
    snaps = cc.build_snapshots(("S2", "S3"), srcs)
    assert [s.label for s in snaps] == ["S2", "S3"]
    assert [s.ordinal for s in snaps] == [1, 2]
    s2, s3 = snaps
    assert (s2.chunk_id, s2.document_id, s2.version_id, s2.version_no) == (h2.chunk_id, h2.document_id, h2.version_id, 3)
    assert (s2.document_title, s2.heading_path, s2.file_kind) == (h2.document_title, h2.heading_path, "pptx")
    assert (s2.location_kind, s2.page_start, s2.page_end) == ("slide", 5, 6)
    assert s2.flagged is True and s3.flagged is False
    assert s2.similarity == h2.vector_similarity
    assert (s3.location_kind, s3.page_start, s3.page_end) == ("section", None, None)
    assert s2.snippet == cc.make_snippet(h2.content)


def test_an_uncited_source_gives_no_snapshot() -> None:
    assert cc.build_snapshots((), sources(make_hit(1))) == ()


def test_a_label_outside_the_table_is_refused_loudly() -> None:
    with pytest.raises(ValueError):
        cc.build_snapshots(("S2",), sources(make_hit(1)))


def test_a_repeated_label_gives_one_snapshot() -> None:
    assert len(cc.build_snapshots(("S1", "S1"), sources(make_hit(1)))) == 1


def test_snapshot_values_fit_the_columns() -> None:
    hit = make_hit(1, document_title="T" * 500, content="y" * 5000, file_kind=None)
    (snap,) = cc.build_snapshots(("S1",), sources(hit))
    assert len(snap.document_title) == cc.TITLE_CHARS == 200
    assert len(snap.snippet) <= CITATION_SNIPPET_MAX
    assert len(snap.file_kind) <= 8 and snap.file_kind == "unknown"
    assert len(snap.label) <= 8


def test_the_citation_event_items_hold_the_student_fields_only() -> None:
    h = make_hit(1, page_start=3, page_end=4)
    items = cc.citation_items(cc.build_snapshots(("S1",), sources(h, flagged=(True,))))
    assert items == [{
        "label": "S1", "document_title": h.document_title, "location_kind": "page", "page_start": 3, "page_end": 4,
        "heading_path": h.heading_path, "snippet": cc.make_snippet(h.content),
    }]
    for forbidden in (str(h.chunk_id), str(h.document_id), str(h.version_id), h.original_filename):
        assert forbidden not in repr(items)


@pytest.mark.parametrize(
    ("flags", "cited", "review"),
    [
        ((True, False), ("S1",), True),
        ((True, True), ("S1", "S2"), True),
        ((True, False), ("S1", "S2"), False),
        ((False, False), ("S1",), False),
        ((True, True), (), False),
    ],
)
def test_needs_review_only_when_every_cited_source_is_flagged(
    flags: tuple[bool, bool], cited: tuple[str, ...], review: bool
) -> None:
    assert cc.needs_review(cited, sources(make_hit(1), make_hit(2), flagged=flags)) is review


def test_the_module_is_pure() -> None:
    allowed = {"__future__", "uuid", "dataclasses", "typing", "collections.abc"}
    found: set[str] = set()
    for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.add(node.module or "")
    assert found <= allowed, sorted(found - allowed)
