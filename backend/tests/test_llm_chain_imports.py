"""
Step 6a-4 (docs/phase6-plan.md 7.1): "No other module imports an adapter."

app/llm/chain.py is the one way the rest of the app reaches a provider: it applies the policy, the limits, the
breaker and the cooldowns. A module that imported an adapter (or its shared transport) could call a provider
around all of that. This scan reads every file of app/ and fails on:
  * `import app.llm.groq`, `from app.llm.groq import ...`, `from app.llm import groq`, and the relative forms
    (`from .groq import ...`, `from . import groq`) of the adapter modules, outside the allowed files;
  * the dotted module name written as text anywhere else (importlib, __import__, a string for a lazy import).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parent.parent / "app"
LLM_DIR = APP_DIR / "llm"

ADAPTERS = frozenset({"groq", "openrouter", "gemini"})
# The pieces the three adapters are built from. Only the adapters (and each other) use them.
ADAPTER_PARTS = frozenset({"transport", "openai_compat", "errmap", "sse"})
CHAIN_FILE = "llm/chain.py"


def _allowed_importers(module: str) -> set[str]:
    """Which files of app/ may import app.llm.<module>."""
    insiders = {f"llm/{name}.py" for name in ADAPTERS | ADAPTER_PARTS}
    if module in ADAPTERS:
        return {CHAIN_FILE} | (insiders - {f"llm/{module}.py"})
    return insiders - {f"llm/{module}.py"}


def _resolve(node: ast.ImportFrom, path: Path) -> str | None:
    """The absolute module an ImportFrom names (relative imports resolved against the file's package)."""
    if node.level == 0:
        return node.module
    package = path.relative_to(APP_DIR.parent).with_suffix("").parts[:-1]
    base = list(package[: len(package) - (node.level - 1)])
    if node.module:
        base.append(node.module)
    return ".".join(base)


def imported_llm_modules(path: Path) -> set[str]:
    """The app.llm.<name> modules a file imports, by any import statement."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[:2] == ["app", "llm"] and len(parts) >= 3:
                    found.add(parts[2])
        elif isinstance(node, ast.ImportFrom):
            module = _resolve(node, path)
            if module is None:
                continue
            parts = module.split(".")
            if parts[:2] != ["app", "llm"]:
                continue
            if len(parts) >= 3:
                found.add(parts[2])
            else:  # from app.llm import groq
                found.update(alias.name for alias in node.names)
    return found & (ADAPTERS | ADAPTER_PARTS)


def _offenders() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for path in APP_DIR.rglob("*.py"):
        rel = path.relative_to(APP_DIR).as_posix()
        bad = sorted(m for m in imported_llm_modules(path) if rel not in _allowed_importers(m))
        if bad:
            result[rel] = bad
    return result


def test_no_module_but_the_chain_imports_an_adapter() -> None:
    assert _offenders() == {}


def test_the_chain_is_the_one_outside_importer_of_every_adapter() -> None:
    assert imported_llm_modules(LLM_DIR / "chain.py") & ADAPTERS == ADAPTERS
    assert imported_llm_modules(LLM_DIR / "chain.py") & ADAPTER_PARTS == set()


def test_no_file_names_an_adapter_module_as_text() -> None:
    # catches importlib.import_module("app.llm.groq") and similar lazy imports the AST scan cannot see
    needles = [f"app.llm.{name}" for name in ADAPTERS | ADAPTER_PARTS]
    exempt = {CHAIN_FILE} | {f"llm/{name}.py" for name in ADAPTERS | ADAPTER_PARTS}
    offenders = {}
    for path in APP_DIR.rglob("*.py"):
        rel = path.relative_to(APP_DIR).as_posix()
        if rel in exempt:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        hits = sorted({needle for needle in needles for s in strings if needle in s})
        if hits:
            offenders[rel] = hits
    assert offenders == {}


# ── the scan itself catches every spelling (so a green result means something) ───────────────────────


@pytest.mark.parametrize(
    ("source", "where"),
    [
        ("import app.llm.groq\n", "services/x.py"),
        ("import app.llm.groq as g\n", "services/x.py"),
        ("from app.llm.groq import GroqClient\n", "services/x.py"),
        ("from app.llm import groq\n", "services/x.py"),
        ("from app.llm import chain, gemini\n", "api/v1/x.py"),
        ("from app.llm.transport import HTTPAdapter\n", "services/x.py"),
        ("from .groq import GroqClient\n", "llm/policy.py"),
        ("from . import openrouter\n", "llm/fake.py"),
        ("from .transport import Stream\n", "llm/chain.py"),
        ("def f():\n    from app.llm.gemini import GeminiClient\n", "services/x.py"),
    ],
)
def test_the_scan_sees_every_way_of_importing_an_adapter(source: str, where: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = tmp_path / "app"
    target = fake_app / where
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "APP_DIR", fake_app)
    found = imported_llm_modules(target)
    assert found, source
    assert any(where not in _allowed_importers(m) for m in found), source


@pytest.mark.parametrize(
    "source",
    [
        "from app.llm.chain import answer_stream\n",
        "from app.llm import chain\n",
        "from app.llm.errors import LLMError\n",
        "from app.llm.types import LLMRequest\n",
        "import app.llm.models\n",
    ],
)
def test_the_scan_leaves_the_public_modules_alone(source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_app = tmp_path / "app"
    target = fake_app / "services" / "x.py"
    target.parent.mkdir(parents=True)
    target.write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "APP_DIR", fake_app)
    assert imported_llm_modules(target) == set()
