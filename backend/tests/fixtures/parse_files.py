"""
tests/fixtures/parse_files.py
─────────────────────────────
Small REAL files for the document parser tests (step 5b-3), generated inside the tests:

  - `make_text_pdf(...)`  a valid PDF built from bytes (correct xref table, standard Helvetica, no font
                          file). Devanagari goes in through a /ToUnicode map: the glyphs would be wrong
                          if the file were drawn, but PDFium extracts the right text, which is what
                          the parser tests need. No licensed font is needed (see the documented gap in
                          docs/phase5-design.md, 5b-3 record).
  - `make_docx(...)`, `make_pptx(...)`  with python-docx / python-pptx (the same libraries the parser uses).
  - `LeftoverGuard`       asserts that a parse left no child process, temp directory or stored file.
"""

import io
import tempfile
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.core import sandbox

# ── PDF ──────────────────────────────────────────────────────────────────────


def _escape_pdf(text: bytes) -> bytes:
    return text.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _to_unicode_cmap(mapping: dict[int, str]) -> bytes:
    entries = b"".join(b"<%02X> <%s>\n" % (code, ch.encode("utf-16-be").hex().upper().encode()) for code, ch in sorted(mapping.items()))
    return (
        b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap\n"
        b"/CMapName /Adobe-Identity-UCS def /CMapType 2 def\n"
        b"1 begincodespacerange <00> <FF> endcodespacerange\n"
        + b"%d beginbfchar\n" % len(mapping)
        + entries
        + b"endbfchar\nendcmap CMapName currentdict /CMap defineresource pop end end\n"
    )


def make_text_pdf(
    pages: list[str | None],
    *,
    unicode_text: bool = False,
    width: float = 612.0,
    height: float = 792.0,
    page_type_token: bytes = b"/Type /Page",
    extra_catalog: bytes = b"",
    font_size: int = 12,
    extra_content: bytes = b"",
    bare_empty_pages: bool = False,
) -> bytes:
    """
    A valid PDF, one entry of `pages` per page (None or "" = an empty page). `\\n` in a page text starts
    a new line. With `unicode_text` every distinct character gets a one-byte code and a /ToUnicode map
    (up to 220 distinct characters), so any Unicode text can be extracted. `page_type_token` lets a test
    write `/Type /Page` with unusual whitespace so the upload byte scan cannot count the pages.
    With `bare_empty_pages` an empty page has no content stream at all (one object per page).
    """
    mapping: dict[int, str] = {}
    code_of: dict[str, int] = {}

    def encode(line: str) -> bytes:
        if not unicode_text:
            return _escape_pdf(line.encode("latin-1"))
        out = bytearray()
        for ch in line:
            if ch not in code_of:
                code = 0x21 + len(code_of)
                if code > 0xFE:
                    raise ValueError("too many distinct characters for the one-byte test font")
                code_of[ch] = code
                mapping[code] = ch
            out.append(code_of[ch])
        return _escape_pdf(bytes(out))

    contents: list[bytes] = []
    for text in pages:
        lines = (text or "").split("\n") if text else []
        ops = b""
        if lines:
            ops = b"BT /F1 %d Tf 72 %d Td %d TL\n" % (font_size, int(height) - 72, font_size + 2)
            for line in lines:
                ops += b"(" + encode(line) + b") Tj T*\n"
            ops += b"ET\n"
        contents.append(ops + extra_content)

    # Object layout: 1 catalog, 2 pages, 3 font, 4 ToUnicode (or unused), then page/content pairs
    n_pages = len(pages)
    first_page_obj = 5
    kids = b" ".join(b"%d 0 R" % (first_page_obj + 2 * i) for i in range(n_pages))
    objs: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R " + extra_catalog + b">>",
        2: b"<< /Type /Pages /Kids [" + kids + b"] /Count %d >>" % n_pages,
    }
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
    if unicode_text:
        font += b" /ToUnicode 4 0 R"
    objs[3] = font + b" >>"
    cmap = _to_unicode_cmap(mapping) if unicode_text else b""
    objs[4] = b"<< /Length %d >>\nstream\n" % len(cmap) + cmap + b"endstream"
    for i, content in enumerate(contents):
        page_no = first_page_obj + 2 * i
        if bare_empty_pages and not pages[i]:
            objs[page_no] = b"<< " + page_type_token + b" /Parent 2 0 R /MediaBox [0 0 %s %s] >>" % (_num(width), _num(height))
            continue
        objs[page_no] = (
            b"<< "
            + page_type_token
            + b" /Parent 2 0 R /MediaBox [0 0 %s %s] /Contents %d 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
            % (_num(width), _num(height), page_no + 1)
        )
        objs[page_no + 1] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream"
    return _assemble(objs)


def _num(value: float) -> bytes:
    return (b"%d" % value) if float(value).is_integer() else (b"%.2f" % value)


def _assemble(objs: dict[int, bytes], trailer_extra: bytes = b"") -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for number in sorted(objs):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objs[number] + b"\nendobj\n"
    xref = len(out)
    size = max(objs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += (b"%010d 00000 n \n" % offsets[number]) if number in offsets else b"0000000000 65535 f \n"
    out += b"trailer\n<< /Size %d /Root 1 0 R %s>>\nstartxref\n%d\n%%%%EOF\n" % (size, trailer_extra, xref)
    return bytes(out)


def make_encrypted_pdf() -> bytes:
    """A PDF with a standard security handler whose user password is not empty: it needs a password."""
    base = make_text_pdf(["secret text"])
    # A /Encrypt dictionary with an /O and /U that no empty password can satisfy
    enc = b"<< /Filter /Standard /V 1 /R 2 /O <%s> /U <%s> /P -4 >>" % (b"AA" * 32, b"BB" * 32)
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>", 4: enc}
    del base
    return _assemble(objs, b"/Encrypt 4 0 R /ID [<" + b"CC" * 16 + b"> <" + b"CC" * 16 + b">] ")


# ── DOCX and PPTX ────────────────────────────────────────────────────────────


def docx_bytes(build: Any) -> bytes:
    """Build a DOCX: `build(document)` fills a python-docx Document, which is returned as bytes."""
    import docx

    document = docx.Document()
    build(document)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def pptx_bytes(build: Any) -> bytes:
    """Build a PPTX: `build(presentation)` fills a python-pptx Presentation, which is returned as bytes."""
    import pptx

    presentation = pptx.Presentation()
    build(presentation)
    buf = io.BytesIO()
    presentation.save(buf)
    return buf.getvalue()


def rewrite_zip_member(data: bytes, name: str, transform: Any) -> bytes:
    """A copy of a zip with one member's bytes replaced by `transform(old_bytes)`."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            content = src.read(info.filename)
            dst.writestr(info.filename, transform(content) if info.filename == name else content)
    return out.getvalue()


# ── No leftovers ─────────────────────────────────────────────────────────────


class LeftoverGuard:
    """
    Records every sandbox child started while it is active and the `sandbox_*` temp directories that
    exist before it, then (check()) asserts that all children have exited, no temp directory is left
    and the stored files are unchanged. Cross-platform: it watches the Popen objects the sandbox
    creates instead of listing processes.
    """

    def __init__(self, store_root: Path | None = None) -> None:
        self.store_root = store_root
        self.children: list[Any] = []
        self._tmp_before: set[str] = set()
        self._store_before: set[str] = set()

    @staticmethod
    def _sandbox_dirs() -> set[str]:
        return {p.name for p in Path(tempfile.gettempdir()).glob("sandbox_*")}

    def _store_files(self) -> set[str]:
        if self.store_root is None or not self.store_root.exists():
            return set()
        return {str(p.relative_to(self.store_root)) for p in self.store_root.rglob("*") if p.is_file()}

    def start(self, monkeypatch: Any) -> None:
        import subprocess

        self._tmp_before = self._sandbox_dirs()
        self._store_before = self._store_files()
        children = self.children
        real_popen = subprocess.Popen

        class Recording(real_popen):  # type: ignore[type-arg, misc]
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                children.append(self)

        monkeypatch.setattr(sandbox.subprocess, "Popen", Recording)

    def check(self) -> None:
        for child in self.children:
            assert child.poll() is not None, "a sandbox child process is still running"
        left = self._sandbox_dirs() - self._tmp_before
        assert not left, f"temporary sandbox directories were left behind: {len(left)}"
        assert self._store_files() == self._store_before, "the stored files changed"


@contextmanager
def leftover_guard(monkeypatch: Any, store_root: Path | None = None) -> Iterator[LeftoverGuard]:
    guard = LeftoverGuard(store_root)
    guard.start(monkeypatch)
    yield guard
    guard.check()
