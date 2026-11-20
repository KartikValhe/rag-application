"""
app/services/retrieval_eval.py
──────────────────────────────
The PURE part of the retrieval evaluation harness (step 5d-3, docs/phase5d-plan.md section 10): no database,
no model, no file access except load_golden. The command that runs it is app/scripts/eval_retrieval.py.

What it does
    * reads the golden set (a CSV with the columns #, Language, Question, Document, Expected location,
      Should answer) and says which row is wrong when one is;
    * decides whether a retrieved chunk is the expected one (the document by title or file name, the place by
      page / slide range overlap or by a heading text);
    * computes hit@k, MRR, answer rate and refusal accuracy, overall and per language, plus the same hit@k for
      the keyword side alone and the vector side alone (the input for the trigram decision, backlog B92);
    * sweeps the sufficiency threshold from the STORED best similarity (no new search per threshold) and marks
      the best balanced threshold and the highest one that keeps 90% of the should-answer questions answered;
    * builds a JSON-able report and a plain-English console summary;
    * (step 5d-4, `--compare`) builds the grid of retrieval configurations to try, counts for each one how many
      questions changed their hit@1 against the current settings, warns when the golden set is too small to
      trust small differences, and prints all of it as ONE table.

It never sees chunk text: ChunkRef has no content field, so a report cannot leak any. Titles, file names and
headings are untrusted text from uploaded files; the console summary cleans them with safe_text.

It decides nothing: no threshold is hard-coded here (the sweep range 0.70 to 0.95 is only the range of the
table). The owner reads the table and sets RETRIEVAL_MIN_SIMILARITY.
"""

from __future__ import annotations

import csv
import io
import itertools
import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

NONE_DOCUMENT = "(none)"
COLUMNS = ("#", "Language", "Question", "Document", "Expected location", "Should answer")
HIT_KS = (1, 3, 5)  # the hit@k columns; the cutoff k of the run is added when it is another number
TOP_ROWS = 5  # rows of the per-question table
MIN_ANSWER_RATE = 0.90  # for the "highest threshold that keeps the answer rate" line of the sweep

# The sweep: 0.70, 0.71 ... 0.95. Built from whole hundredths so every value equals its decimal literal.
THRESHOLDS: tuple[float, ...] = tuple((70 + i) / 100 for i in range(26))

# What happened to one question
OK = "ok"
NO_CANDIDATE = "no_candidate"
WRONG_CHUNK = "wrong_chunk"
BELOW_THRESHOLD = "below_threshold"
DOCUMENT_NOT_IN_SCOPE = "document_not_in_scope"
REFUSED_CORRECTLY = "refused_correctly"
ANSWERED_ANYWAY = "answered_anyway"
OUTCOMES = (OK, BELOW_THRESHOLD, WRONG_CHUNK, NO_CANDIDATE, DOCUMENT_NOT_IN_SCOPE, REFUSED_CORRECTLY, ANSWERED_ANYWAY)

_DASHES = "-" + chr(0x2013) + chr(0x2014)  # hyphen, en dash, em dash
_PAGES_RE = re.compile(r"^(?:slides?|pages?)\s+(\d{1,6})(?:\s*[" + _DASHES + r"]\s*(\d{1,6}))?$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^heading\s*:\s*(\S.*)$", re.IGNORECASE)
_YES = frozenset({"yes", "y", "true", "1"})
_NO = frozenset({"no", "n", "false", "0"})

LOCATION_FORMATS = "'slide N', 'slide N-M', 'page N', 'pages N-M' or 'heading: <text>'"


class GoldenSetError(ValueError):
    """The golden set cannot be used. `row` is the spreadsheet row of the bad line (the header is row 1)."""

    def __init__(self, message: str, row: int | None = None) -> None:
        super().__init__(f"golden set row {row}: {message}" if row else f"golden set: {message}")
        self.message = message
        self.row = row


# ── text helpers ─────────────────────────────────────────────────────────────


def fold(text: str) -> str:
    """The comparison form: NFC, casefolded, NFC again (casefold can leave NFC), whitespace collapsed."""
    folded = unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())
    return " ".join(folded.split())


def safe_text(text: str | None, limit: int = 80) -> str:
    """Text from an uploaded file or a golden file, made safe for one console line: control and format
    characters (escape sequences, bidi marks, newlines) become spaces, runs of spaces collapse, and a long
    text is cut with '...'."""
    cleaned = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in (text or ""))
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: max(limit - 3, 0)] + "..."


# ── golden set ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Location:
    """Where the answer is. kind "pages": a page or slide range (start..end, inclusive). kind "heading": a
    text that must appear in the chunk's heading path."""

    kind: Literal["pages", "heading"]
    start: int | None
    end: int | None
    heading: str | None


@dataclass(frozen=True)
class GoldenRow:
    number: str  # the "#" cell
    row: int  # spreadsheet row of the line (the header is row 1)
    language: str
    question: str
    document: str | None  # None for a should-not-answer row written as (none)
    location_text: str
    location: Location | None  # None when the row should not be answered
    should_answer: bool


def parse_location(text: str) -> Location:
    """'slide 5', 'slide 3-4', 'page 2', 'pages 2-4' or 'heading: <text>' (any case; hyphen, en dash or em dash)."""
    cleaned = text.strip()
    if not cleaned:
        raise GoldenSetError(f"Expected location is empty (write {LOCATION_FORMATS})")
    heading = _HEADING_RE.match(cleaned)
    if heading:
        return Location("heading", None, None, heading.group(1).strip())
    pages = _PAGES_RE.match(cleaned)
    if pages:
        start = int(pages.group(1))
        end = int(pages.group(2)) if pages.group(2) else start
        if start < 1:
            raise GoldenSetError(f"Expected location {safe_text(cleaned, 40)!r}: numbers start at 1")
        if end < start:
            raise GoldenSetError(f"Expected location {safe_text(cleaned, 40)!r}: the range runs backwards")
        return Location("pages", start, end, None)
    raise GoldenSetError(f"Expected location {safe_text(cleaned, 40)!r} is not {LOCATION_FORMATS}")


def _cell_row(cells: list[str], index: dict[str, int], row: int) -> GoldenRow:
    def cell(name: str) -> str:
        return cells[index[name.casefold()]].strip()

    try:
        language = unicodedata.normalize("NFC", cell("Language"))
        question = unicodedata.normalize("NFC", cell("Question"))
        if not question:
            raise GoldenSetError("Question is empty")
        if not language:
            raise GoldenSetError("Language is empty")
        document_cell = cell("Document")
        none_document = fold(document_cell) == fold(NONE_DOCUMENT)
        answer_cell = fold(cell("Should answer"))
        if answer_cell in _YES:
            should_answer = True
        elif answer_cell in _NO:
            should_answer = False
        elif answer_cell == "":
            should_answer = not none_document and bool(document_cell)
        else:
            raise GoldenSetError(f"Should answer must be Yes or No, not {safe_text(cell('Should answer'), 20)!r}")
        location_text = cell("Expected location")
        if should_answer and none_document:
            raise GoldenSetError("Document is (none) but Should answer is Yes")
        number = cell("#") or str(row - 1)
        if not should_answer:
            document = None if none_document or not document_cell else document_cell
            return GoldenRow(number, row, language, question, document, location_text, None, False)
        if not document_cell:
            raise GoldenSetError("Document is empty (write the document title or file name)")
        return GoldenRow(
            number, row, language, question, document_cell, location_text, parse_location(location_text), True
        )
    except GoldenSetError as exc:
        raise GoldenSetError(exc.message, row) from None


def parse_golden(text: str) -> list[GoldenRow]:
    """The rows of a golden-set CSV (UTF-8 text; a leading BOM is ignored). Columns are found by their header
    name; other columns are ignored; blank lines are skipped. Any bad row raises GoldenSetError with its row
    number (the header is row 1, so the numbers match a spreadsheet)."""
    reader = csv.reader(io.StringIO(text.lstrip(chr(0xFEFF)), newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise GoldenSetError("the file has no header row") from None
    except csv.Error as exc:
        raise GoldenSetError(f"the file is not readable as CSV: {exc}", 1) from None
    index = {name.strip().casefold(): i for i, name in enumerate(header)}
    missing = [c for c in COLUMNS if c.casefold() not in index]
    if missing:
        raise GoldenSetError(f"missing column(s): {', '.join(missing)} (found: {', '.join(h.strip() for h in header)})", 1)
    rows: list[GoldenRow] = []
    row_number = 1
    while True:
        row_number += 1
        try:
            cells = next(reader)
        except StopIteration:
            break
        except csv.Error as exc:
            raise GoldenSetError(f"the line is not readable as CSV: {exc}", row_number) from None
        if all(not c.strip() for c in cells):
            continue
        if len(cells) != len(header):
            raise GoldenSetError(
                f"has {len(cells)} columns but the header has {len(header)} (a question with a comma needs quotes)",
                row_number,
            )
        rows.append(_cell_row(cells, index, row_number))
    if not rows:
        raise GoldenSetError("the file has no questions")
    return rows


def load_golden(path: Path) -> list[GoldenRow]:
    """parse_golden for a file; the file must be UTF-8 (with or without a BOM)."""
    try:
        text = Path(path).read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        raise GoldenSetError("the file is not UTF-8 (save the CSV as 'CSV UTF-8')") from None
    return parse_golden(text)


# ── matching ─────────────────────────────────────────────────────────────────


def location_matches(location: Location, *, page_start: int | None, page_end: int | None, heading_path: str | None) -> bool:
    """A page or slide location is hit when the chunk's page range overlaps it; a heading location is hit when
    the casefolded NFC heading text is a substring of the chunk's heading path."""
    if location.kind == "heading":
        return bool(location.heading) and heading_path is not None and fold(location.heading or "") in fold(heading_path)
    low = page_start if page_start is not None else page_end
    high = page_end if page_end is not None else page_start
    if low is None or high is None or location.start is None or location.end is None:
        return False
    return low <= location.end and high >= location.start


def document_matches(expected: str, *, title: str, original_filename: str) -> bool:
    """The Document cell names a document by its title, its file name, or its file name without the extension
    (case, spaces and Unicode form do not matter). No partial names: 'Science' would match every science file."""
    wanted = fold(expected)
    stem = original_filename.rsplit(".", 1)[0] if "." in original_filename else original_filename
    return bool(wanted) and wanted in {fold(title), fold(original_filename), fold(stem)}


@dataclass(frozen=True)
class DocRef:
    """A published document of the evaluated class and subject (for 'the document is not in scope')."""

    title: str
    original_filename: str


@dataclass(frozen=True)
class ChunkRef:
    """One chunk as the evaluation sees it. No content on purpose (so no report can print it)."""

    chunk_id: str
    document_title: str
    original_filename: str
    page_start: int | None
    page_end: int | None
    heading_path: str | None
    vector_similarity: float | None = None
    keyword_matched_terms: int | None = None
    fused_score: float | None = None


@dataclass(frozen=True)
class Retrieved:
    """What one search gave back for one question."""

    hits: tuple[ChunkRef, ...]  # fused order, at most k
    vector_ranked: tuple[ChunkRef, ...]  # the vector side alone, best first
    keyword_ranked: tuple[ChunkRef, ...]  # the keyword side alone, best first
    sufficient: bool  # at the configured threshold
    reason: str
    max_similarity: float | None
    keyword_coverage: float | None
    candidate_count: int


@dataclass(frozen=True)
class QuestionResult:
    number: str
    row: int
    language: str
    question: str
    document: str | None
    expected_location: str
    should_answer: bool
    sufficient: bool
    reason: str
    max_similarity: float | None
    keyword_coverage: float | None
    candidate_count: int
    hit_position: int | None  # 1-based place of the expected chunk among the hits
    vector_hit_position: int | None  # ... in the vector side alone
    keyword_hit_position: int | None  # ... in the keyword side alone
    outcome: str
    top: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "number": self.number, "row": self.row, "language": self.language, "question": self.question,
            "document": self.document if self.document is not None else NONE_DOCUMENT,
            "expected_location": self.expected_location, "should_answer": self.should_answer,
            "sufficient": self.sufficient, "reason": self.reason, "max_similarity": self.max_similarity,
            "keyword_coverage": self.keyword_coverage, "candidate_count": self.candidate_count,
            "hit_position": self.hit_position, "vector_hit_position": self.vector_hit_position,
            "keyword_hit_position": self.keyword_hit_position, "outcome": self.outcome, "top": list(self.top),
        }


def _is_expected(row: GoldenRow, chunk: ChunkRef) -> bool:
    return (
        row.should_answer and row.document is not None and row.location is not None
        and document_matches(row.document, title=chunk.document_title, original_filename=chunk.original_filename)
        and location_matches(
            row.location, page_start=chunk.page_start, page_end=chunk.page_end, heading_path=chunk.heading_path
        )
    )


def _first_position(row: GoldenRow, chunks: Sequence[ChunkRef]) -> int | None:
    for position, chunk in enumerate(chunks, start=1):
        if _is_expected(row, chunk):
            return position
    return None


def evaluate_question(row: GoldenRow, retrieved: Retrieved, documents: Sequence[DocRef] | None = None) -> QuestionResult:
    """Score one question. `documents` (the published documents of the evaluated scope) lets a Document cell
    that matches nothing be reported as such, instead of as a retrieval failure."""
    hit = _first_position(row, retrieved.hits)
    if not row.should_answer:
        outcome = ANSWERED_ANYWAY if retrieved.sufficient else REFUSED_CORRECTLY
    elif documents is not None and row.document is not None and not any(
        document_matches(row.document, title=d.title, original_filename=d.original_filename) for d in documents
    ):
        outcome = DOCUMENT_NOT_IN_SCOPE
    elif not retrieved.hits:
        outcome = NO_CANDIDATE
    elif hit is None:
        outcome = WRONG_CHUNK
    elif not retrieved.sufficient:
        outcome = BELOW_THRESHOLD
    else:
        outcome = OK
    top = tuple(
        {
            "rank": rank, "chunk_id": c.chunk_id, "document_title": c.document_title, "page_start": c.page_start,
            "page_end": c.page_end, "heading_path": c.heading_path, "vector_similarity": c.vector_similarity,
            "keyword_matched_terms": c.keyword_matched_terms, "fused_score": c.fused_score,
            "is_match": _is_expected(row, c),
        }
        for rank, c in enumerate(retrieved.hits[:TOP_ROWS], start=1)
    )
    return QuestionResult(
        number=row.number, row=row.row, language=row.language, question=row.question, document=row.document,
        expected_location=row.location_text, should_answer=row.should_answer, sufficient=retrieved.sufficient,
        reason=retrieved.reason, max_similarity=retrieved.max_similarity, keyword_coverage=retrieved.keyword_coverage,
        candidate_count=retrieved.candidate_count, hit_position=hit,
        vector_hit_position=_first_position(row, retrieved.vector_ranked),
        keyword_hit_position=_first_position(row, retrieved.keyword_ranked), outcome=outcome, top=top,
    )


# ── metrics ──────────────────────────────────────────────────────────────────


def _hit_rates(positions: Sequence[int | None], ks: Sequence[int]) -> dict[str, float] | None:
    if not positions:
        return None
    return {str(k): sum(1 for p in positions if p is not None and p <= k) / len(positions) for k in ks}


def aggregate(results: Sequence[QuestionResult], cutoff: int) -> dict[str, Any]:
    """The numbers of one group of questions. Rates are fractions from 0 to 1, or None when the group has no
    row of the kind the rate is about (so 'no refusal rows' is never shown as 0%). `cutoff` is the k of the
    run: MRR counts a hit only inside it, and it adds a hit@k column when it is not 1, 3 or 5."""
    ks = sorted({*HIT_KS, cutoff})
    answers = [r for r in results if r.should_answer]
    refusals = [r for r in results if not r.should_answer]
    mrr = (
        sum(1 / r.hit_position for r in answers if r.hit_position is not None and r.hit_position <= cutoff) / len(answers)
        if answers else None
    )
    return {
        "questions": len(results),
        "should_answer": len(answers),
        "should_not_answer": len(refusals),
        "hit_at": _hit_rates([r.hit_position for r in answers], ks),
        "mrr": mrr,
        "mrr_cutoff": cutoff,
        "answer_rate": (sum(1 for r in answers if r.sufficient) / len(answers)) if answers else None,
        "refusal_accuracy": (sum(1 for r in refusals if not r.sufficient) / len(refusals)) if refusals else None,
        "keyword_only_hit_at": _hit_rates([r.keyword_hit_position for r in answers], ks),
        "vector_only_hit_at": _hit_rates([r.vector_hit_position for r in answers], ks),
        "outcomes": {o: sum(1 for r in results if r.outcome == o) for o in OUTCOMES},
    }


def by_language(results: Sequence[QuestionResult], cutoff: int) -> dict[str, dict[str, Any]]:
    """aggregate() for each Language value, in alphabetical order."""
    languages = sorted({r.language for r in results})
    return {lang: aggregate([r for r in results if r.language == lang], cutoff) for lang in languages}


# ── threshold sweep ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SweepRow:
    threshold: float
    answered: int  # should-answer rows that would be answered at this threshold
    answer_total: int
    refused: int  # should-not-answer rows that would be refused
    refuse_total: int

    @property
    def answer_rate(self) -> float | None:
        return self.answered / self.answer_total if self.answer_total else None

    @property
    def refusal_accuracy(self) -> float | None:
        return self.refused / self.refuse_total if self.refuse_total else None

    @property
    def balanced(self) -> float | None:
        exact = self.balanced_exact
        return float(exact) if exact is not None else None

    @property
    def balanced_exact(self) -> Fraction | None:
        """The mean of answer rate and refusal accuracy, exact, so ties are real ties. None without both kinds."""
        if not self.answer_total or not self.refuse_total:
            return None
        return (Fraction(self.answered, self.answer_total) + Fraction(self.refused, self.refuse_total)) / 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold, "answer_rate": self.answer_rate, "refusal_accuracy": self.refusal_accuracy,
            "balanced": self.balanced, "answered": self.answered, "answer_total": self.answer_total,
            "refused": self.refused, "refuse_total": self.refuse_total,
        }


@dataclass(frozen=True)
class SweepSummary:
    best_balanced: SweepRow | None
    highest_keeping_answer_rate: SweepRow | None
    min_answer_rate: float


def sweep(
    results: Sequence[QuestionResult],
    signal: str = "similarity",
    thresholds: Sequence[float] = THRESHOLDS,
) -> list[SweepRow]:
    """For each threshold: how many should-answer rows have the stored signal at or above it (they would be
    answered), and how many should-not-answer rows stay below it (they would be refused). `signal` is
    "similarity" (the best vector similarity of the hits) or "keyword_coverage" (the best share of the
    question's words that a hit matched). A question without the signal is never answered."""
    if signal not in ("similarity", "keyword_coverage"):
        raise ValueError("signal must be 'similarity' or 'keyword_coverage'")

    def value(r: QuestionResult) -> float | None:
        return r.max_similarity if signal == "similarity" else r.keyword_coverage

    answers = [value(r) for r in results if r.should_answer]
    refusals = [value(r) for r in results if not r.should_answer]
    rows = []
    for t in thresholds:
        rows.append(
            SweepRow(
                threshold=t,
                answered=sum(1 for v in answers if v is not None and v >= t),
                answer_total=len(answers),
                refused=sum(1 for v in refusals if v is None or v < t),
                refuse_total=len(refusals),
            )
        )
    return rows


def summarize_sweep(rows: Sequence[SweepRow]) -> SweepSummary:
    """The best balanced threshold (highest mean of answer rate and refusal accuracy; when several thresholds
    tie, the highest of them, which refuses more) and the highest threshold that keeps the answer rate at or
    above MIN_ANSWER_RATE. Either is None when it cannot be decided (no rows of one kind, or no threshold in
    the range qualifies)."""
    balanced = [r for r in rows if r.balanced_exact is not None]
    best = max(balanced, key=lambda r: (r.balanced_exact or Fraction(0), r.threshold)) if balanced else None
    wanted = Fraction(str(MIN_ANSWER_RATE))
    keeping = [r for r in rows if r.answer_total and Fraction(r.answered, r.answer_total) >= wanted]
    return SweepSummary(best, max(keeping, key=lambda r: r.threshold) if keeping else None, MIN_ANSWER_RATE)


def _sweep_section(results: Sequence[QuestionResult], signal: str) -> dict[str, Any]:
    rows = sweep(results, signal)
    summary = summarize_sweep(rows)
    return {
        "signal": signal,
        "min_answer_rate": summary.min_answer_rate,
        "rows": [r.to_dict() for r in rows],
        "best_balanced": summary.best_balanced.to_dict() if summary.best_balanced else None,
        "highest_keeping_answer_rate": (
            summary.highest_keeping_answer_rate.to_dict() if summary.highest_keeping_answer_rate else None
        ),
    }


def build_report(results: Sequence[QuestionResult], *, cutoff: int, meta: dict[str, Any]) -> dict[str, Any]:
    """Everything, as plain JSON-able data. `meta` is the caller's description of the run (tenant, ids, model,
    the configured threshold, the corpus); it is copied in as given."""
    return {
        "meta": meta,
        "overall": aggregate(results, cutoff),
        "by_language": by_language(results, cutoff),
        "questions": [r.to_dict() for r in results],
        "sweep": _sweep_section(results, "similarity"),
        "keyword_coverage_sweep": _sweep_section(results, "keyword_coverage"),
    }


# ── plain-English summary ────────────────────────────────────────────────────


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _sim(value: float | None) -> str:
    return "none" if value is None else f"{value:.3f}"


def _pages(start: int | None, end: int | None) -> str:
    if start is None and end is None:
        return "no page"
    return f"p.{start}" if end in (None, start) else f"p.{start}-{end}"


def _cause(q: dict[str, Any], threshold: float) -> str:
    outcome = q["outcome"]
    where = f"{safe_text(q['document'], 40)}, {safe_text(q['expected_location'], 40)}"
    if outcome == DOCUMENT_NOT_IN_SCOPE:
        return (f"the document is not in this class and subject: {safe_text(q['document'], 40)!r} matches no published "
                "document here (check the Document column, or publish the file)")
    if outcome == NO_CANDIDATE:
        return "no candidate: nothing in this class and subject was retrieved for the question at all"
    if outcome == WRONG_CHUNK:
        return f"wrong chunk: the retrieved chunks do not include the expected place ({where})"
    if outcome == BELOW_THRESHOLD:
        return (f"found but below the threshold: the right chunk was retrieved (position {q['hit_position']}) but the best "
                f"similarity {_sim(q['max_similarity'])} is under {threshold}, so the question would be refused")
    if outcome == ANSWERED_ANYWAY:
        return (f"answered although it should have refused: best similarity {_sim(q['max_similarity'])} is at or above "
                f"{threshold}")
    return str(outcome)


def _hit_columns(metrics: dict[str, Any], ks: Sequence[str]) -> str:
    hits = metrics["hit_at"]
    return "  ".join(f"{_pct(hits[k]) if hits else 'n/a':>6}" for k in ks)


def format_console(report: dict[str, Any]) -> str:
    """The summary the command prints at the end: what was measured, what the numbers mean, which questions
    failed and why, and the threshold sweep. Titles and headings come from uploaded files and are cleaned."""
    meta = report["meta"]
    overall = report["overall"]
    cutoff = overall["mrr_cutoff"]
    threshold = meta.get("threshold")
    ks = list(overall["hit_at"]) if overall["hit_at"] else [str(k) for k in sorted({*HIT_KS, cutoff})]
    lines: list[str] = ["", "=== Retrieval evaluation ==="]
    lines.append(
        f"tenant {safe_text(meta.get('tenant'), 40)}, class {meta.get('class_id')}, subject {meta.get('subject_id')}, "
        f"model {safe_text(meta.get('model'), 60)}, k = {meta.get('k', cutoff)}"
    )
    corpus = meta.get("corpus") or {}
    lines.append("")
    lines.append(f"Corpus (published versions only): {len(corpus.get('documents', []))} document(s), "
                 f"{corpus.get('chunks', 0)} chunk(s), {corpus.get('flagged_chunks', 0)} flagged by the injection scanner")
    for d in corpus.get("documents", []):
        lines.append(f"  - {safe_text(d['title'], 60)} ({safe_text(d['original_filename'], 40)}): "
                     f"{d['chunks']} chunks, {d['flagged']} flagged")
    cap = meta.get("keyword_max_candidates")
    if isinstance(cap, int) and corpus:
        chunks = corpus.get("chunks", 0)
        lines.append(
            f"Keyword cap (backlog B97): the keyword side ranks at most {cap} matching chunks; this scope has "
            f"{chunks}, so the cap {'CAN' if chunks > cap else 'cannot'} drop a better match here"
            + (" (check the keyword-only numbers below)" if chunks > cap else "")
        )
    calibrated = meta.get("threshold_calibrated")
    lines.append("")
    lines.append(f"Current setting: RETRIEVAL_MIN_SIMILARITY = {threshold} "
                 f"({'calibrated' if calibrated else 'not calibrated yet: a default, not a measured value'})")
    fusion = meta.get("fusion")
    if isinstance(fusion, dict):
        df = fusion.get("keyword_max_df")
        lines.append(
            f"Keyword and fusion settings (step 5d-4): keyword max document frequency = {'off' if df == 1.0 else df}, "
            f"weights vector / keyword = {fusion.get('rrf_vector_weight')} / {fusion.get('rrf_keyword_weight')}, "
            f"min matched terms = {fusion.get('keyword_min_matched_terms')}"
        )

    lines += ["", "Results by language (should-answer rows: hit@k, MRR, answer rate; should-not-answer rows: refusal accuracy)"]
    header = f"  {'Language':<14} {'ans/ref':>8}  " + "  ".join(f"{'hit@' + k:>6}" for k in ks) + \
        f"  {'MRR@' + str(cutoff):>7}  {'answer rate':>11}  {'refusal accuracy':>16}"
    lines.append(header)
    groups = [*report["by_language"].items(), ("ALL", overall)]
    for name, m in groups:
        mrr = "n/a" if m["mrr"] is None else f"{m['mrr']:.2f}"
        lines.append(
            f"  {safe_text(name, 14):<14} {str(m['should_answer']) + '/' + str(m['should_not_answer']):>8}  "
            f"{_hit_columns(m, ks)}  {mrr:>7}  {_pct(m['answer_rate']):>11}  {_pct(m['refusal_accuracy']):>16}"
        )
    lines += [
        "",
        "What the numbers mean",
        "  hit@k: for the questions that have an answer, how often the right chunk is among the first k results.",
        f"  MRR@{cutoff}: the average of 1 / (position of the right chunk); 1.00 means always first.",
        "  answer rate: how many should-answer questions pass the similarity threshold and would be answered.",
        "  refusal accuracy: how many should-not-answer questions stay under the threshold and are refused.",
        "  A high answer rate with a low refusal accuracy means the threshold is too low; the reverse, too high.",
    ]

    lines += ["", "Keyword only vs vector only (input for the trigram decision, backlog B92)"]
    lines.append(f"  {'Language':<14} " + "  ".join(f"{'keyword ' + k:>9}" for k in ks) + "  " +
                 "  ".join(f"{'vector ' + k:>8}" for k in ks))
    for name, m in groups:
        kw = m["keyword_only_hit_at"]
        vec = m["vector_only_hit_at"]
        kw_cols = "  ".join(f"{_pct(kw[k]) if kw else 'n/a':>9}" for k in ks)
        vec_cols = "  ".join(f"{_pct(vec[k]) if vec else 'n/a':>8}" for k in ks)
        lines.append(f"  {safe_text(name, 14):<14} {kw_cols}  {vec_cols}")
    lines.append("  If a language's keyword-only hit rate is far below its vector-only rate, inflected words are being")
    lines.append("  missed by the keyword side; that is the case for the trigram proposal (B92).")

    failed = [q for q in report["questions"] if q["outcome"] not in (OK, REFUSED_CORRECTLY)]
    lines += ["", f"Questions that failed: {len(failed)} of {len(report['questions'])}"]
    for q in failed:
        lines.append(f"  #{q['number']} [{safe_text(q['language'], 20)}] \"{safe_text(q['question'], 70)}\"")
        lines.append(f"      {_cause(q, threshold if isinstance(threshold, (int, float)) else 0.0)}")
        for t in q["top"][:3]:
            marker = "  <- expected" if t["is_match"] else ""
            lines.append(
                f"      {t['rank']}. {safe_text(t['document_title'], 40)} {_pages(t['page_start'], t['page_end'])} "
                f"\"{safe_text(t['heading_path'], 50)}\" similarity {_sim(t['vector_similarity'])}{marker}"
            )
    if not failed:
        lines.append("  none")

    sweep_data = report["sweep"]
    best = sweep_data["best_balanced"]
    keeping = sweep_data["highest_keeping_answer_rate"]
    lines += ["", "Threshold sweep (from the stored best similarity of each question; no new search)"]
    lines.append(f"  {'threshold':>9}  {'answer rate':>11}  {'refusal accuracy':>16}  {'balanced':>8}")
    for r in sweep_data["rows"]:
        marks = []
        if best and r["threshold"] == best["threshold"]:
            marks.append("best balanced")
        if keeping and r["threshold"] == keeping["threshold"]:
            marks.append("highest keeping 90% answered")
        if threshold is not None and r["threshold"] == threshold:
            marks.append("current setting")
        lines.append(
            f"  {r['threshold']:>9.2f}  {_pct(r['answer_rate']):>11}  {_pct(r['refusal_accuracy']):>16}  "
            f"{_pct(r['balanced']):>8}" + (f"   <- {', '.join(marks)}" if marks else "")
        )
    if best:
        lines.append(f"  best balanced threshold: {best['threshold']:.2f} (answer rate {_pct(best['answer_rate'])}, "
                     f"refusal accuracy {_pct(best['refusal_accuracy'])}; the highest of any ties)")
    else:
        lines.append("  best balanced threshold: not available (it needs both should-answer and should-not-answer rows)")
    if keeping:
        lines.append(f"  highest threshold that keeps the answer rate at or above 90%: {keeping['threshold']:.2f} "
                     f"(refusal accuracy {_pct(keeping['refusal_accuracy'])})")
    else:
        lines.append("  highest threshold that keeps the answer rate at or above 90%: none between 0.70 and 0.95")
    lines.append("  Choose the threshold yourself from this table, then see docs/api/phase5.md (section 12) to set it.")
    lines.append("")
    return "\n".join(lines)


# ── comparing retrieval configurations (step 5d-4, `eval_retrieval --compare`) ───────────────────────────────

DEFAULT_VECTOR_WEIGHTS: tuple[float, ...] = (1.0, 2.0, 3.0)
DEFAULT_MAX_DFS: tuple[float, ...] = (1.0, 0.5, 0.3, 0.2, 0.1)  # 1.0 = pruning off
DEFAULT_MIN_TERMS: tuple[int, ...] = (1, 2)
MAX_GRID_POINTS = 200  # a grid is one search per question per point; more than this is a typing mistake
MAX_MIN_TERMS = 64  # the most lexemes a query has (retrieval.MAX_QUERY_LEXEMES)
MIN_ANSWER_ROWS = 20  # fewer should-answer rows than this: small differences are noise
MIN_REFUSAL_ROWS = 8  # the same for should-not-answer rows
GRID_NAMES = ("vw", "df", "mt")

RunRole = Literal["current", "baseline", "grid"]


class GridError(ValueError):
    """The --grid argument cannot be used (the message says which part)."""


def _num(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True)
class GridPoint:
    """One retrieval configuration of the comparison, as plain numbers (the command turns it into a config)."""

    vector_weight: float
    keyword_weight: float
    keyword_max_df: float  # 1.0 = pruning off
    keyword_min_matched_terms: int

    @property
    def label(self) -> str:
        df = "off" if self.keyword_max_df >= 1.0 else _num(self.keyword_max_df)
        return (
            f"vw={_num(self.vector_weight)} kw={_num(self.keyword_weight)} df={df} "
            f"mt={self.keyword_min_matched_terms}"
        )


def vector_only_point() -> GridPoint:
    """The baseline: the keyword side has weight 0, so the ranking is the vector side alone."""
    return GridPoint(1.0, 0.0, 1.0, 1)


def build_grid(
    vector_weights: Sequence[float] = DEFAULT_VECTOR_WEIGHTS,
    max_dfs: Sequence[float] = DEFAULT_MAX_DFS,
    min_terms: Sequence[int] = DEFAULT_MIN_TERMS,
    keyword_weight: float = 1.0,
) -> list[GridPoint]:
    """Every combination, vector weight outermost and minimum matched terms innermost. The default grid is
    3 x 5 x 2 = 30 points, the first one the neutral configuration (weights 1, no pruning, one term)."""
    return [
        GridPoint(vw, keyword_weight, df, mt) for vw, df, mt in itertools.product(vector_weights, max_dfs, min_terms)
    ]


def _grid_values(name: str, raw: str) -> list[Any]:
    parts = [p.strip() for p in raw.split(",")]
    if not raw.strip() or any(not p for p in parts):
        raise GridError(f"{name}= needs one or more values separated by commas")
    try:
        if name == "mt":
            ints = [int(p) for p in parts]
            if any(not 1 <= n <= MAX_MIN_TERMS for n in ints):
                raise GridError(f"mt= values must be whole numbers from 1 to {MAX_MIN_TERMS}")
            return list(dict.fromkeys(ints))
        floats = [1.0 if (name == "df" and p.casefold() == "off") else float(p) for p in parts]
    except ValueError:
        raise GridError(f"{name}= has a value that is not a number: {safe_text(raw, 40)!r}") from None
    if name == "vw" and any(not (math.isfinite(v) and v > 0) for v in floats):
        raise GridError("vw= values must be numbers above 0")
    if name == "df" and any(not 0.0 <= v <= 1.0 for v in floats):
        raise GridError("df= values must be 'off' or numbers from 0 to 1")
    return list(dict.fromkeys(floats))


def parse_grid(tokens: Sequence[str]) -> list[GridPoint]:
    """A custom grid from `vw=1,2 df=off,0.3 mt=1,2` (any of the three; a dimension that is left out stays at
    its neutral value: vw=1, df=off, mt=1). Raises GridError, with a message the operator can act on."""
    if not tokens:
        raise GridError("--grid needs at least one of vw=, df=, mt= (for example: --grid vw=1,2 df=off,0.3)")
    chosen: dict[str, list[Any]] = {}
    for token in tokens:
        name, sep, raw = token.partition("=")
        if not sep or name not in GRID_NAMES:
            raise GridError(f"{safe_text(token, 40)!r} is not one of vw=<list>, df=<list>, mt=<list>")
        if name in chosen:
            raise GridError(f"{name}= is given twice")
        chosen[name] = _grid_values(name, raw)
    size = math.prod(len(v) for v in chosen.values())
    if size > MAX_GRID_POINTS:
        raise GridError(f"the grid has {size} points; at most {MAX_GRID_POINTS} are allowed")
    return build_grid(chosen.get("vw", [1.0]), chosen.get("df", [1.0]), chosen.get("mt", [1]))


@dataclass(frozen=True)
class ConfigRun:
    """The results of the golden set under one configuration. `role` is "current" (the settings as they are
    now: the reference of every comparison), "baseline" (vector only) or "grid"; `label` is the tag the table
    shows for the first two."""

    role: RunRole
    label: str
    point: GridPoint
    results: tuple[QuestionResult, ...]


def hit1_changes(reference: Sequence[QuestionResult], other: Sequence[QuestionResult]) -> tuple[int, int]:
    """(wins, losses) among the should-answer questions: how many have the right chunk at position 1 in `other`
    but not in `reference` (wins), and the other way round (losses). The two lists must be the same questions
    in the same order."""
    if len(reference) != len(other) or any(a.row != b.row for a, b in zip(reference, other, strict=True)):
        raise ValueError("the two runs are not about the same questions")
    wins = losses = 0
    for a, b in zip(reference, other, strict=True):
        if not a.should_answer:
            continue
        was, now = a.hit_position == 1, b.hit_position == 1
        wins += 1 if now and not was else 0
        losses += 1 if was and not now else 0
    return wins, losses


def sample_warnings(answer_rows: int, refusal_rows: int) -> list[str]:
    """The overfitting guard: a warning for each kind of row that the golden set has too few of."""
    warnings = []
    if answer_rows < MIN_ANSWER_ROWS:
        warnings.append(
            f"only {answer_rows} should-answer row(s), fewer than {MIN_ANSWER_ROWS}: too few to trust small differences"
        )
    if refusal_rows < MIN_REFUSAL_ROWS:
        warnings.append(
            f"only {refusal_rows} should-not-answer row(s), fewer than {MIN_REFUSAL_ROWS}: "
            "too few to trust small differences"
        )
    return warnings


def build_compare_report(runs: Sequence[ConfigRun], *, cutoff: int, meta: dict[str, Any]) -> dict[str, Any]:
    """The comparison as plain JSON-able data: one entry per configuration (numbers only, no per-question rows)
    with its changes against the "current" run, and the sizes of the golden set with the warnings."""
    if not runs:
        raise ValueError("a comparison needs at least one run")
    reference = next((r for r in runs if r.role == "current"), runs[0])
    answers = sum(1 for q in reference.results if q.should_answer)
    refusals = len(reference.results) - answers
    entries = []
    for run in runs:
        wins, losses = hit1_changes(reference.results, run.results)
        entries.append(
            {
                "role": run.role,
                "label": run.label,
                "point_label": run.point.label,
                "vector_weight": run.point.vector_weight,
                "keyword_weight": run.point.keyword_weight,
                "keyword_max_df": run.point.keyword_max_df,
                "keyword_min_matched_terms": run.point.keyword_min_matched_terms,
                "overall": aggregate(run.results, cutoff),
                "by_language": by_language(run.results, cutoff),
                "answered": sum(1 for q in run.results if q.should_answer and q.sufficient),
                "refused": sum(1 for q in run.results if not q.should_answer and not q.sufficient),
                "hit1_wins": wins,
                "hit1_losses": losses,
            }
        )
    return {
        "meta": meta,
        "sample": {
            "should_answer": answers,
            "should_not_answer": refusals,
            "warnings": sample_warnings(answers, refusals),
        },
        "runs": entries,
    }


def _cells(metrics: dict[str, Any]) -> str:
    hits = metrics["hit_at"] or {}
    mrr = "n/a" if metrics["mrr"] is None else f"{metrics['mrr']:.2f}"
    return " ".join(f"{_pct(hits.get(k)):>5}" for k in ("1", "3", "5")) + f" {mrr:>5}"


def format_compare(report: dict[str, Any]) -> str:
    """The one table of `--compare`, with the sizes of the golden set and the warnings above it."""
    meta = report["meta"]
    sample = report["sample"]
    runs = report["runs"]
    languages = list(runs[0]["by_language"]) if runs else []
    lines = ["", "=== Retrieval comparison ==="]
    lines.append(
        f"tenant {safe_text(meta.get('tenant'), 40)}, class {meta.get('class_id')}, subject {meta.get('subject_id')}, "
        f"model {safe_text(meta.get('model'), 60)}, k = {meta.get('k')}, {len(runs)} configuration(s)"
    )
    lines.append(
        f"Golden set: {sample['should_answer']} should-answer and {sample['should_not_answer']} "
        "should-not-answer row(s)"
    )
    lines += [f"  WARNING: {w}" for w in sample["warnings"]]
    calibrated = meta.get("threshold_calibrated")
    lines.append(
        f"Answer and refusal numbers use the current setting RETRIEVAL_MIN_SIMILARITY = {meta.get('threshold')} "
        f"({'calibrated' if calibrated else 'not calibrated yet: a default, not a measured value'})"
    )
    group_names = ["ALL", *(safe_text(name, 10) for name in languages)]
    group_head = " | ".join(f"{name:<27}" for name in group_names)
    column_head = " | ".join(f"{'hit@1':>5} {'hit@3':>5} {'hit@5':>5} {'MRR':>5}".ljust(27) for _ in group_names)
    head1 = f"  {'':<16} {'settings':<22} {group_head} | {'answered':>8} {'refused':>7} | {'hit@1 vs':>8}"
    head2 = f"  {'':<16} {'':<22} {column_head} | {'':>8} {'':>7} | {'current':>8}"
    lines += ["", head1, head2, "  " + "-" * (len(head2) - 2)]
    for run in runs:
        tag = run["label"] if run["role"] != "grid" else ""
        groups = [_cells(run["overall"]), *(_cells(run["by_language"][name]) for name in languages)]
        answered = f"{run['answered']}/{sample['should_answer']}"
        refused = f"{run['refused']}/{sample['should_not_answer']}"
        change = f"+{run['hit1_wins']}/-{run['hit1_losses']}"
        lines.append(
            f"  {safe_text(tag, 16):<16} {safe_text(run['point_label'], 22):<22} "
            + " | ".join(f"{g:<27}" for g in groups)
            + f" | {answered:>8} {refused:>7} | {change:>8}"
        )
    lines += [
        "",
        "How to read it",
        "  vw / kw: fusion weight of the vector / keyword side (kw=0 is the vector-only baseline).",
        "  df: a keyword word found in more than this share of the chunks is dropped (off = nothing dropped).",
        "  mt: a chunk needs at least this many of the (remaining) question words to enter the keyword ranking.",
        "  hit@k, MRR: for the should-answer questions only; ALL, then one column group per language.",
        "  answered / refused: should-answer questions at or above the threshold / should-not-answer ones below it.",
        "  hit@1 vs current: +W/-L = W questions whose right chunk became first, L that stopped being first,",
        "  compared with the 'current settings' row. Many small wins and losses that cancel out are noise.",
        "  Nothing is changed by this command: choose values yourself, then see docs/api/phase5.md (section 12).",
        "",
    ]
    return "\n".join(lines)
