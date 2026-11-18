"""
tests/test_run_tests.py
───────────────────────
The tier runner (app/scripts/run_tests.py): tier selection, the --touched mapping, the lock file, the
summary line and the exit code. No real pytest run happens here: pytest is replaced by a fake `spawn`.
"""

import os
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from app.scripts import run_tests as rt

# ── tier selection ───────────────────────────────────────────────────────────


def test_fast_selects_the_fast_marker_only() -> None:
    assert rt.build_pytest_args("fast") == ["-m", "fast"]


def test_gate_selects_fast_and_security() -> None:
    assert rt.build_pytest_args("gate") == ["-m", "fast or security"]


def test_full_has_no_marker_filter() -> None:
    assert rt.build_pytest_args("full") == []


def test_extra_pytest_arguments_are_passed_through_after_the_selection() -> None:
    assert rt.build_pytest_args("fast", ["-k", "retry", "tests/test_x.py"]) == [
        "-m", "fast", "-k", "retry", "tests/test_x.py",
    ]
    assert rt.build_pytest_args("full", ["tests/test_x.py"]) == ["tests/test_x.py"]


def test_touched_tests_extend_the_gate_only() -> None:
    gate = rt.build_pytest_args("gate", touched_tests=["test_a.py", "security/test_b.py"])
    assert gate == ["-m", "fast or security or touched", "--touched-tests", "test_a.py,security/test_b.py"]
    assert rt.build_pytest_args("fast", touched_tests=["test_a.py"]) == ["-m", "fast"]
    assert rt.build_pytest_args("full", touched_tests=["test_a.py"]) == []


def test_an_unknown_tier_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown tier"):
        rt.build_pytest_args("everything")


def test_main_rejects_touched_outside_the_gate() -> None:
    with pytest.raises(SystemExit) as excinfo:
        rt.main(["fast", "--touched"])
    assert excinfo.value.code == 2


def test_the_markers_the_runner_selects_exist_in_the_tier_table() -> None:
    from tests.tier_map import TEST_FILE_TIERS

    used = {m for marks in TEST_FILE_TIERS.values() for m in marks}
    assert {"fast", "security"} <= used  # the gate selects something real
    assert any("fast" in marks for marks in TEST_FILE_TIERS.values())


# ── --touched mapping ────────────────────────────────────────────────────────

TESTS = [
    "test_document_upload.py", "test_document_routes.py", "test_isolation.py",
    "test_authz_rbac.py", "security/test_prove_it_fails_worker.py",
]


def test_a_changed_test_file_selects_itself() -> None:
    assert rt.touched_test_files(["tests/test_isolation.py"], TESTS) == ["test_isolation.py"]
    assert rt.touched_test_files(["tests/security/test_prove_it_fails_worker.py"], TESTS) == [
        "security/test_prove_it_fails_worker.py"
    ]


def test_a_changed_module_selects_the_tests_that_carry_its_name() -> None:
    assert rt.touched_test_files(["app/services/document_upload.py"], TESTS) == ["test_document_upload.py"]
    assert rt.touched_test_files(["app/services/rbac.py"], TESTS) == ["test_authz_rbac.py"]


def test_short_private_and_unrelated_modules_select_nothing() -> None:
    changed = ["app/db/engine.py", "app/__init__.py", "app/core/_x.py", "app/api/v1/db.py", "README.md"]
    assert rt.touched_test_files(changed, TESTS) == []


def test_windows_separators_and_duplicates_are_handled() -> None:
    changed = ["app\\services\\document_upload.py", "tests\\test_document_upload.py"]
    assert rt.touched_test_files(changed, TESTS) == ["test_document_upload.py"]


def test_shared_test_code_is_detected() -> None:
    assert rt.shared_test_code_changed(["tests/conftest.py"])
    assert rt.shared_test_code_changed(["tests/fixtures/worker.py"])
    assert not rt.shared_test_code_changed(["app/services/rbac.py", "tests/test_isolation.py"])


# ── lock file ────────────────────────────────────────────────────────────────


def test_a_second_run_refuses_while_the_first_holds_the_lock(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    assert rt.acquire_lock(lock) is None
    assert rt.acquire_lock(lock) == os.getpid()  # the holder is alive (this process)
    rt.release_lock(lock)
    assert not lock.exists()
    assert rt.acquire_lock(lock) is None
    rt.release_lock(lock)


def test_a_stale_lock_of_a_dead_process_is_replaced(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    lock.write_text("999999999")  # no such process
    assert rt.acquire_lock(lock) is None
    assert lock.read_text() == str(os.getpid())
    rt.release_lock(lock)


def test_release_never_removes_somebody_elses_lock(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    lock.write_text("999999999")
    rt.release_lock(lock)
    assert lock.exists()


def test_run_refuses_to_start_when_locked_and_does_not_call_pytest(tmp_path: Path) -> None:
    lock = tmp_path / "run.lock"
    assert rt.acquire_lock(lock) is None
    calls: list[Sequence[str]] = []
    lines: list[str] = []

    def spawn(args: Sequence[str], emit: Callable[[str], None]) -> int:
        calls.append(args)
        return 0

    code = rt.run("fast", spawn=spawn, lock_path=lock, out=lines.append)
    assert code == rt.EXIT_LOCKED
    assert calls == []
    assert "another run is in progress" in lines[-1]
    assert lock.exists()  # the holder's lock is untouched
    rt.release_lock(lock)


# ── summary line and exit code ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "874 passed, 12 skipped in 479.64s (0:07:59)",
        "============ 3 passed, 1 skipped in 2.10s ============",
        "== 1 failed, 5 passed, 2 warnings in 0.52s ==",
        "400 passed, 5 skipped, 4 xfailed in 250.27s (0:04:10)",
        "171 failed, 931 passed, 44 skipped, 4 xfailed, 616 errors in 153.00s (0:02:33)",
        "1 error in 0.50s",
        "no tests ran in 0.01s",
        "========================= 1766 deselected in 1.68s =========================",
        "1 passed, 1 deselected in 0.02s",
    ],
)
def test_summary_lines_are_recognised(line: str) -> None:
    assert rt.SUMMARY_RE.match(line), line


@pytest.mark.parametrize(
    "line",
    [
        "tests/test_x.py::test_a PASSED                                         [ 50%]",
        "collected 12 items",
        "ERROR: file or directory not found: nope",
        "tests/test_x.py::test_runs_in 3s PASSED",
        "",
    ],
)
def test_other_lines_are_not_summary_lines(line: str) -> None:
    assert not rt.SUMMARY_RE.match(line), line


def test_find_summary_returns_the_last_summary_verbatim() -> None:
    lines = ["a\n", "1 passed in 0.10s\n", "noise\n", "=== 2 failed, 1 passed in 0.30s ===\n", "tail\n"]
    assert rt.find_summary(lines) == "=== 2 failed, 1 passed in 0.30s ==="
    assert rt.find_summary(["nothing\n"]) is None


def _fake_spawn(output: list[str], code: int, seen: list[Sequence[str]]):  # type: ignore[no-untyped-def]
    def spawn(args: Sequence[str], emit: Callable[[str], None]) -> int:
        seen.append(args)
        for line in output:
            emit(line)
        return code

    return spawn


def test_the_summary_is_the_last_line_and_the_exit_code_is_pytests(tmp_path: Path) -> None:
    seen: list[Sequence[str]] = []
    lines: list[str] = []
    spawn = _fake_spawn(
        ["collected 3 items\n", "FAILED tests/test_x.py::test_b\n",
         "=========== 1 failed, 2 passed in 0.40s ===========\n"],
        1, seen,
    )
    code = rt.run("gate", spawn=spawn, lock_path=tmp_path / "l", out=lines.append)
    assert code == 1
    assert seen == [["-m", "fast or security"]]
    assert lines[-1] == "=========== 1 failed, 2 passed in 0.40s ==========="
    assert not (tmp_path / "l").exists()  # the lock is released


def test_without_a_summary_line_the_last_line_says_so_and_the_code_is_kept(tmp_path: Path) -> None:
    lines: list[str] = []
    spawn = _fake_spawn(["ERROR: Test file(s) without a tier:\n"], 4, [])
    code = rt.run("fast", spawn=spawn, lock_path=tmp_path / "l", out=lines.append)
    assert code == 4
    assert lines[-1] == "run_tests: pytest printed no summary line (exit code 4)"


def test_the_lock_is_released_even_when_pytest_cannot_be_started(tmp_path: Path) -> None:
    def spawn(args: Sequence[str], emit: Callable[[str], None]) -> int:
        raise OSError("no python")

    with pytest.raises(OSError):
        rt.run("full", spawn=spawn, lock_path=tmp_path / "l", out=lambda s: None)
    assert not (tmp_path / "l").exists()


def test_touched_adds_the_test_files_of_the_changed_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rt, "changed_files", lambda: ["app/services/rbac.py", "tests/conftest.py"])
    monkeypatch.setattr(rt, "discover_test_files", lambda: ["test_authz_rbac.py", "test_isolation.py"])
    seen: list[Sequence[str]] = []
    lines: list[str] = []
    rt.run("gate", touched=True, spawn=_fake_spawn(["1 passed in 0.1s\n"], 0, seen),
           lock_path=tmp_path / "l", out=lines.append)
    assert seen == [["-m", "fast or security or touched", "--touched-tests", "test_authz_rbac.py"]]
    assert any("touched test files: test_authz_rbac.py" in line for line in lines)
    assert any("shared test code changed" in line for line in lines)
    assert lines[-1] == "1 passed in 0.1s"


def test_touched_without_git_adds_nothing_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rt, "changed_files", lambda: None)
    seen: list[Sequence[str]] = []
    lines: list[str] = []
    rt.run("gate", touched=True, spawn=_fake_spawn(["1 passed in 0.1s\n"], 0, seen),
           lock_path=tmp_path / "l", out=lines.append)
    assert seen == [["-m", "fast or security"]]
    assert any("git could not list" in line for line in lines)
