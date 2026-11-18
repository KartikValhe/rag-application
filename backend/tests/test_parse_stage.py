"""
The parse stage (app/workers/stages.py, step 5b-3): the stored original is read from the PRIVATE store
through the tenant-prefix check, parsed in the sandboxed child with the tenant's caps, and the child's
answer is validated by the PARENT before it is used. Errors map to the codes the worker understands.

Most tests run the real stage and the real sandbox with a StageContext that needs no database (the
parse stage only reads the store); the end-to-end tests at the bottom go through the real worker and
the test database. EVERY test checks that no child process, temp directory or stored file is left behind.
"""

import random
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import document_store
from app.services.document_limits import DocumentLimits, default_limits
from app.services.parsed_document import ParsedDocument
from app.workers import errors, ingest, stages
from app.workers.queue import Job
from tests.fixtures.parse_files import (
    docx_bytes,
    leftover_guard,
    make_text_pdf,
    pptx_bytes,
    rewrite_zip_member,
)
from tests.fixtures.worker import FAST
from tests.security.parse_assertions import (
    assert_corrupt_child_output_is_rejected,
    assert_page_bomb_is_refused,
    assert_parent_refuses_inflated_page_count,
)

PARSE = stages.ParseSettings(timeout_seconds=60.0, max_output_bytes=20 * 1024 * 1024, memory_limit_mb=1024, cpu_seconds=120)
CONFIG = replace(FAST, parse=PARSE)
HIDDEN_PAGES = b"/Type" + b" " * 40 + b"/Page"
TEXT = "A page of syllabus text that is long enough for the scanned gate."


@pytest.fixture
def store_root(tmp_path: Path) -> Path:
    return tmp_path / "private_store"


@pytest.fixture(autouse=True)
def no_leftovers(monkeypatch: pytest.MonkeyPatch, store_root: Path) -> Iterator[None]:
    """Every test in this module: no child process, no sandbox temp directory, no new stored file."""
    store_root.mkdir(parents=True, exist_ok=True)
    with leftover_guard(monkeypatch, store_root):
        yield


async def _no_session() -> Any:
    raise AssertionError("the parse stage must not need a database transaction")


def offline_context(
    store_root: Path, data: bytes, kind: str, *, limits: DocumentLimits | None = None, tenant: uuid.UUID | None = None
) -> stages.StageContext:
    """A StageContext with a stored file and no database (the parse stage reads the store only)."""
    tenant_id = tenant or uuid.uuid4()
    store = document_store.LocalPrivateStore(store_root)
    key = document_store.new_document_key(tenant_id)
    job = Job(id=uuid.uuid4(), tenant_id=tenant_id, version_id=uuid.uuid4(), attempts=1, worker_id="test")
    version = stages.VersionInfo(
        id=job.version_id, document_id=uuid.uuid4(), subject_id=uuid.uuid4(), file_kind=kind, storage_key=key, size_bytes=len(data)
    )
    return stages.StageContext(
        job=job, version=version, limits=limits or default_limits(), config=CONFIG, store=store,
        tenant_sessions=None,  # type: ignore[arg-type]
        _session=_no_session,  # type: ignore[arg-type]
    )


async def parse(store_root: Path, data: bytes, kind: str, **kwargs: Any) -> ParsedDocument:
    """Store the file, run the REAL parse stage, remove the file again; return the parsed document."""
    stage_kwargs = {k: kwargs.pop(k) for k in ("max_text_chars", "pages_quota_hook") if k in kwargs}
    ctx = offline_context(store_root, data, kind, **kwargs)
    await ctx.store.put(ctx.job.tenant_id, ctx.version.storage_key, data)
    try:
        await stages.make_parse_stage(ctx.store, PARSE, **stage_kwargs)(ctx)
    finally:
        await ctx.store.delete(ctx.job.tenant_id, ctx.version.storage_key)
    parsed = ctx.results["parsed"]
    assert isinstance(parsed, ParsedDocument)
    return parsed


async def code_of(store_root: Path, data: bytes, kind: str, **kwargs: Any) -> str:
    with pytest.raises(errors.PermanentIngestError) as exc:
        await parse(store_root, data, kind, **kwargs)
    return exc.value.code


# ── Store access ─────────────────────────────────────────────────────────────


async def test_the_original_is_read_from_the_private_store(store_root: Path) -> None:
    doc = await parse(store_root, make_text_pdf([TEXT, None, TEXT]), "pdf")
    assert (doc.file_kind, doc.page_count, [p.n for p in doc.pages]) == ("pdf", 3, [1, 3])


async def test_a_key_of_another_tenant_is_refused_without_reading(store_root: Path) -> None:
    other = uuid.uuid4()
    store = document_store.LocalPrivateStore(store_root)
    key = document_store.new_document_key(other)
    await store.put(other, key, b"Unit 1: Algebra")
    try:
        ctx = offline_context(store_root, b"x", "txt")
        ctx.version = replace(ctx.version, storage_key=key)  # the version row points at tenant B's file
        with pytest.raises(errors.PermanentIngestError) as exc:
            await stages.make_parse_stage(ctx.store, PARSE)(ctx)
        assert exc.value.code == "processing_failed"
        assert "parsed" not in ctx.results
    finally:
        await store.delete(other, key)


async def test_a_missing_file_is_permanent(store_root: Path) -> None:
    ctx = offline_context(store_root, b"x", "txt")
    with pytest.raises(errors.PermanentIngestError) as exc:
        await stages.make_parse_stage(ctx.store, PARSE)(ctx)
    assert exc.value.code == "processing_failed"


# ── Parent-side validation of the child's answer ─────────────────────────────


@pytest.mark.parametrize(
    ("variant", "code"),
    [
        ("control", "parse_output_invalid"),
        ("extra_key", "parse_output_invalid"),
        ("wrong_type", "parse_output_invalid"),
        ("negative_page", "parse_output_invalid"),
        ("no_document", "parse_output_invalid"),
        ("not_a_dict", "parse_output_invalid"),
        ("stats_lie", "parse_output_invalid"),
        ("kind_lie", "parse_output_invalid"),
        ("too_many_blocks", "parse_output_too_large"),
        ("page_count_lie", "parse_output_invalid"),  # a "pdf" document for a txt version
        ("unknown_code", "parse_failed"),
    ],
)
async def test_a_corrupt_child_answer_is_rejected_by_the_parent(store_root: Path, variant: str, code: str) -> None:
    async def run() -> ParsedDocument:
        return await stages.parse_in_sandbox(
            b"Unit 1", file_kind="txt", limits=default_limits(), parse=PARSE,
            child_module="tests._parse_children", child_args=(f"corrupt-{variant}",),
        )

    if code != "parse_failed":  # an unknown failure code is a refusal too, but not a corrupt document
        await assert_corrupt_child_output_is_rejected(run)
    with pytest.raises(errors.PermanentIngestError) as exc:
        await run()
    assert exc.value.code == code


async def test_the_parent_rechecks_the_page_count_the_child_reports(store_root: Path) -> None:
    """A compromised child claims a valid-looking PDF of 500 pages (one short page of text) for a short
    file: the PARENT's own check against the tenant's cap of 200 refuses it. Driven through
    run_parse_child, so deleting the check_page_limits call fails this test (meta-test in
    tests/security/test_prove_it_fails_review5b3.py)."""
    await assert_parent_refuses_inflated_page_count(
        lambda variant, kind: stages.run_parse_child(
            b"short file", file_kind=kind, limits=default_limits(), parse=PARSE,
            child_module="tests._parse_children", child_args=(f"corrupt-{variant}",),
        )
    )


@pytest.mark.parametrize(("variant", "kind", "pages"), [("pdf_page_count_200", "pdf", 200), ("pptx_page_count_100", "pptx", 100)])
async def test_a_page_count_exactly_at_the_cap_is_accepted(store_root: Path, variant: str, kind: str, pages: int) -> None:
    outcome = await stages.run_parse_child(
        b"short file", file_kind=kind, limits=default_limits(), parse=PARSE,
        child_module="tests._parse_children", child_args=(f"corrupt-{variant}",),
    )
    assert outcome.document.page_count == pages


@pytest.mark.parametrize(
    "code",
    [
        "parse_output_invalid", "parse_output_too_large", "text_too_large", "scanned_pdf", "unreadable_text", "no_text_found",
        "too_many_pages", "too_many_slides", "corrupt_pdf", "password_protected_pdf", "parse_timeout", "parse_failed",
        "quota_pages_exceeded", "xml_too_complex", "pdf_too_complex",
    ],
)
async def test_a_parse_error_code_is_permanent_through_the_worker_and_never_retried(
    code: str, worker_dbs: Any, academic_world: Any, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None, ingest_slots: Any,
) -> None:
    """Behaviour, not a constant: a stage that raises this code fails the version with it after ONE attempt,
    and the job is never claimed again."""
    from tests.fixtures.worker import (
        FakeClock,
        FakePipeline,
        job_row,
        make_context,
        queued_version,
        version_row,
    )
    from tests.security.worker_assertions import assert_no_slots_held

    made = await queued_version(raw_db, academic_world.a, tmp_path)
    clock = FakeClock(datetime.now(UTC))
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=CONFIG, clock=clock)
    pipeline = FakePipeline()

    async def boom(_ctx: stages.StageContext) -> None:
        raise errors.PermanentIngestError(code)

    pipeline.hooks["parse"] = boom
    assert await ingest.run_one(ctx, pipeline.stages()) == ingest.JobOutcome.FAILED
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code) == ("failed", code)
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"], row["last_error_code"]) == ("failed", 1, code)
    clock.advance(86_400)
    assert await ctx.queue.claim("again") is None
    await assert_no_slots_held(ingest_slots, academic_world.a.id)


async def test_timeout_is_permanent_and_crash_is_transient(store_root: Path, tmp_path: Path) -> None:
    slow = stages.ParseSettings(timeout_seconds=1.0, max_output_bytes=1024, memory_limit_mb=1024, cpu_seconds=60)
    with pytest.raises(errors.PermanentIngestError) as exc:
        await stages.parse_in_sandbox(
            b"x", file_kind="txt", limits=default_limits(), parse=slow, child_module="tests._sandbox_children",
            child_args=("sleep",), extra_params={"pid_file": str(tmp_path / "pid")},
        )
    assert exc.value.code == "parse_timeout"
    with pytest.raises(errors.TransientIngestError) as crash:
        await stages.parse_in_sandbox(
            b"x", file_kind="txt", limits=default_limits(), parse=PARSE, child_module="tests._sandbox_children",
            child_args=("boom",),
        )
    assert crash.value.code == "worker_crash"


# ── Hostile files through the real stage and the sandbox ─────────────────────


async def baseline_seconds(store_root: Path) -> float:
    """What one trivial parse costs on this machine (process start, imports, the sandbox round trip)."""
    started = time.monotonic()
    await parse(store_root, b"Unit 1: Algebra", "txt")
    return time.monotonic() - started


def generous(baseline: float, *, factor: float = 60.0, floor: float = 15.0) -> float:
    """A wall-clock bound that follows the machine: `factor` times a trivial parse, never below `floor` s.
    These tests catch a hang or a quadratic blow-up (minutes), not a slow disk."""
    return max(floor, baseline * factor)


async def test_huge_page_dimensions(store_root: Path) -> None:
    assert await code_of(store_root, make_text_pdf([TEXT], width=1e6, height=1e6), "pdf") == "pdf_too_complex"


@pytest.mark.machine_dependent
async def test_a_huge_content_stream_without_text(store_root: Path) -> None:
    ops = b"0 0 m 100 100 l S\n" * 400_000  # 7 MB of drawing operators on one page
    bound = generous(await baseline_seconds(store_root))
    started = time.monotonic()
    assert await code_of(store_root, make_text_pdf([None], extra_content=ops), "pdf") == "scanned_pdf"
    assert time.monotonic() - started < bound


async def test_a_huge_text_content_stream_hits_the_per_page_cap(store_root: Path) -> None:
    ops = b"BT /F1 1 Tf 0 0 Td " + b"(abcdefghij) Tj " * 60_000 + b"ET\n"  # 600,000 characters on one page
    assert await code_of(store_root, make_text_pdf([None], extra_content=ops), "pdf") == "text_too_large"


async def test_deeply_nested_objects(store_root: Path) -> None:
    nested = b"/Deep " + b"[" * 50_000 + b"]" * 50_000 + b" "
    nested_dict = b"/Deeper " + b"<< /A " * 20_000 + b"1" + b" >>" * 20_000 + b" "
    for extra in (nested, nested_dict):
        assert (await parse(store_root, make_text_pdf([TEXT], extra_catalog=extra), "pdf")).page_count == 1


async def test_ten_thousand_empty_pages(store_root: Path) -> None:
    visible = make_text_pdf([None] * 10_000, bare_empty_pages=True)
    assert await code_of(store_root, visible, "pdf") == "too_many_pages"  # the upload scan already counts them

    async def hidden() -> ParsedDocument:  # pages the scan cannot count: the parser's re-check refuses
        return await parse(store_root, make_text_pdf([None] * 10_000, bare_empty_pages=True, page_type_token=HIDDEN_PAGES), "pdf")

    await assert_page_bomb_is_refused(hidden)


async def test_page_cap_boundary_with_pages_the_scan_cannot_see(store_root: Path) -> None:
    limits = replace(default_limits(), max_pdf_pages=5)
    assert (await parse(store_root, make_text_pdf([TEXT] * 5, page_type_token=HIDDEN_PAGES), "pdf", limits=limits)).page_count == 5
    assert await code_of(store_root, make_text_pdf([TEXT] * 6, page_type_token=HIDDEN_PAGES), "pdf", limits=limits) == "too_many_pages"


async def test_a_tenant_with_raised_limits_accepts_what_the_default_rejects(store_root: Path) -> None:
    pdf = make_text_pdf([TEXT] * 201, page_type_token=HIDDEN_PAGES)
    assert await code_of(store_root, pdf, "pdf") == "too_many_pages"
    raised = replace(default_limits(), max_pdf_pages=300)
    assert (await parse(store_root, pdf, "pdf", limits=raised)).page_count == 201


@pytest.mark.machine_dependent
async def test_a_docx_with_a_2_mb_single_paragraph(store_root: Path) -> None:
    bound = generous(await baseline_seconds(store_root), factor=120.0)
    started = time.monotonic()
    two_mb = _random_words(2 * 1024 * 1024)  # random words: repeated text would trip the zip-bomb ratio
    assert await code_of(store_root, docx_bytes(lambda d: d.add_paragraph(two_mb)), "docx") == "text_too_large"
    almost = two_mb[:1_900_000]
    doc = await parse(store_root, docx_bytes(lambda d: d.add_paragraph(almost)), "docx")
    assert doc.stats.chars >= 1_899_000 and doc.stats.blocks >= 190
    assert time.monotonic() - started < bound


_RNG = random.Random(5)  # one stream for the module: two calls give different (but reproducible) text
_WORDS = ["".join(_RNG.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(_RNG.randint(2, 9))) for _ in range(5000)]


def _random_words(size: int) -> str:
    out = " ".join(_RNG.choice(_WORDS) for _ in range(size // 5))
    return out[:size].strip()


def _many_shapes_pptx(count: int) -> bytes:
    base = pptx_bytes(lambda p: p.slides.add_slide(p.slide_layouts[6]))
    ns = (
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
    )
    shapes = "".join(
        f'<p:sp><p:nvSpPr><p:cNvPr id="{i + 2}" name="t{i}"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="{(i % 10) * 800000}" y="{(i // 10) * 300000}"/><a:ext cx="700000" cy="10000"/></a:xfrm>'
        f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>'
        f'<p:txBody><a:bodyPr/><a:p><a:r><a:t>shape {i}</a:t></a:r></a:p></p:txBody></p:sp>'
        for i in range(count)
    )
    slide = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><p:sld {ns}><p:cSld><p:spTree>'
        '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
        f"{shapes}</p:spTree></p:cSld></p:sld>"
    ).encode()
    return rewrite_zip_member(base, "ppt/slides/slide1.xml", lambda _old: slide)


@pytest.mark.machine_dependent
async def test_a_pptx_with_5000_shapes(store_root: Path) -> None:
    bound = generous(await baseline_seconds(store_root), factor=120.0)
    started = time.monotonic()
    doc = await parse(store_root, _many_shapes_pptx(5000), "pptx")
    assert doc.stats.blocks == 5000
    assert [b.text for b in doc.pages[0].blocks[:3]] == ["shape 0", "shape 1", "shape 2"]  # reading order kept
    assert time.monotonic() - started < bound


BILLION_LAUGHS = (
    b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "lol">'
    + b"".join(b'<!ENTITY %s "%s">' % (chr(98 + i).encode(), (b"&" + chr(97 + i).encode() + b";") * 10) for i in range(9))
    + b']><r>&j;</r>'
)


@pytest.mark.parametrize("part", ["word/document.xml", "docProps/core.xml"])
async def test_xml_entity_fixtures_in_a_docx(store_root: Path, part: str, tmp_path: Path) -> None:
    canary = tmp_path / "canary.txt"
    canary.write_text("CANARY-55")
    xxe = f'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY f SYSTEM "{canary.as_uri()}">]><r>&f;</r>'.encode()
    base = docx_bytes(lambda d: d.add_paragraph("Visible text"))
    for payload in (xxe, BILLION_LAUGHS):
        data = rewrite_zip_member(base, part, lambda _old, p=payload: p)
        assert await code_of(store_root, data, "docx") == "xml_entity_expansion_forbidden"


async def test_xml_entity_fixture_in_a_pptx(store_root: Path) -> None:
    base = pptx_bytes(lambda p: p.slides.add_slide(p.slide_layouts[6]))
    data = rewrite_zip_member(base, "ppt/slides/slide1.xml", lambda _old: BILLION_LAUGHS)
    assert await code_of(store_root, data, "pptx") == "xml_entity_expansion_forbidden"


@pytest.mark.parametrize(
    ("kind", "data", "code"),
    [
        ("pdf", make_text_pdf([TEXT] * 3)[:300], "corrupt_pdf"),
        ("docx", docx_bytes(lambda d: d.add_paragraph("text"))[:2000], "corrupt_archive"),
        ("pptx", pptx_bytes(lambda p: p.slides.add_slide(p.slide_layouts[6]))[:2000], "corrupt_archive"),
    ],
    ids=["pdf", "docx", "pptx"],
)
async def test_a_truncated_file(store_root: Path, kind: str, data: bytes, code: str) -> None:
    assert await code_of(store_root, data, kind) == code


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("pdf", docx_bytes(lambda d: d.add_paragraph("a docx stored as pdf"))),
        ("docx", make_text_pdf([TEXT])),
        ("pptx", docx_bytes(lambda d: d.add_paragraph("a docx stored as pptx"))),
        ("txt", make_text_pdf([TEXT])),
        ("md", docx_bytes(lambda d: d.add_paragraph("zip as md"))),
    ],
)
async def test_a_file_with_the_wrong_extension(store_root: Path, kind: str, data: bytes) -> None:
    assert await code_of(store_root, data, kind) == "file_type_mismatch"


async def test_hindi_and_marathi_through_every_format(store_root: Path) -> None:
    hindi = "भारत का संविधान लोकतंत्र की नींव है।"
    marathi = "महाराष्ट्र राज्य शिक्षण मंडळ अभ्यासक्रम॥"
    files = {
        "pdf": make_text_pdf([hindi, marathi], unicode_text=True),
        "docx": docx_bytes(lambda d: (d.add_paragraph(hindi), d.add_paragraph(marathi))),
        "pptx": pptx_bytes(lambda p: _slide_with(p, [hindi, marathi])),
        "txt": f"{hindi}\n\n{marathi}\n".encode(),
        "md": f"# {hindi}\n\n{marathi}\n".encode(),
    }
    for kind, data in files.items():
        doc = await parse(store_root, data, kind)
        texts = [b.text for p in doc.pages for b in p.blocks]
        assert texts == [hindi, marathi], kind
        assert doc.stats.script_guess == "deva", kind


def _slide_with(p: Any, lines: list[str]) -> None:
    from pptx.util import Inches

    slide = p.slides.add_slide(p.slide_layouts[6])
    for i, line in enumerate(lines):
        slide.shapes.add_textbox(Inches(1), Inches(1 + i), Inches(6), Inches(1)).text_frame.text = line


# ── An unexpected error in the child: permanent, one attempt (review of 5b-3, finding 1) ─────────────


def _outline_docx(value: str) -> bytes:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def build(d: Any) -> None:
        p = d.add_paragraph("A paragraph with a hostile outline level")
        el = OxmlElement("w:outlineLvl")
        el.set(qn("w:val"), value)
        p._p.get_or_add_pPr().append(el)

    return docx_bytes(build)


async def test_an_exception_in_the_real_child_is_answered_as_parse_failed_without_its_text() -> None:
    with pytest.raises(errors.PermanentIngestError) as exc:
        await stages.parse_in_sandbox(
            b"Unit 1", file_kind="txt", limits=default_limits(), parse=PARSE,
            child_module="tests._parse_children", child_args=("raise_value_error",),
        )
    assert exc.value.code == "parse_failed"
    assert "CANARY" not in repr(exc.value) and "secret" not in repr(exc.value)


async def test_a_hostile_file_fails_after_one_attempt_through_the_real_worker(
    worker_dbs: Any, academic_world: Any, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None, ingest_slots: Any,
) -> None:
    from dataclasses import replace as dc_replace

    from tests.fixtures.worker import (
        FakeClock,
        FakePipeline,
        job_row,
        make_context,
        queued_version,
        version_row,
    )

    made = await queued_version(raw_db, academic_world.a, tmp_path, subject="Math", data=_outline_docx("²"))
    clock = FakeClock(datetime.now(UTC))
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=CONFIG, clock=clock)
    real = stages.make_parse_stage(ctx.store, PARSE)

    async def parse_as_docx(sctx: stages.StageContext) -> None:
        sctx.version = dc_replace(sctx.version, file_kind="docx")  # the fixture stores every version as txt
        await real(sctx)

    st = dc_replace(FakePipeline().stages(), validate=_no_op, parse=parse_as_docx)
    assert await ingest.run_one(ctx, st) == ingest.JobOutcome.FAILED
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code) == ("failed", "parse_failed")
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"], row["last_error_code"]) == ("failed", 1, "parse_failed")
    clock.advance(86_400)
    assert await ctx.queue.claim("again") is None  # no retry, however long it waits


@pytest.mark.parametrize("mode", ["die", "silent"], ids=["killed-by-a-signal", "no-answer"])
async def test_a_child_that_dies_stays_transient_but_has_a_smaller_attempt_budget(
    mode: str, worker_dbs: Any, academic_world: Any, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None, ingest_slots: Any,
) -> None:
    """A genuine crash (a signal, SIGXCPU, an OOM kill, no answer at all) may be a one-off, so it is retried;
    but it counts against INGEST_MAX_CRASH_ATTEMPTS (2), not the 3 attempts of an outage, so a file that
    kills its parser cannot loop. The user can still retry by hand (rate limited)."""
    from dataclasses import replace as dc_replace

    from tests.fixtures.worker import (
        FakeClock,
        FakePipeline,
        job_row,
        make_context,
        queued_version,
        version_row,
    )

    made = await queued_version(raw_db, academic_world.a, tmp_path)
    clock = FakeClock(datetime.now(UTC))
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=CONFIG, clock=clock)
    parse_stage = stages.make_parse_stage(ctx.store, PARSE, child_module="tests._parse_children", child_args=(mode,))
    st = dc_replace(FakePipeline().stages(), validate=_no_op, parse=parse_stage)

    assert await ingest.run_one(ctx, st) == ingest.JobOutcome.RETRY
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"], row["last_error_code"]) == ("queued", 1, "worker_crash")
    assert (await version_row(raw_db, made.version.id)).status == "queued"

    clock.advance(1000)
    assert await ingest.run_one(ctx, st) == ingest.JobOutcome.FAILED  # the second crash exhausts the budget
    v = await version_row(raw_db, made.version.id)
    assert (v.status, v.error_code) == ("failed", "processing_failed")
    row = await job_row(raw_db, made.job.id)
    assert (row["status"], row["attempts"]) == ("failed", 2)


# ── Damaged PDFs: always an answer, never a crash (a crash would be a transient worker_crash) ────────


def _damage(pdf: bytes, how: str) -> bytes:
    if how == "xref_offset":  # the startxref offset points into the middle of nowhere
        i = pdf.rindex(b"startxref")
        return pdf[: i + 10] + b"7" + pdf[i + 11 :]
    if how == "xref_table":  # the cross-reference table is overwritten with garbage
        i = pdf.index(b"xref\n")
        return pdf[: i + 5] + b"\xde\xad\xbe\xef" * 40 + pdf[i + 5 + 160 :]
    if how == "mid_stream":  # cut in the middle of a content stream
        return pdf[: int(len(pdf) * 0.55)]
    if how == "tail_garbage":
        return pdf + b"\x00\xff" * 5000
    raise AssertionError(how)


@pytest.mark.parametrize("how", ["xref_offset", "xref_table", "mid_stream", "tail_garbage"])
async def test_a_damaged_pdf_is_answered_and_never_crashes_the_child(store_root: Path, how: str) -> None:
    pdf = _damage(make_text_pdf([TEXT] * 6), how)
    try:
        outcome: Any = await parse(store_root, pdf, "pdf")
    except errors.PermanentIngestError as exc:
        outcome = exc
    except errors.TransientIngestError as exc:  # a crash of the child
        raise AssertionError(f"{how}: the child crashed ({exc.code})") from None
    if isinstance(outcome, errors.PermanentIngestError):
        assert outcome.code in ("corrupt_pdf", "scanned_pdf", "no_text_found", "parse_failed", "pdf_too_complex"), outcome.code
    else:
        assert 1 <= outcome.page_count <= 6  # PDFium repaired it: whatever pages it found, within the cap


# ── Linux resource limits (CI; skipped on Windows) ───────────────────────────

LINUX = sys.platform.startswith("linux")


@pytest.mark.skipif(not LINUX, reason="RLIMIT_AS and RLIMIT_CPU exist on Linux only (the production worker)")
@pytest.mark.parametrize("kind", ["pdf", "docx", "pptx", "txt"])
async def test_extraction_under_the_linux_resource_limits(store_root: Path, kind: str, capsys: pytest.CaptureFixture[str]) -> None:
    limited = stages.ParseSettings(timeout_seconds=60.0, max_output_bytes=20 * 1024 * 1024, memory_limit_mb=768, cpu_seconds=60)
    files = {
        "pdf": make_text_pdf([TEXT * 20] * 150),
        "docx": docx_bytes(lambda d: [d.add_paragraph(_random_words(400)) for _ in range(3000)]),
        "pptx": _many_shapes_pptx(2000),
        "txt": ("\n\n".join([TEXT * 10] * 2500)).encode(),
    }
    data = files[kind]
    parsed, peak = await _parse_with_peak(data, kind, limited)
    assert parsed.stats.chars > 0
    assert peak is not None and peak < 768 * 1024, f"peak {peak} KiB"
    with capsys.disabled():
        print(f"\n[linux-limits] {kind}: {len(data)} bytes, {parsed.stats.chars} chars, peak {peak // 1024} MiB of 768")


async def _parse_with_peak(data: bytes, kind: str, settings: stages.ParseSettings) -> tuple[ParsedDocument, int | None]:
    outcome = await stages.run_parse_child(
        data, file_kind=kind, limits=default_limits(), parse=settings
    )
    return outcome.document, outcome.peak_memory_kb


@pytest.mark.skipif(not LINUX, reason="RLIMIT_AS exists on Linux only")
async def test_a_memory_limit_below_what_the_parser_needs_is_never_a_success(store_root: Path) -> None:
    """Measured in 5b-3 (python:3.12, x86_64): the parser needs about 50 MiB of address space; at 32 MiB
    the child hits MemoryError. Since the review of 5b-3 a MemoryError the child can still ANSWER is a
    permanent parse_failed (the file needs more memory than the limit: a retry cannot help); one that kills
    the child before it can answer (during start-up) is still a transient worker_crash with the smaller
    attempt budget. Either way, never a half-parsed success. (Not run on Windows: RLIMIT_AS is Linux only.)"""
    tiny = stages.ParseSettings(timeout_seconds=60.0, max_output_bytes=20 * 1024 * 1024, memory_limit_mb=32, cpu_seconds=60)
    with pytest.raises(errors.IngestError) as exc:
        await stages.parse_in_sandbox(make_text_pdf([TEXT] * 50), file_kind="pdf", limits=default_limits(), parse=tiny)
    assert (exc.value.permanent, exc.value.code) in ((True, "parse_failed"), (False, "worker_crash"))


# ── Timing per format (generous bounds; the numbers are printed) ─────────────


def _timing_files() -> dict[str, bytes]:
    paragraph = "Linear equations in one variable and their applications in daily life. " * 4
    return {
        "pdf": make_text_pdf([paragraph[:1500]] * 100),
        "docx": docx_bytes(lambda d: [d.add_paragraph(paragraph) for _ in range(500)]),
        "pptx": pptx_bytes(lambda p: [_slide_with(p, [f"Slide {i}", paragraph]) for i in range(100)]),
        "txt": ("\n\n".join([paragraph] * 3000)).encode(),
        "md": ("\n\n".join(f"## Unit {i}\n{paragraph}" for i in range(1500))).encode(),
    }


@pytest.mark.machine_dependent
@pytest.mark.parametrize("kind", ["pdf", "docx", "pptx", "txt", "md"])
async def test_parse_time_per_format(store_root: Path, kind: str, capsys: pytest.CaptureFixture[str]) -> None:
    data = _timing_files()[kind]
    bound = generous(await baseline_seconds(store_root))
    started = time.monotonic()
    doc = await parse(store_root, data, kind)
    seconds = time.monotonic() - started
    assert doc.stats.chars > 20_000
    assert seconds < bound, f"{kind} took {seconds:.1f} s (bound {bound:.0f} s)"
    with capsys.disabled():
        print(f"\n[timing] {kind}: {len(data) // 1024} KiB, {doc.page_count} pages, {doc.stats.chars} chars, {seconds:.2f} s")


# ── The pages quota hook (backlog B39; wired in 5c) ──────────────────────────


def test_check_pages_quota_boundaries() -> None:
    limits = replace(default_limits(), max_pages=100)
    stages.check_pages_quota(limits, used_pages=60, new_pages=40)  # exactly at the limit: allowed
    with pytest.raises(errors.PermanentIngestError) as exc:
        stages.check_pages_quota(limits, used_pages=60, new_pages=41)
    assert exc.value.code == "quota_pages_exceeded"
    stages.check_pages_quota(limits, used_pages=0, new_pages=100)


async def test_the_quota_hook_is_called_with_the_parsed_document(store_root: Path) -> None:
    seen: list[int] = []

    async def hook(ctx: stages.StageContext, parsed: ParsedDocument) -> None:
        seen.append(parsed.page_count)
        raise errors.PermanentIngestError("quota_pages_exceeded")

    assert await code_of(store_root, make_text_pdf([TEXT] * 4), "pdf", pages_quota_hook=hook) == "quota_pages_exceeded"
    assert seen == [4]


# ── End to end through the real worker and the test database ─────────────────


async def test_the_real_parse_stage_in_the_worker(
    worker_dbs: Any, academic_world: Any, raw_db: AsyncSession, tmp_path: Path, quiet_queue: None, ingest_slots: Any,
) -> None:
    from tests.fixtures.worker import (
        FakePipeline,
        job_row,
        make_context,
        queued_version,
        version_row,
    )

    w = academic_world.a
    ctx = make_context(worker_dbs, tmp_path, ingest_slots, config=CONFIG)
    seen: dict[str, Any] = {}
    parse_stage = stages.make_parse_stage(ctx.store, PARSE)

    async def parse_and_keep(sctx: stages.StageContext) -> None:
        await parse_stage(sctx)
        seen["parsed"] = sctx.results["parsed"]

    pipeline = FakePipeline()
    st = replace(pipeline.stages(), validate=stages.make_validate_stage(ctx.store, PARSE), parse=parse_and_keep,
                 chunk=_no_op, embed=_no_op)

    good = await queued_version(raw_db, w, tmp_path, subject="Math", data="Unit 1: Algebra\n\nलघुगणक।\n".encode())
    assert await ingest.run_one(ctx, st) == ingest.JobOutcome.DONE
    assert [b.text for b in seen["parsed"].pages[0].blocks] == ["Unit 1: Algebra", "लघुगणक।"]
    assert (await job_row(raw_db, good.job.id))["status"] == "done"

    empty = await queued_version(raw_db, w, tmp_path, subject="Physics", data=b"   \n\n   \n")
    assert await ingest.run_one(ctx, st) == ingest.JobOutcome.FAILED
    v = await version_row(raw_db, empty.version.id)
    assert (v.status, v.error_code) == ("failed", "no_text_found")


async def _no_op(_ctx: stages.StageContext) -> None:
    return None


async def test_pages_used_by_others_counts_active_versions_of_live_documents(
    worker_dbs: Any, academic_world: Any, raw_db: AsyncSession, tmp_path: Path,
) -> None:
    from sqlalchemy import update

    from app.db.models.document import DocumentVersion
    from app.workers.db import TenantRef
    from tests.fixtures.worker import queued_version

    w = academic_world.a
    async with worker_dbs.tenant_sessions.for_job(TenantRef(w.id)) as db:
        before = await stages.pages_used_by_others(db, w.id, uuid.uuid4())
    mine = await queued_version(raw_db, w, tmp_path, subject="Math")
    other = await queued_version(raw_db, w, tmp_path, subject="Physics")
    await raw_db.execute(update(DocumentVersion).where(DocumentVersion.id == mine.version.id).values(page_count=7))
    await raw_db.execute(update(DocumentVersion).where(DocumentVersion.id == other.version.id).values(page_count=11))
    await raw_db.commit()
    async with worker_dbs.tenant_sessions.for_job(TenantRef(w.id)) as db:
        used = await stages.pages_used_by_others(db, w.id, mine.version.id)
    assert used == before + 11  # its own version never counts against itself
    limits = replace(default_limits(), max_pages=used + 5)
    stages.check_pages_quota(limits, used_pages=used, new_pages=5)
    with pytest.raises(errors.PermanentIngestError):
        stages.check_pages_quota(limits, used_pages=used, new_pages=6)
