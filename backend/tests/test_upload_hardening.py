"""
Regression tests for the upload-validation findings 4-8:
4 body-size limit before parsing, 5 content-based type, 6 OpenXML hardening,
7 no internal details in error responses, 8 best-effort PDF scan.
"""

import io
import uuid
import zipfile
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import create_access_token
from app.core.upload_security import UploadSecurityError, sniff_and_validate_file_type
from app.db.models.academic import Class
from app.db.models.tenant import Tenant
from app.db.models.user import User
from app.services import timetable_parser_runner as tpr

CT = "[Content_Types].xml"
CT_XML = b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>'
EMPTY_XML = b"<root/>"


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def good_xlsx(extra: dict[str, bytes] | None = None) -> bytes:
    files = {CT: CT_XML, "xl/workbook.xml": EMPTY_XML}
    files.update(extra or {})
    return make_zip(files)


def good_docx(extra: dict[str, bytes] | None = None) -> bytes:
    files = {CT: CT_XML, "word/document.xml": EMPTY_XML}
    files.update(extra or {})
    return make_zip(files)


def good_pptx(extra: dict[str, bytes] | None = None) -> bytes:
    files = {CT: CT_XML, "ppt/presentation.xml": EMPTY_XML}
    files.update(extra or {})
    return make_zip(files)


def code_of(exc: pytest.ExceptionInfo[UploadSecurityError]) -> str:
    assert isinstance(exc.value.detail, dict)
    return str(exc.value.detail["code"])


# ─── Finding 5: type decided by content ──────────────────────────────────────

def test_content_decides_type_for_valid_files() -> None:
    assert sniff_and_validate_file_type(good_xlsx(), "a.xlsx") == "xlsx"
    assert sniff_and_validate_file_type(good_docx(), "a.docx") == "docx"
    assert sniff_and_validate_file_type(good_pptx(), "a.pptx") == "pptx"


def test_zip_without_office_parts_named_xlsx_is_rejected() -> None:
    data = make_zip({CT: CT_XML, "readme.txt": b"hello"})
    for name in ("fake.xlsx", "fake.docx"):
        with pytest.raises(UploadSecurityError) as exc:
            sniff_and_validate_file_type(data, name)
        assert code_of(exc) == "invalid_openxml_format"


def test_extension_content_mismatch_is_rejected() -> None:
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(good_docx(), "renamed.xlsx")
    assert code_of(exc) == "file_type_mismatch"


def test_xl_prefix_without_workbook_is_rejected() -> None:
    data = make_zip({CT: CT_XML, "xl/styles.xml": EMPTY_XML})
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(data, "x.xlsx")
    assert code_of(exc) == "invalid_openxml_format"


# ─── Finding 6: macro / embedded content / fail closed ───────────────────────

@pytest.mark.parametrize(
    "part",
    ["ppt/vbaProject.bin", "XL/VBAPROJECT.BIN", "deep/er/vbaproject.bin", "xl/sub/VbaProject.Bin"],
)
def test_vba_project_anywhere_rejected(part: str) -> None:
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(good_xlsx({part: b"x"}), "a.xlsx")
    assert code_of(exc) == "macro_enabled_file"


@pytest.mark.parametrize("part", ["xl/embeddings/oleObject1.xlsx", "word/Embeddings/a.dat", "ppt/embeddings/x.bin"])
def test_embeddings_rejected(part: str) -> None:
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(good_xlsx({part: b"x"}), "a.xlsx")
    assert code_of(exc) == "embedded_content"


def test_nested_zip_rejected() -> None:
    inner = make_zip({"a.txt": b"hi"})
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(good_xlsx({"xl/media/innocent.png": inner}), "a.xlsx")
    assert code_of(exc) == "embedded_content"


def test_ole_magic_inside_archive_rejected() -> None:
    ole = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 50
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(good_docx({"word/data.bin": ole}), "a.docx")
    assert code_of(exc) == "embedded_content"


def test_external_relationship_rejected() -> None:
    rels = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="r1" Type="x" Target="http://evil.example/a" TargetMode="External"/>'
        b"</Relationships>"
    )
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(good_xlsx({"xl/_rels/workbook.xml.rels": rels}), "a.xlsx")
    assert code_of(exc) == "external_relationship"


def test_malformed_content_types_fails_closed() -> None:
    data = make_zip({CT: b"<Types><unclosed>", "xl/workbook.xml": EMPTY_XML})
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(data, "a.xlsx")
    assert code_of(exc) == "invalid_xml"


def test_missing_content_types_fails_closed() -> None:
    data = make_zip({"xl/workbook.xml": EMPTY_XML})
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(data, "a.xlsx")
    assert code_of(exc) == "invalid_openxml_format"


def test_malformed_workbook_and_rels_fail_closed() -> None:
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(make_zip({CT: CT_XML, "xl/workbook.xml": b"<a><b>"}), "a.xlsx")
    assert code_of(exc) == "invalid_xml"
    with pytest.raises(UploadSecurityError) as exc2:
        sniff_and_validate_file_type(good_xlsx({"_rels/.rels": b"not xml"}), "a.xlsx")
    assert code_of(exc2) == "invalid_xml"


def test_pptx_rules() -> None:
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(good_pptx({"ppt/vbaProject.bin": b"x"}), "a.pptx")
    assert code_of(exc) == "macro_enabled_file"
    with pytest.raises(UploadSecurityError) as exc2:
        sniff_and_validate_file_type(good_pptx({"ppt/embeddings/oleObject1.bin": b"x"}), "a.pptx")
    assert code_of(exc2) == "embedded_content"
    macro_ct = (
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Override PartName="/ppt/presentation.xml" '
        b'ContentType="application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml"/></Types>'
    )
    with pytest.raises(UploadSecurityError) as exc3:
        sniff_and_validate_file_type(make_zip({CT: macro_ct, "ppt/presentation.xml": EMPTY_XML}), "a.pptx")
    assert code_of(exc3) == "macro_enabled_file"


# ─── Finding 8: PDF best-effort scan ─────────────────────────────────────────

def make_pdf(extra_obj: bytes = b"", pages: int = 1) -> bytes:
    body = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R " + extra_obj + b" >>\nendobj\n"
    for i in range(pages):
        body += b"%d 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n" % (10 + i)
    return body + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


def test_clean_pdf_passes() -> None:
    assert sniff_and_validate_file_type(make_pdf(), "a.pdf") == "pdf"


@pytest.mark.parametrize(
    "token",
    [b"/JavaScript", b"/JS (x)", b"/Launch", b"/EmbeddedFile", b"/OpenAction 3 0 R", b"/AA << >>", b"/J#61vaScript"],
)
def test_pdf_active_content_rejected(token: bytes) -> None:
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(make_pdf(token), "a.pdf")
    assert code_of(exc) == "pdf_active_content"


def test_pdf_similar_names_not_flagged() -> None:
    assert sniff_and_validate_file_type(make_pdf(b"/JSON 1 /AAA 2"), "a.pdf") == "pdf"


def test_pdf_page_and_object_caps() -> None:
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(make_pdf(pages=150), "a.pdf")
    assert code_of(exc) == "pdf_too_complex"
    many = b"%PDF-1.4\n" + b"".join(b"%d 0 obj\n<< >>\nendobj\n" % i for i in range(1, 25000))
    with pytest.raises(UploadSecurityError) as exc2:
        sniff_and_validate_file_type(many, "a.pdf")
    assert code_of(exc2) == "pdf_too_complex"


# ─── Finding 7: no internals in client errors ────────────────────────────────

LEAK_MARKERS = ("Traceback", "site-packages", "openpyxl", "pdfplumber", "pdfminer", "\\Users\\", "/usr/", "File \"", "boom")


async def test_child_failure_message_not_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner must ignore the child's free-text message and use a fixed one."""

    async def fake_blocking(*_a: Any, **_k: Any) -> bytes:
        return b'{"success": false, "error_code": "parse_failure", "error_msg": "C:\\\\secret\\\\site-packages\\\\x.py boom"}'

    monkeypatch.setattr(tpr.asyncio, "to_thread", fake_blocking)
    with pytest.raises(UploadSecurityError) as exc:
        await tpr.run_killable_parse(b"x", "csv", uuid.uuid4())
    detail = exc.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "parse_failed"
    assert not any(m in str(detail) for m in LEAK_MARKERS)


def test_corrupt_pdf_message_is_fixed() -> None:
    from app.services.timetable_importer import parse_raw_pdf

    with pytest.raises(UploadSecurityError) as exc:
        parse_raw_pdf(b"%PDF-1.4 garbage that is not a pdf")
    assert isinstance(exc.value.detail, dict)
    assert exc.value.detail["code"] == "corrupt_pdf"
    assert not any(m in str(exc.value.detail) for m in ("pdfminer", "pdfplumber", "Traceback", "EOF"))


def test_zip_member_names_not_echoed() -> None:
    from app.core.upload_security import validate_zip_archive

    big = zipfile.ZipInfo("secret_dir/secret_name.xml")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(big, b"A" * (6 * 1024 * 1024))
    with pytest.raises(UploadSecurityError) as exc:
        validate_zip_archive(buf.getvalue())
    assert "secret_name" not in str(exc.value.detail)


# ─── API level: findings 4 and 7 ─────────────────────────────────────────────

@pytest.fixture
async def import_ctx(tenant_a: Tenant, admin_a: User, raw_db: AsyncSession) -> AsyncIterator[tuple[str, dict[str, str]]]:
    c = Class(tenant_id=tenant_a.id, name="Grade 9")
    raw_db.add(c)
    await raw_db.commit()
    token = create_access_token(
        tenant_id=str(tenant_a.id), user_id=str(admin_a.id), is_admin=True, permissions=["timetable:manage"]
    )
    yield f"/api/v1/classes/{c.id}/timetables/import-preview", {
        "Authorization": f"Bearer {token}",
        "X-Tenant-Slug": tenant_a.slug,
    }


async def test_oversized_content_length_rejected_413(client: AsyncClient, import_ctx: tuple[str, dict[str, str]]) -> None:
    url, headers = import_ctx
    big = b"a,b\n" + b"x" * (3 * 1024 * 1024)
    res = await client.post(url, headers=headers, files={"file": ("t.csv", big, "text/csv")})
    assert res.status_code == 413
    assert res.json()["detail"]["code"] == "file_too_large"


async def test_chunked_body_without_content_length_rejected_413(
    client: AsyncClient, import_ctx: tuple[str, dict[str, str]]
) -> None:
    url, headers = import_ctx
    boundary = "bnd"

    async def body() -> AsyncIterator[bytes]:
        yield f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="t.csv"\r\n\r\n'.encode()
        for _ in range(48):
            yield b"x" * (64 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    h = {**headers, "Content-Type": f"multipart/form-data; boundary={boundary}"}
    res = await client.post(url, headers=h, content=body())
    assert res.status_code == 413


async def test_too_many_multipart_parts_rejected(client: AsyncClient, import_ctx: tuple[str, dict[str, str]]) -> None:
    url, headers = import_ctx
    files = [("file", ("t.csv", b"a,b\n1,2\n", "text/csv"))] + [(f"f{i}", (None, "v")) for i in range(40)]
    res = await client.post(url, headers=headers, files=files)
    assert res.status_code == 413
    assert res.json()["detail"]["code"] == "too_many_parts"


async def test_template_endpoint_rejects_large_content_length(
    client: AsyncClient, import_ctx: tuple[str, dict[str, str]]
) -> None:
    url, headers = import_ctx
    url = url.replace("import-preview", "template.csv")
    res = await client.request("GET", url, headers={**headers, "Content-Length": str(50 * 1024 * 1024)})
    assert res.status_code == 413


async def test_api_error_for_bad_pdf_has_no_internals(client: AsyncClient, import_ctx: tuple[str, dict[str, str]]) -> None:
    url, headers = import_ctx
    res = await client.post(url, headers=headers, files={"file": ("t.pdf", b"%PDF-1.4 not really a pdf", "application/pdf")})
    assert res.status_code == 422
    assert not any(m in res.text for m in LEAK_MARKERS)
    assert "code" in res.json()["detail"]


async def test_middleware_stops_reading_body_at_limit() -> None:
    """The downstream app must see at most limit+one chunk, never the whole body."""
    from app.core.body_limit import MAX_REQUEST_BODY_BYTES, BodyLimitMiddleware

    seen = 0

    async def app(scope: Any, receive: Any, send: Any) -> None:
        nonlocal seen
        while True:
            msg = await receive()
            seen += len(msg.get("body", b""))
            if not msg.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    chunk = b"x" * (256 * 1024)
    chunks_left = 40  # 10 MB, no Content-Length
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal chunks_left
        chunks_left -= 1
        return {"type": "http.request", "body": chunk, "more_body": chunks_left > 0}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/anything",
        "headers": [(b"content-type", b"multipart/form-data; boundary=zz")],
    }
    await BodyLimitMiddleware(app)(scope, receive, send)
    assert sent[0]["status"] == 413
    assert seen <= MAX_REQUEST_BODY_BYTES
    assert chunks_left > 20  # most of the 10 MB was never pulled


# ─── Batch C item 1: only hyperlink relationships may be external ────────────

REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
OD = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rels(rel_type: str, target: str = "http://example.org/x") -> bytes:
    return (
        f'<Relationships xmlns="{REL_NS}"><Relationship Id="r1" Type="{OD}/{rel_type}" '
        f'Target="{target}" TargetMode="External"/></Relationships>'
    ).encode()


def test_docx_with_ordinary_hyperlink_passes() -> None:
    data = good_docx({"word/_rels/document.xml.rels": _rels("hyperlink")})
    assert sniff_and_validate_file_type(data, "a.docx") == "docx"


@pytest.mark.parametrize(
    "rel_type",
    ["attachedTemplate", "oleObject", "externalLink", "frame", "image", "video", "audio", "subDocument"],
)
def test_other_external_relationship_types_rejected(rel_type: str) -> None:
    data = good_docx({"word/_rels/document.xml.rels": _rels(rel_type, "file:///x/y")})
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(data, "a.docx")
    assert code_of(exc) == "external_relationship"


def test_external_rel_with_hyperlink_lookalike_type_rejected() -> None:
    rels = (
        f'<Relationships xmlns="{REL_NS}"><Relationship Id="r1" Type="{OD}/hyperlinkX" '
        f'Target="http://e" TargetMode="External"/></Relationships>'
    ).encode()
    with pytest.raises(UploadSecurityError) as exc:
        sniff_and_validate_file_type(good_docx({"word/_rels/document.xml.rels": rels}), "a.docx")
    assert code_of(exc) == "external_relationship"
