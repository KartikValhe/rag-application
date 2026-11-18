"""
app/scripts/run_tests.py
────────────────────────
One entry point for the backend tests, by tier:

    python -m app.scripts.run_tests fast  [pytest args]
    python -m app.scripts.run_tests gate  [--touched] [pytest args]
    python -m app.scripts.run_tests full  [pytest args]

  fast  tests that need no Postgres, Redis or child process (`-m fast`)
  gate  fast + security tests (`-m "fast or security"`); with --touched also the test files of the
        modules changed in the working tree (git diff against HEAD, plus untracked files)
  full  everything (CI, or on explicit request)

The tiers come from the markers that tests/tier_map.py gives every test file. Extra arguments go to pytest
unchanged, for example `run_tests full tests/test_document_upload.py -k retry`.

Guarantees:
  * never two runs at once: a lock file in the temp directory makes a second run_tests refuse to start
    (it cannot stop someone from running plain `pytest` next to it);
  * the pytest final summary line (for example `874 passed, 12 skipped in 479.64s (0:07:59)`) is printed
    verbatim as the LAST line of the output;
  * the exit code is pytest's own (0 passed, 1 failures, 2 interrupted, 3 internal error, 4 usage error
    or an unmapped test file, 5 nothing collected). If another run holds the lock the exit code is 75.

pytest only ever uses the dedicated test database (tests/conftest.py enforces that).
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

TIERS = ("fast", "gate", "full")
TIER_EXPRESSIONS: dict[str, str | None] = {
    "fast": "fast",
    "gate": "fast or security",
    "full": None,  # no marker filter: everything
}
BACKEND_DIR = Path(__file__).resolve().parents[2]
EXIT_LOCKED = 75
LOCK_PATH = Path(tempfile.gettempdir()) / "rag_run_tests.lock"

# pytest's last line: "=== 3 passed, 1 skipped in 2.10s ===" (verbose) or "3 passed in 2.10s (0:00:02)" (-q).
SUMMARY_RE = re.compile(
    r"^(?:=+ )?(?:no tests ran|\d+ [A-Za-z]+(?: [a-z]+)?.*) in \d+(?:\.\d+)?s(?: \(\d+:\d{2}:\d{2}\))?(?: =+)?$"
)

# Runs pytest with the given arguments, passes each output line to the callback and returns the exit code;
# replaced in the unit tests so they need no real pytest run.
Spawn = Callable[[Sequence[str], Callable[[str], None]], int]


# ── tier selection (pure, unit-tested) ───────────────────────────────────────


def build_pytest_args(
    tier: str, extra: Sequence[str] = (), touched_tests: Sequence[str] = ()
) -> list[str]:
    """The pytest arguments for a tier. `touched_tests` (paths relative to tests/) only matter for the
    gate: they are added to the selection through the 'touched' marker (see tests/conftest.py)."""
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}; use one of {', '.join(TIERS)}")
    args: list[str] = []
    expression = TIER_EXPRESSIONS[tier]
    if expression is not None:
        if tier == "gate" and touched_tests:
            expression += " or touched"
        args += ["-m", expression]
    if tier == "gate" and touched_tests:
        args += ["--touched-tests", ",".join(touched_tests)]
    return args + list(extra)


def touched_test_files(changed: Iterable[str], test_files: Iterable[str]) -> list[str]:
    """Map changed files (paths relative to backend/) to test files (relative to tests/).

    A changed test file selects itself. A changed module under app/ selects every test file whose name
    contains the module's file name (app/services/document_upload.py -> test_document_upload.py). Names
    shorter than 4 characters and private modules are ignored. Shared test code (conftest, fixtures,
    tier_map) cannot be mapped: run the full tier for that."""
    tests = sorted(set(test_files))
    selected: set[str] = set()
    for raw in changed:
        path = raw.replace("\\", "/")
        if path.startswith("tests/"):
            rel = path[len("tests/") :]
            if rel in tests:
                selected.add(rel)
        elif path.startswith("app/") and path.endswith(".py"):
            stem = Path(path).stem
            if stem.startswith("_") or len(stem) < 4:
                continue
            selected.update(t for t in tests if stem in Path(t).stem)
    return sorted(selected)


def shared_test_code_changed(changed: Iterable[str]) -> bool:
    """True when a file changed that every test depends on (so --touched cannot be trusted alone)."""
    shared = ("tests/conftest.py", "tests/tier_map.py", "pyproject.toml")
    for raw in changed:
        path = raw.replace("\\", "/")
        if path in shared or path.startswith("tests/fixtures/"):
            return True
    return False


def discover_test_files(backend_dir: Path = BACKEND_DIR) -> list[str]:
    tests_root = backend_dir / "tests"
    return sorted(p.relative_to(tests_root).as_posix() for p in tests_root.rglob("test_*.py"))


def changed_files(backend_dir: Path = BACKEND_DIR) -> list[str] | None:
    """Files changed in the working tree relative to HEAD (tracked) plus untracked files, as paths relative
    to backend/. None when git cannot tell."""
    try:
        tracked = subprocess.run(
            ["git", "diff", "--name-only", "--relative", "HEAD"],
            cwd=backend_dir, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.splitlines()
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=backend_dir, capture_output=True, text=True, timeout=30, check=True,
        ).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    return sorted({line.strip() for line in tracked + untracked if line.strip()})


# ── lock file: never two runs at once ────────────────────────────────────────


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid, 0) would TERMINATE the process on Windows, so ask the OS instead.
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def acquire_lock(path: Path = LOCK_PATH) -> int | None:
    """Create the lock file. Returns None on success, or the pid of the run that holds it. A lock left by
    a process that no longer exists is replaced."""
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = int(path.read_text().strip() or "0")
            except (OSError, ValueError):
                holder = 0
            if holder and _pid_alive(holder):
                return holder
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            continue
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        return None
    return -1  # lost a race twice: somebody else is starting right now


def release_lock(path: Path = LOCK_PATH) -> None:
    try:
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


# ── running pytest ───────────────────────────────────────────────────────────


def find_summary(lines: Iterable[str]) -> str | None:
    """The last line that looks like pytest's final summary, verbatim (without the line break)."""
    found: str | None = None
    for line in lines:
        text = line.rstrip("\r\n")
        if SUMMARY_RE.match(text):
            found = text
    return found


def spawn_pytest(args: Sequence[str], emit: Callable[[str], None]) -> int:
    """Run pytest in a child process from backend/, echoing its output line by line."""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pytest", *args],
        cwd=BACKEND_DIR, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            emit(line)
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        return 2


def run(
    tier: str,
    extra: Sequence[str] = (),
    *,
    touched: bool = False,
    spawn: Spawn = spawn_pytest,
    lock_path: Path = LOCK_PATH,
    out: Callable[[str], None] | None = None,
) -> int:
    """Run one tier. Prints the pytest summary line last and returns pytest's exit code."""
    write = out or (lambda s: print(s, flush=True))
    touched_tests: list[str] = []
    if touched:
        changed = changed_files()
        if changed is None:
            write("run_tests: git could not list the changed files; --touched adds nothing")
        else:
            touched_tests = touched_test_files(changed, discover_test_files())
            write(f"run_tests: touched test files: {', '.join(touched_tests) or '(none)'}")
            if shared_test_code_changed(changed):
                write("run_tests: shared test code changed (conftest, fixtures, tier_map or pyproject): "
                      "run the full tier too")
    args = build_pytest_args(tier, extra, touched_tests)

    holder = acquire_lock(lock_path)
    if holder is not None:
        write(f"run_tests: another run is in progress (pid {holder}, lock {lock_path}); not starting")
        return EXIT_LOCKED
    seen: list[str] = []

    def emit(line: str) -> None:
        seen.append(line)
        write(line.rstrip("\r\n"))

    try:
        write(f"run_tests: {tier}: pytest {' '.join(args)}")
        code = spawn(args, emit)
    finally:
        release_lock(lock_path)
    summary = find_summary(seen)
    write(summary if summary is not None else f"run_tests: pytest printed no summary line (exit code {code})")
    return code


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scripts.run_tests",
        description="Run the backend tests by tier (fast, gate, full). Unknown arguments go to pytest.",
        allow_abbrev=False,
    )
    parser.add_argument("tier", choices=TIERS)
    parser.add_argument(
        "--touched", action="store_true",
        help="gate only: also run the test files of the modules changed in the working tree",
    )
    options, pytest_args = parser.parse_known_args(argv)
    if options.touched and options.tier != "gate":
        parser.error("--touched only applies to the gate tier")
    return run(options.tier, pytest_args, touched=options.touched)


if __name__ == "__main__":
    raise SystemExit(main())
