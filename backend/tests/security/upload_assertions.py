"""
Security assertions for uploads (step 5a-3), written as helpers.

Each helper is used by the REAL test of its protection and by the prove-it-fails meta-test in
`test_prove_it_fails_uploads.py`, which switches the protection off and expects the helper to raise.
A helper must therefore fail with AssertionError when the protection is missing.
"""

import io
import uuid
import zipfile
from collections.abc import AsyncIterator
from typing import Any

from httpx import AsyncClient

from app.core.body_limit import FRAMING_OVERHEAD_BYTES, MAX_REQUEST_BODY_BYTES
from app.core.upload_security import (
    DOC_UPLOAD_CEILING_BYTES,
    UploadProfile,
    UploadSecurityError,
    validate_upload,
)
from app.services import document_store as ds

MB = 1024 * 1024

# ─── Builders ────────────────────────────────────────────────────────────────

CT = "[Content_Types].xml"
CT_XML = b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"></Types>'
EMPTY_XML = b"<root/>"


def make_zip(files: dict[str, bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def good_docx(extra: dict[str, bytes] | None = None) -> bytes:
    return make_zip({CT: CT_XML, "word/document.xml": EMPTY_XML, **(extra or {})})


def good_pptx(extra: dict[str, bytes] | None = None) -> bytes:
    return make_zip({CT: CT_XML, "ppt/presentation.xml": EMPTY_XML, **(extra or {})})


def make_pdf(extra_obj: bytes = b"", pages: int = 1) -> bytes:
    body = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R " + extra_obj + b" >>\nendobj\n"
    for i in range(pages):
        body += b"%d 0 obj\n<< /Type /Page /Parent 2 0 R >>\nendobj\n" % (10 + i)
    return body + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


def code_of(exc: UploadSecurityError) -> str:
    assert isinstance(exc.detail, dict)
    return str(exc.detail["code"])


# ─── Hostile PDFs (both profiles share one rule) ─────────────────────────────

FORBIDDEN_PDF_TOKENS = [
    b"/JavaScript",
    b"/JS (x)",
    b"/Launch",
    b"/EmbeddedFile",
    b"/RichMedia",
    b"/XFA",
    b"/J#61vaScript",  # #-escaped variants
    b"/R#69chMedia",
    b"/XF#41",
    b"/E#6dbeddedFile",
]


def assert_hostile_pdfs_rejected() -> None:
    """Every forbidden name, also #-escaped, is rejected with pdf_active_content by BOTH profiles."""
    for profile in UploadProfile:
        for token in FORBIDDEN_PDF_TOKENS:
            try:
                validate_upload(make_pdf(token), "a.pdf", profile)
            except UploadSecurityError as exc:
                assert code_of(exc) == "pdf_active_content", f"{profile} {token!r}: wrong code {code_of(exc)}"
            else:
                raise AssertionError(f"{profile.value}: hostile PDF {token!r} was accepted")


# ─── Body limits ─────────────────────────────────────────────────────────────

MULTIPART = {"Content-Type": "multipart/form-data; boundary=zz"}


async def _chunks(total: int, chunk: int, counter: list[int]) -> AsyncIterator[bytes]:
    sent = 0
    while sent < total:
        n = min(chunk, total - sent)
        counter[0] += 1
        sent += n
        yield b"x" * n


async def assert_documents_limits_enforced(client: AsyncClient, url: str) -> None:
    """
    The documents prefix: 20 MB is accepted; a body of ceiling + 64 KB is accepted and one byte
    more is rejected with 413; a 7th multipart part is rejected; a request without Content-Length
    is counted while it streams and the server stops reading near the limit.
    """
    limit = DOC_UPLOAD_CEILING_BYTES + FRAMING_OVERHEAD_BYTES

    ok = await client.post(url, headers=MULTIPART, content=b"x" * (20 * MB))
    assert ok.status_code == 200, f"20 MB body to the documents prefix was refused ({ok.status_code})"

    edge = await client.post(url, headers=MULTIPART, content=b"x" * limit)
    assert edge.status_code == 200, f"a body of exactly the limit was refused ({edge.status_code})"

    over = await client.post(url, headers=MULTIPART, content=b"x" * (limit + 1))
    assert over.status_code == 413, "ceiling + 64 KB + 1 byte was not rejected"
    assert over.json()["detail"]["code"] == "file_too_large"

    six = await client.post(url, files={"file": ("a.bin", b"data")}, data={f"f{i}": "v" for i in range(5)})
    assert six.status_code == 200, f"6 parts were refused ({six.status_code})"
    seven = await client.post(url, files={"file": ("a.bin", b"data")}, data={f"f{i}": "v" for i in range(6)})
    assert seven.status_code == 413, "a 7th part was not rejected"
    assert seven.json()["detail"]["code"] == "too_many_parts"

    counter = [0]
    streamed = await client.post(url, headers=MULTIPART, content=_chunks(limit + 4 * MB, MB, counter))
    assert streamed.status_code == 413, "a body without Content-Length was not counted while streaming"
    assert counter[0] <= (limit // MB) + 2, "the server kept reading long after the limit"


async def assert_default_limit_enforced(client: AsyncClient, url: str) -> None:
    """Every other multipart route keeps 2 MB + 64 KB and 5 parts."""
    ok = await client.post(url, headers=MULTIPART, content=b"x" * MAX_REQUEST_BODY_BYTES)
    assert ok.status_code == 200, f"a body of exactly 2 MB + 64 KB was refused ({ok.status_code})"
    over = await client.post(url, headers=MULTIPART, content=b"x" * (MAX_REQUEST_BODY_BYTES + 1))
    assert over.status_code == 413, "2 MB + 64 KB + 1 byte was not rejected on the default prefix"
    assert over.json()["detail"]["code"] == "file_too_large"
    twenty = await client.post(url, headers=MULTIPART, content=b"x" * (20 * MB))
    assert twenty.status_code == 413, "20 MB was accepted outside the documents prefix"
    five = await client.post(url, files={"file": ("a.bin", b"d")}, data={f"f{i}": "v" for i in range(4)})
    assert five.status_code == 200, f"5 parts were refused ({five.status_code})"
    six = await client.post(url, files={"file": ("a.bin", b"d")}, data={f"f{i}": "v" for i in range(5)})
    assert six.status_code == 413, "a 6th part was accepted on the default prefix"

    counter = [0]
    streamed = await client.post(url, headers=MULTIPART, content=_chunks(6 * MB, 64 * 1024, counter))
    assert streamed.status_code == 413, "a streamed body without Content-Length was not counted"


# ─── Private document store ──────────────────────────────────────────────────

async def assert_store_enforces_tenant_prefix(store: Any, tenant_a: uuid.UUID, tenant_b: uuid.UUID) -> None:
    """
    Tenant A can use its own keys. It cannot put, get, delete or stream a well-formed key of
    tenant B, and malformed or traversal keys are rejected before any file system access.
    """
    own = ds.new_document_key(tenant_a)
    foreign = ds.new_document_key(tenant_b)
    await store.put(tenant_a, own, b"secret-a")
    await store.put(tenant_b, foreign, b"secret-b")

    async def must_refuse(coro_factory: Any, what: str) -> None:
        try:
            await coro_factory()
        except ds.InvalidStorageKey:
            return
        raise AssertionError(f"store accepted: {what}")

    await must_refuse(lambda: store.get(tenant_a, foreign), "reading another tenant's key")
    await must_refuse(lambda: store.delete(tenant_a, foreign), "deleting another tenant's key")
    await must_refuse(lambda: store.put(tenant_a, foreign, b"x"), "writing under another tenant's prefix")
    await must_refuse(lambda: store.stream(tenant_a, foreign), "streaming another tenant's key")

    random_part = own.rsplit("/", 1)[-1]
    attacks = [
        f"t/{tenant_a}/d/../../{tenant_b}/d/{foreign.rsplit('/', 1)[-1]}",
        f"t/{tenant_a}/d/../{random_part}",
        f"../t/{tenant_a}/d/{random_part}",
        f"/t/{tenant_a}/d/{random_part}",
        f"t\\{tenant_a}\\d\\{random_part}",
        f"t/{tenant_a}/d/{random_part}/",
        f"t/{tenant_a}/d/{random_part}/../{random_part}",
        f"t/{tenant_a}/x/{random_part}",
        f"t/{tenant_a}/d/{random_part[:-1]}",
        f"t/{tenant_a}/d/{random_part.upper()}",
        f"t/{tenant_a}/d/{random_part}\x00.txt",
        "C:/Windows/win.ini",
        "",
        "..",
    ]
    for bad in attacks:
        await must_refuse(lambda bad=bad: store.get(tenant_a, bad), f"key {bad!r}")
        await must_refuse(lambda bad=bad: store.put(tenant_a, bad, b"x"), f"put key {bad!r}")

    # The foreign object was never touched by any of the above
    assert await store.get(tenant_b, foreign) == b"secret-b", "tenant B's object changed or vanished"
    assert await store.get(tenant_a, own) == b"secret-a"
