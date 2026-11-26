"""
tests/test_chat_source_scans.py
───────────────────────────────
The two source scans of plan 9.2 (moved out of 6a-8, see its build record; built with the first chat API code, 6a-9a):

  1. The chat maintenance connection lives in the worker only (review 6-0c). The setting CHAT_MAINT_DATABASE_URL, its
     attribute name and the module `chat_maintenance` may be named in app/ only by the files on ALLOWED_MAINT_FILES.
     Nothing under app/api/, app/main.py or any other service may name them, however it is imported.
  2. The retired maintenance bypass setting `app.chat_maintenance` (review 6-0b) appears nowhere in app/, alembic/ or
     infra/: not in set_config, not in current_setting, not in a policy.

Pure text scans (fast tier). The scanners are also run on a small fake tree so a broken scanner cannot pass by
finding nothing.
"""

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent

# Plan 9.2: chat_maintenance.py, the setting itself, the test-DB guard and the worker (sweeper and its wiring).
ALLOWED_MAINT_FILES = frozenset({
    "app/services/chat_maintenance.py",
    "app/core/config.py",
    "app/core/test_db_guard.py",
    "app/workers/sweeper.py",
})
# Never allowed, whatever ALLOWED_MAINT_FILES says (review 6-0c: the API process never reads the URL).
NEVER_MAINT_PREFIXES = ("app/api/", "app/main.py")

MAINT_NAME = re.compile(r"CHAT_MAINT_DATABASE_URL|chat_maint_database_url|chat_maintenance", re.IGNORECASE)
# The retired Postgres setting: as a dotted GUC name, or quoted inside set_config / current_setting.
RETIRED_SETTING = re.compile(
    r"app\.chat_maintenance|(?:set_config|current_setting)\s*\(\s*['\"][^'\"]*chat_maintenance", re.IGNORECASE
)
TEXT_SUFFIXES = (".py", ".sql", ".sh", ".toml", ".ini", ".cfg", ".yml", ".yaml", ".mako")


def _files(root: Path, sub: str, suffixes: tuple[str, ...]) -> list[Path]:
    base = root / sub
    if base.is_file():
        return [base]
    return sorted(p for p in base.rglob("*") if p.is_file() and p.suffix in suffixes and "__pycache__" not in p.parts)


def maint_name_violations(backend: Path) -> list[str]:
    """Files under app/ that name the maintenance URL or module and are not allowed to."""
    bad = []
    for path in _files(backend, "app", (".py",)):
        rel = path.relative_to(backend).as_posix()
        if not MAINT_NAME.search(path.read_text(encoding="utf-8")):
            continue
        if rel.startswith(NEVER_MAINT_PREFIXES) or rel not in ALLOWED_MAINT_FILES:
            bad.append(rel)
    return bad


def retired_setting_violations(repo: Path) -> list[str]:
    """Files in app/, alembic/ (backend) and infra/ (repo) that name the retired setting."""
    bad = []
    roots = [(repo / "backend", "app"), (repo / "backend", "alembic"), (repo, "infra")]
    for root, sub in roots:
        if not (root / sub).exists():
            continue
        for path in _files(root, sub, TEXT_SUFFIXES):
            if RETIRED_SETTING.search(path.read_text(encoding="utf-8", errors="replace")):
                bad.append(path.relative_to(repo).as_posix())
    return bad


# ── The real tree ─────────────────────────────────────────────────────────────


def test_only_the_worker_side_names_the_chat_maintenance_url_or_module() -> None:
    assert maint_name_violations(BACKEND) == []


def test_the_retired_chat_maintenance_setting_is_nowhere() -> None:
    assert retired_setting_violations(REPO) == []


def test_the_scans_really_read_the_tree() -> None:
    """Guard: a scan over an empty or wrong folder would pass by finding nothing."""
    app_files = _files(BACKEND, "app", (".py",))
    assert len(app_files) > 100, len(app_files)
    rels = {p.relative_to(BACKEND).as_posix() for p in app_files}
    assert {"app/api/v1/chat.py", "app/core/tenant_db.py", "app/main.py"} <= rels
    assert (BACKEND / "alembic" / "versions" / "0013_chat.py").is_file()
    assert (REPO / "infra" / "postgres" / "init.sql").is_file()


# ── The scanners on a fake tree ───────────────────────────────────────────────


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


@pytest.mark.parametrize(
    ("rel", "body"),
    [
        ("app/api/v1/chat.py", "from app.services import chat_maintenance\n"),
        ("app/api/v1/chat.py", "import app.services.chat_maintenance as m\n"),
        ("app/api/v1/chat.py", "url = settings.CHAT_MAINT_DATABASE_URL\n"),
        ("app/main.py", "x = settings.chat_maint_database_url\n"),
        ("app/services/rag_answer.py", "importlib.import_module('app.services.chat_maintenance')\n"),
        ("app/api/v1/documents.py", "from app.services.chat_maintenance import blank\n"),
        ("app/workers/db.py", "from app.services.chat_maintenance import engine\n"),
    ],
)
def test_the_maint_scan_catches_every_form(tmp_path: Path, rel: str, body: str) -> None:
    _write(tmp_path, "app/core/config.py", "CHAT_MAINT_DATABASE_URL = None\n")  # allowed
    _write(tmp_path, "app/services/chat_maintenance.py", "settings.chat_maint_database_url\n")  # allowed
    _write(tmp_path, rel, body)
    assert maint_name_violations(tmp_path) == [rel]


def test_the_maint_scan_allows_the_planned_files_only(tmp_path: Path) -> None:
    for rel in ALLOWED_MAINT_FILES:
        _write(tmp_path, rel, "CHAT_MAINT_DATABASE_URL chat_maintenance\n")
    _write(tmp_path, "app/api/v1/chat.py", "nothing here\n")
    assert maint_name_violations(tmp_path) == []
    assert not any(rel.startswith(NEVER_MAINT_PREFIXES) for rel in ALLOWED_MAINT_FILES)


@pytest.mark.parametrize(
    ("rel", "body"),
    [
        ("backend/app/core/tenant_db.py", "text(\"SELECT set_config('app.chat_maintenance', 'on', true)\")\n"),
        ("backend/app/core/tenant_db.py", "SET LOCAL app.chat_maintenance = 'on'\n"),
        ("backend/alembic/versions/0014_x.py", "USING (current_setting('app.chat_maintenance', true) = 'on')\n"),
        ("backend/alembic/versions/0014_x.py", 'current_setting( "app.chat_maintenance" )\n'),
        ("infra/postgres/init.sql", "ALTER ROLE app_tenant SET app.chat_maintenance = 'off';\n"),
    ],
)
def test_the_retired_setting_scan_catches_every_form(tmp_path: Path, rel: str, body: str) -> None:
    _write(tmp_path, "backend/app/x.py", "chat_maintenance module name alone is fine here\n")
    _write(tmp_path, rel, body)
    assert retired_setting_violations(tmp_path) == [rel]
