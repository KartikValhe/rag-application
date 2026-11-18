"""
Security assertions for uploads (step 5a-3), written as helpers.

Each helper is used by the REAL test of its protection and by the prove-it-fails meta-test in
`test_prove_it_fails_uploads.py`, which switches the protection off and expects the helper to raise.
A helper must therefore fail with AssertionError when the protection is missing.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import uuid
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
from httpx import AsyncClient

from app.core import upload_security
from app.core.body_limit import FRAMING_OVERHEAD_BYTES, MAX_REQUEST_BODY_BYTES
from app.core.config import settings
from app.core.upload_security import (
    DOC_UPLOAD_CEILING_BYTES,
    UploadProfile,
    UploadSecurityError,
    validate_upload,
)
from app.services import document_store as ds
from tests.security.asgi_stream import Header, call_asgi, multipart_body, split

MB = 1024 * 1024
BACKEND_DIR = Path(__file__).resolve().parents[2]

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
    b"/RichMedia",
    b"/XFA",
    b"/J#61vaScript",  # #-escaped variants
    b"/R#69chMedia",
    b"/XF#41",
]
# A real attached file has its own code (real-corpus finding, 2026-11-18)
EMBEDDED_PDF_TOKENS = [b"/EmbeddedFile", b"/E#6dbeddedFile"]


def assert_hostile_pdfs_rejected() -> None:
    """Every forbidden name, also #-escaped, is rejected by BOTH profiles: active content with
    pdf_active_content, an attached file with pdf_embedded_file."""
    cases = [(t, "pdf_active_content") for t in FORBIDDEN_PDF_TOKENS] + [(t, "pdf_embedded_file") for t in EMBEDDED_PDF_TOKENS]
    for profile in UploadProfile:
        for token, expected in cases:
            try:
                validate_upload(make_pdf(token), "a.pdf", profile)
            except UploadSecurityError as exc:
                assert code_of(exc) == expected, f"{profile} {token!r}: wrong code {code_of(exc)}"
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


async def assert_documents_limits_enforced(client: AsyncClient, url: str, auth: dict[str, str]) -> None:
    """
    A document upload route (POST /api/v1/documents or .../versions, with a valid Bearer token):
    20 MB is accepted; a body of ceiling + 64 KB is accepted and one byte more is rejected with 413;
    a 7th multipart part is rejected; a lying Content-Length is caught by counting the bytes while
    they stream, and the server stops reading near the limit.
    """
    limit = DOC_UPLOAD_CEILING_BYTES + FRAMING_OVERHEAD_BYTES
    h = {**MULTIPART, **auth}

    ok = await client.post(url, headers=h, content=b"x" * (20 * MB))
    assert ok.status_code == 200, f"20 MB body to a document upload route was refused ({ok.status_code})"

    edge = await client.post(url, headers=h, content=b"x" * limit)
    assert edge.status_code == 200, f"a body of exactly the limit was refused ({edge.status_code})"

    over = await client.post(url, headers=h, content=b"x" * (limit + 1))
    assert over.status_code == 413, "ceiling + 64 KB + 1 byte was not rejected"
    assert over.json()["detail"]["code"] == "file_too_large"

    form = {"file": ("a.bin", b"data")}
    six = await client.post(url, headers=auth, files=form, data={f"f{i}": "v" for i in range(5)})
    assert six.status_code == 200, f"6 parts were refused ({six.status_code})"
    seven = await client.post(url, headers=auth, files=form, data={f"f{i}": "v" for i in range(6)})
    assert seven.status_code == 413, "a 7th part was not rejected"
    assert seven.json()["detail"]["code"] == "too_many_parts"

    counter = [0]
    lying = {**h, "Content-Length": str(MB)}
    streamed = await client.post(url, headers=lying, content=_chunks(limit + 4 * MB, MB, counter))
    assert streamed.status_code == 413, "a body longer than its Content-Length was not counted while streaming"
    assert counter[0] <= (limit // MB) + 2, "the server kept reading long after the limit"


SMALL_LIMIT_BYTES = 64 * 1024


async def assert_documents_registry_enforced(client: AsyncClient, auth: dict[str, str]) -> None:
    """
    Only POST /api/v1/documents and POST /api/v1/documents/{uuid}/versions get the document
    ceiling. Every other request under /api/v1/documents (other methods, other paths, JSON) is
    limited to 64 KB, before the route runs.
    """
    doc_id = uuid.uuid4()
    json_h = {"Content-Type": "application/json", **auth}
    others = [
        ("GET", "/api/v1/documents"),
        ("PUT", "/api/v1/documents"),
        ("PATCH", f"/api/v1/documents/{doc_id}"),
        ("POST", f"/api/v1/documents/{doc_id}/retry"),
        ("PUT", f"/api/v1/documents/{doc_id}/versions"),
        ("POST", f"/api/v1/documents/{doc_id}/versions/extra"),
        ("POST", "/api/v1/documents/search"),
        ("POST", "/api/v1/documents/not-a-uuid/versions"),
    ]
    for method, url in others:
        edge = await client.request(method, url, headers=json_h, content=b"x" * SMALL_LIMIT_BYTES)
        assert edge.status_code == 200, f"{method} {url}: a body of exactly 64 KB was refused ({edge.status_code})"
        big = await client.request(method, url, headers=json_h, content=b"x" * (3 * MB))
        assert big.status_code == 413, f"{method} {url}: 3 MB of JSON was accepted ({big.status_code})"
        multi = await client.request(method, url, headers={**MULTIPART, **auth}, content=b"x" * (SMALL_LIMIT_BYTES + 1))
        assert multi.status_code == 413, f"{method} {url}: a multipart body over 64 KB was accepted"

    up = await client.post(f"/api/v1/documents/{doc_id}/versions", headers={**MULTIPART, **auth}, content=b"x" * (3 * MB))
    assert up.status_code == 200, f"the versions upload route lost the document ceiling ({up.status_code})"


# ─── Header confusion (finding 2 of the 5a-3 review) ─────────────────────────

DUPLICATE_HEADER_BODY = {
    "detail": {"code": "duplicate_header", "message": "The request repeats a header that may appear only once."}
}


async def assert_duplicate_headers_rejected(
    app: Callable[..., Awaitable[None]], method: str, path: str, base_headers: list[Header]
) -> None:
    """A repeated Content-Type or Content-Length is refused with 400 before any body byte is read."""
    boundary = "dupbnd"
    body = multipart_body(boundary, "t.csv", b"weekday,start_time\n")
    ct = ("Content-Type", f"multipart/form-data; boundary={boundary}")
    cl = ("Content-Length", str(len(body)))
    cases = {
        "Content-Type": [ct, ("Content-Type", "multipart/form-data; boundary=other"), cl],
        "Content-Type (same value)": [ct, ct, cl],
        "Content-Length": [ct, cl, ("Content-Length", "1")],
        "Content-Length (same value)": [ct, cl, cl],
    }
    for name, extra in cases.items():
        res = await call_asgi(app, method, path, base_headers + extra, split(body, 16))
        assert res.status == 400, f"{method} {path}: repeated {name} was not refused ({res.status})"
        assert res.json() == DUPLICATE_HEADER_BODY, f"repeated {name}: wrong body {res.body!r}"
        assert res.chunks_consumed == 0, f"repeated {name}: the body was read before the refusal"


# ─── Anonymous spooling (finding 3 of the 5a-3 review) ───────────────────────

def _token(secret: str, *, minutes: int, token_type: str = "access") -> str:
    now = datetime.now(UTC)
    claims = {"sub": str(uuid.uuid4()), "tenant_id": str(uuid.uuid4()), "type": token_type,
              "iat": now, "exp": now + timedelta(minutes=minutes)}
    return jwt.encode(claims, secret, algorithm="HS256")


def bad_authorizations() -> dict[str, list[Header]]:
    """Requests that must never reach the body parser of an upload route."""
    return {
        "no Authorization header": [],
        "empty Bearer": [("Authorization", "Bearer ")],
        "not a JWT": [("Authorization", "Bearer not-a-jwt")],
        "Basic scheme": [("Authorization", "Basic dXNlcjpwYXNz")],
        "expired token": [("Authorization", f"Bearer {_token(settings.SECRET_KEY, minutes=-5)}")],
        "wrong signature": [("Authorization", f"Bearer {_token('x' * 64, minutes=5)}")],
        "platform token": [
            ("Authorization", f"Bearer {_token(settings.PLATFORM_SECRET_KEY, minutes=5, token_type='platform_access')}")
        ],
        "refresh-type token": [("Authorization", f"Bearer {_token(settings.SECRET_KEY, minutes=5, token_type='refresh')}")],
    }


@contextmanager
def record_spools() -> Generator[list[int], None, None]:
    """Count the SpooledTemporaryFile objects Starlette's form parser creates, and point the temp dir at an empty folder."""
    import starlette.formparsers as fp

    made: list[int] = []
    original = fp.SpooledTemporaryFile

    class Recording(original):  # type: ignore[valid-type,misc]
        def __init__(self, *a: Any, **k: Any) -> None:
            made.append(1)
            super().__init__(*a, **k)

    old_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory() as spool_dir:
        fp.SpooledTemporaryFile = Recording  # type: ignore[misc]
        tempfile.tempdir = spool_dir
        try:
            yield made
            leftovers = os.listdir(spool_dir)
            assert not leftovers, f"spool files appeared in the temp directory: {leftovers}"
        finally:
            fp.SpooledTemporaryFile = original  # type: ignore[misc]
            tempfile.tempdir = old_tempdir


async def assert_anonymous_upload_not_consumed(
    app: Callable[..., Awaitable[None]], path: str, tenant_headers: list[Header], *, method: str = "POST"
) -> None:
    """
    Every request without a valid tenant Bearer token gets 401 from the middleware: not one body
    chunk is consumed and Starlette never creates a spool file. The body is 1.5 MB, above
    Starlette's 1 MB in-memory spool size, so parsing it would have written a temp file.
    """
    boundary = "anonbnd"
    body = multipart_body(boundary, "t.csv", b"x" * (3 * MB // 2))
    framing = [
        ("Content-Type", f"multipart/form-data; boundary={boundary}"),
        ("Content-Length", str(len(body))),
    ]
    for name, auth in bad_authorizations().items():
        with record_spools() as spools:
            res = await call_asgi(app, method, path, tenant_headers + auth + framing, split(body))
        assert res.status == 401, f"{path} with {name}: expected 401, got {res.status}"
        assert res.json()["detail"]["code"] in ("missing_token", "invalid_token"), f"{name}: {res.body!r}"
        assert res.chunks_consumed == 0, (
            f"{path} with {name}: {res.chunks_consumed} of {res.chunks_total} body chunks were read before the 401"
        )
        assert not spools, f"{path} with {name}: Starlette spooled the form before authentication"


# ─── PDF scan cost (finding 1 of the 5a-3 review) ────────────────────────────

# Every regex the PDF scan uses. The current values are handed to the child process, so a
# monkeypatched (for example unbounded) pattern in the parent is what the child really runs.
PDF_SCAN_PATTERNS = ("PDF_OBJ_RE", "PDF_PAGE_RE", "PDF_FORBIDDEN_RE", "PDF_ACTIVE_RE", "PDF_HEX_ESCAPE_RE")
SCAN_BUDGET_SECONDS = 2.0  # generous: the linear scan of 2 MB takes well under 0.5 s
SCAN_KILL_SECONDS = 25.0  # a quadratic scan is killed here instead of running for hours


def assert_pdf_scan_fast(payload: str, profile: UploadProfile, *, kill_after: float = SCAN_KILL_SECONDS) -> str:
    """
    Validate a 2 MB hostile PDF-like payload (see pdf_scan_child.PAYLOADS) in a separate process
    and require it to finish in under SCAN_BUDGET_SECONDS. A separate process is needed because a
    regex holds the GIL: a stuck scan in a thread would freeze the test runner too.
    Returns the outcome ("accepted" or the error code).
    """
    patterns = {name: getattr(upload_security, name).pattern.hex() for name in PDF_SCAN_PATTERNS}
    env = {**os.environ, "PDF_SCAN_PATTERNS": json.dumps(patterns)}
    cmd = [sys.executable, "-m", "tests.security.pdf_scan_child", profile.value, payload]
    try:
        out = subprocess.run(cmd, cwd=BACKEND_DIR, env=env, capture_output=True, timeout=kill_after, check=False)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            f"{profile.value}/{payload}: the PDF scan was still running after {kill_after:.0f} s and was killed"
        ) from None
    assert out.returncode == 0, f"{profile.value}/{payload}: child failed: {out.stderr.decode(errors='replace')[-800:]}"
    result = json.loads(out.stdout.decode().strip().splitlines()[-1])
    elapsed = float(result["elapsed"])
    assert elapsed < SCAN_BUDGET_SECONDS, (
        f"{profile.value}/{payload}: validation took {elapsed:.2f} s (budget {SCAN_BUDGET_SECONDS} s)"
    )
    return str(result["outcome"])


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
    assert counter[0] <= (MAX_REQUEST_BODY_BYTES // (64 * 1024)) + 2, "the server kept reading long after the limit"


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
        except Exception as exc:  # noqa: BLE001
            # Only InvalidStorageKey is a refusal. An OSError or ValueError from deeper down means
            # the key got past the checks and only failed later, by luck.
            raise AssertionError(f"store accepted: {what} (it failed later with {type(exc).__name__})") from exc
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


# ─── Zip member names (finding 5 of the second review) ───────────────────────

def zip_entries(entries: list[tuple[str, bytes]]) -> bytes:
    """
    A zip built from a LIST, so the same or case-variant names can appear more than once. A name
    with a backslash or NUL is written with a placeholder ("|", \x02) and patched in the raw bytes
    afterwards, because zipfile turns backslashes into slashes while writing on Windows and cuts
    names at NUL.
    """
    def placeholder(name: str) -> str:
        return name.replace("\\", "|").replace("\x00", "\x02")

    import warnings

    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # zipfile warns about duplicate names
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries:
                zf.writestr(placeholder(name), content)
    raw = buf.getvalue()
    for name, _ in entries:
        if placeholder(name) != name:
            raw = raw.replace(placeholder(name).encode(), name.encode())
    return raw


XXE_PART = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "boom">]><root>&a;</root>'
_MAIN_PARTS = {"docx": "word/document.xml", "pptx": "ppt/presentation.xml", "xlsx": "xl/workbook.xml"}
_ROOTS = {"docx": "word", "pptx": "ppt", "xlsx": "xl"}


def _package(kind: str, extra: list[tuple[str, bytes]]) -> bytes:
    return zip_entries([(CT, CT_XML), (_MAIN_PARTS[kind], EMPTY_XML), *extra])


def assert_zip_member_names_rejected() -> None:
    """
    For docx, pptx and xlsx: a member name that is absolute, has a backslash, '..', a colon, a
    control character or NUL, and two names that collide (same bytes, case variants, NFC/NFD, NUL
    cut-off) are rejected with invalid_openxml_format. The review case is the second of two
    case-variant entries, which used to be left unchecked.
    """
    for kind in ("docx", "pptx", "xlsx"):
        profile = UploadProfile.TIMETABLE if kind == "xlsx" else UploadProfile.DOCUMENT
        root = _ROOTS[kind]
        bad_names = [
            "/x.xml", "\\x.xml", f"{root}\\x.xml", f"{root}/../x.xml", f"{root}/x:y.xml",
            f"{root}/x\x01y.xml", f"{root}/x\ny.xml", f"{root}/x\x00y.xml",
        ]
        collisions = [
            [(f"{root}/a.xml", EMPTY_XML), (f"{root.upper()}/a.xml", XXE_PART)],
            [(f"{root}/a.xml", EMPTY_XML), (f"{root}/a.xml", EMPTY_XML)],
            [(f"{root}/é.xml", EMPTY_XML), (f"{root}/é.xml", EMPTY_XML)],
        ]
        variants = [("name " + repr(n), [(n, EMPTY_XML)]) for n in bad_names]
        variants += [(f"collision {i}", c) for i, c in enumerate(collisions)]
        for label, extra in variants:
            data = _package(kind, extra)
            try:
                validate_upload(data, f"a.{kind}", profile)
            except UploadSecurityError as exc:
                assert code_of(exc) == "invalid_openxml_format", f"{kind} {label}: wrong code {code_of(exc)}"
            else:
                raise AssertionError(f"{kind}: member names were accepted: {label}")


# ─── Slide count (finding 6) ─────────────────────────────────────────────────

_P_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def presentation_xml(slides: int) -> bytes:
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="rId{i}"/>' for i in range(slides))
    return (
        f'<p:presentation xmlns:p="{_P_NS}" xmlns:r="{_R_NS}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>'
    ).encode()


def assert_slide_cap_counts_slide_list_entries() -> None:
    """
    The cap counts the p:sldId entries of the slide list: 101 entries are refused whatever the slide
    parts are called (or when there are none), and 100 are accepted even when 150 parts are named
    slideN.xml (parts that no p:sldId points to are not slides).
    """
    renamed = [(f"ppt/slides/renamed_{i}.xml", EMPTY_XML) for i in range(101)]
    for label, parts, count in (("renamed parts", renamed, 101), ("no parts", [], 101)):
        data = zip_entries([(CT, CT_XML), ("ppt/presentation.xml", presentation_xml(count)), *parts])
        try:
            validate_upload(data, "a.pptx", UploadProfile.DOCUMENT)
        except UploadSecurityError as exc:
            assert code_of(exc) == "too_many_slides", f"{label}: wrong code {code_of(exc)}"
        else:
            raise AssertionError(f"{label}: a presentation with 101 slides was accepted")
    orphans = [(f"ppt/slides/slide{i}.xml", EMPTY_XML) for i in range(1, 151)]
    ok = zip_entries([(CT, CT_XML), ("ppt/presentation.xml", presentation_xml(100)), *orphans])
    assert validate_upload(ok, "a.pptx", UploadProfile.DOCUMENT) == "pptx", "100 slides were refused"


# ─── Atomic writes (finding 10) ──────────────────────────────────────────────

async def assert_failed_write_leaves_nothing(store: Any, root: Path, tenant_id: uuid.UUID) -> None:
    """
    A put whose write fails (here: the data is not bytes, so the write fails after the file was
    opened) leaves nothing: no object under the key, no partial file, no temp file.
    """
    key = ds.new_document_key(tenant_id)
    try:
        await store.put(tenant_id, key, object())
    except Exception:  # noqa: BLE001 - any failure is the expected one
        pass
    else:
        raise AssertionError("the injected write failure did not happen")
    leftovers = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert not leftovers, f"a failed write left files behind: {leftovers}"
    assert await store.get(tenant_id, key) is None, "a partial object can be read after a failed write"
