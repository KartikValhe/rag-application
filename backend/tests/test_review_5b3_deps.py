"""
Review of step 5b-3, finding 12: parser dependencies. lxml and Pillow are pulled in by python-docx and
python-pptx and were not pinned; the document parser's libraries belong in a `[parser]` extra, and nothing
native may be imported by the API process (a native parser bug in the API process is worse than in the
sandbox child).

What stays in the base dependencies, and why (deviation from the review text, see the 5b-3 review record):
  python-docx, lxml  the timetable DOCX import runs python-docx in the timetable child, which the API image
                     starts; the API PROCESS itself still never imports them (checked below)
  pypdfium2          pdfplumber (timetable PDF import, same child) depends on it
  Pillow             `app/services/storage.py` validates branding images with it in the API process
Only python-pptx is document-parser-only.
"""

import importlib.metadata
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
# What the parser extra adds on top of the API: native or XML-parsing libraries for untrusted documents
NATIVE_PARSER_MODULES = ("pypdfium2", "docx", "pptx", "lxml")


def _pyproject() -> dict[str, object]:
    return tomllib.loads((BACKEND / "pyproject.toml").read_text(encoding="utf-8"))


def _names(requirements: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for req in requirements:
        name = req.split("[")[0].split("=")[0].split(">")[0].split("<")[0].strip().lower()
        out[name] = req
    return out


def test_the_api_app_imports_without_any_native_parser_module(tmp_path: Path) -> None:
    """Import app.main with the parser extra's modules blocked: it must work, and none may be loaded.
    The subprocess runs in an empty directory, so no .env file is found or read."""
    import os

    code = (
        "import importlib.abc, sys\n"
        f"BLOCK = {set(NATIVE_PARSER_MODULES)!r}\n"
        "class Blocker(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCK:\n"
        "            raise ImportError('blocked ' + name)\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "import app.main\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in BLOCK)\n"
        "print('LOADED=' + ','.join(loaded))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    out = subprocess.run(
        [sys.executable, "-P", "-c", code], cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.strip().splitlines()[-1] == "LOADED="


def test_the_api_app_does_not_load_a_native_parser_module_even_when_they_are_installed(tmp_path: Path) -> None:
    """The same import WITHOUT the blocker: a library that is merely installed must not be pulled in by
    anything (openpyxl, for one, imports lxml when it finds it: the API process turns that off)."""
    import os

    code = (
        "import sys\n"
        "import app.main\n"
        f"loaded = sorted(m for m in sys.modules if m.split('.')[0] in {set(NATIVE_PARSER_MODULES)!r})\n"
        "print('LOADED=' + ','.join(loaded))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND)
    out = subprocess.run(
        [sys.executable, "-P", "-c", code], cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert out.returncode == 0, out.stderr[-1500:]
    assert out.stdout.strip().splitlines()[-1] == "LOADED="


def test_the_parser_extra_pins_every_parser_dependency_exactly() -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    extras = project["optional-dependencies"]
    parser = _names(extras["parser"])
    assert set(parser) == {"pypdfium2", "python-docx", "python-pptx", "lxml", "pillow"}
    for name, req in parser.items():
        assert "==" in req and not any(op in req for op in (">=", "<=", "~=", ">", "<")), f"{name} is not pinned exactly: {req}"
    # The API image does not need python-pptx (only the document parser uses it)
    base = _names(project["dependencies"])
    assert "python-pptx" not in base
    # What the base keeps (timetable DOCX child, PDFium through pdfplumber, branding images) is pinned to
    # the SAME versions as the parser extra, so one set of versions is installed everywhere
    for name in ("python-docx", "lxml", "pillow", "pypdfium2"):
        assert base[name] == parser[name], name
    # `pip install ".[dev]"` (CI, Dockerfile, CLAUDE.md) still installs everything the tests need
    assert any(req.lower().startswith("rag-platform-backend[parser]") for req in extras["dev"])


@pytest.mark.parametrize("name", ["pypdfium2", "python-docx", "python-pptx", "lxml", "pillow"])
def test_the_installed_versions_are_the_pinned_ones(name: str) -> None:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    pinned = _names(project["optional-dependencies"]["parser"])[name].split("==")[1]
    assert importlib.metadata.version(name) == pinned


def test_the_readme_says_what_pip_audit_cannot_see() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "pip-audit" in readme and "PDFium" in readme
    assert "cannot see" in readme and "each release" in readme
