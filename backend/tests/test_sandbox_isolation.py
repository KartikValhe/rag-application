"""
What the sandbox child can learn about the process that started it (finding 2 of the review of 5b-1/5b-2).

The child's environment is clean, but it runs as the same user. On Linux a parser exploit could read
/proc/<parent pid>/environ (database URLs, SECRET_KEY) unless the parent is not dumpable, so the worker
calls prctl(PR_SET_DUMPABLE, 0) at startup and the child does it before it touches any input. The child's
import path also holds no more than it needs.

The Linux tests need a non-root user (root can read every /proc file whatever the flag says). They are
skipped on Windows and macOS, where none of these protections exist: that is development only. This
module imports only the standard library and app.core.sandbox so it can run on a bare Python image.
"""

import ctypes
import os
import sys
import sysconfig
from pathlib import Path

import pytest

from app.core import sandbox, sandbox_child

CHILD = "tests._sandbox_children"
LIMITS = sandbox.SandboxLimits(timeout_seconds=20.0, max_output_bytes=1024 * 1024)
PR_SET_DUMPABLE = 4

linux_non_root = pytest.mark.skipif(
    not sys.platform.startswith("linux") or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="Linux only, and not as root (root reads every /proc file): Windows and root have no such protection",
)


async def _run(mode: str) -> dict[str, object]:
    raw = await sandbox.run_child(CHILD, b"", args=(mode,), params={}, limits=LIMITS)
    return sandbox.parse_child_json(raw)


def _set_dumpable(value: int) -> None:
    ctypes.CDLL(None, use_errno=True).prctl(PR_SET_DUMPABLE, value, 0, 0, 0)


# ── Linux: the child cannot read the parent's environment ────────────────────


@linux_non_root
async def test_the_child_cannot_read_the_parents_environ() -> None:
    # /proc/<pid>/environ holds the environment the parent was STARTED with (this is where a worker's
    # DATABASE_URL and SECRET_KEY live); PATH is always in it
    _set_dumpable(1)
    try:
        # Control: with a dumpable parent the same child CAN read it, so the check below measures something
        control = await _run("parent_environ")
        assert control["readable"] is True and "PATH=" in str(control["text"])

        assert sandbox_child.set_non_dumpable() is True  # what the worker does at startup
        result = await _run("parent_environ")
        assert result["readable"] is False, "the child read the parent's environment"
        assert "PATH=" not in str(result.get("text", ""))
    finally:
        _set_dumpable(1)  # keep the rest of the test process debuggable and dumpable


@linux_non_root
async def test_the_child_makes_itself_non_dumpable_before_it_touches_input() -> None:
    result = await _run("dumpable")
    assert result["dumpable"] == 0


# ── Everywhere: what the helper does off Linux, the worker's call, the import path ──────────────


@pytest.mark.skipif(sys.platform.startswith("linux"), reason="checks the non-Linux fallback")
def test_set_non_dumpable_is_a_safe_no_op_off_linux() -> None:
    assert sandbox_child.set_non_dumpable() is False


def test_the_worker_makes_itself_non_dumpable_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers import ingest

    calls: list[str] = []

    def record() -> bool:
        calls.append("set_non_dumpable")
        return True

    def not_configured(*_args: object, **_kwargs: object) -> object:
        raise ingest.WorkerNotConfigured("the embedding model is not configured")

    monkeypatch.setattr(sandbox_child, "set_non_dumpable", record)
    monkeypatch.setattr(ingest, "prepare_embedder", not_configured)
    assert ingest.main() == 2
    assert calls == ["set_non_dumpable"], "the worker did not harden itself before doing anything else"


def test_the_timetable_child_is_hardened_too() -> None:
    source = (Path(sandbox.__file__).parent.parent / "services" / "timetable_parse_child.py").read_text(encoding="utf-8")
    assert source.index("set_non_dumpable()") < source.index("sys.stdin.buffer.read()")


def test_the_child_import_path_leaves_out_the_interpreters_own_site_directory(tmp_path: Path) -> None:
    site = Path(sysconfig.get_paths()["purelib"])
    module = "app.services.document_parse_child"
    assert sandbox.child_python_path(module, site) == "", "the package is installed: nothing extra is needed"
    assert "PYTHONPATH" not in sandbox.child_env(str(tmp_path), module, backend_root=site)

    source_tree = tmp_path / "backend"  # run from a source tree: only the directory that holds `app`
    source_tree.mkdir()
    assert sandbox.child_python_path(module, source_tree) == str(source_tree)
    assert sandbox.child_env(str(tmp_path), module, backend_root=source_tree)["PYTHONPATH"] == str(source_tree)
