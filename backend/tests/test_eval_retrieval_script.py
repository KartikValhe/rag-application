"""
tests/test_eval_retrieval_script.py
───────────────────────────────────
python -m app.scripts.eval_retrieval (step 5d-3): the parts that need no database and no model.

  * a missing golden file, a missing model directory: "skipped: <reason>" and exit code 0 (the harness runs
    where the owner's files are; CI has none of them);
  * a malformed golden CSV: a clear error with the row number, and a non-zero exit code;
  * the operator scope (RetrievalScope.for_operator) is used only from app/scripts.

The database parts (documents missing, READ ONLY, the end-to-end numbers) are in test_eval_retrieval_db.py.
NEVER run the harness against the dev database from a test: these tests use fake paths and settings only.
"""

import ast
import uuid
from pathlib import Path

import pytest

from app.core.config import settings
from app.scripts import eval_retrieval

APP_DIR = Path(eval_retrieval.__file__).resolve().parents[1]
HEADER = "#,Language,Question,Document,Expected location,Should answer\n"
GOOD_ROW = "1,English,What is osmosis?,Biology Notes,page 1,Yes\n"


def argv(tmp_path: Path, golden: Path | None = None, *extra: str) -> list[str]:
    return [
        "--tenant", "nosuchtenant", "--class-id", str(uuid.uuid4()), "--subject-id", str(uuid.uuid4()),
        "--golden", str(golden if golden is not None else tmp_path / "golden.csv"),
        "--report", str(tmp_path / "out" / "report.json"), *extra,
    ]


def write_golden(tmp_path: Path, body: str = GOOD_ROW) -> Path:
    path = tmp_path / "golden.csv"
    path.write_text("\ufeff" + HEADER + body, encoding="utf-8")
    return path


async def test_a_missing_golden_file_is_skipped_with_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert await eval_retrieval.run(argv(tmp_path)) == 0
    out = capsys.readouterr().out
    assert out.strip().startswith("skipped:") and "golden" in out
    assert not (tmp_path / "out").exists(), "a skipped run writes no report"


async def test_the_default_golden_path_is_the_fixture_path(tmp_path: Path) -> None:
    args = eval_retrieval.build_parser().parse_args(
        ["--tenant", "t", "--class-id", str(uuid.uuid4()), "--subject-id", str(uuid.uuid4()), "--report", "r.json"]
    )
    assert args.golden == "tests/fixtures/golden_set.csv" and args.k == 10


async def test_a_malformed_golden_file_is_an_error_with_the_row_number_not_a_skip(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_golden(tmp_path, GOOD_ROW + "2,English,Q two?,Biology Notes,sometime,Yes\n")
    assert await eval_retrieval.run(argv(tmp_path, path)) == 2
    captured = capsys.readouterr()
    assert "row 3" in captured.err and "Expected location" in captured.err
    assert "skipped" not in captured.out
    assert not (tmp_path / "out").exists()


async def test_a_golden_file_with_a_missing_column_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "golden.csv"
    path.write_text("#,Language,Question\n1,English,Q?\n", encoding="utf-8")
    assert await eval_retrieval.run(argv(tmp_path, path)) == 2
    assert "Document" in capsys.readouterr().err


async def test_a_missing_model_directory_is_skipped_with_exit_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_golden(tmp_path)
    for value, needle in (("", "EMBEDDING_MODEL_DIR is not set"), (str(tmp_path / "nowhere"), "not a directory"),
                          (str(tmp_path), "model files")):  # the directory exists but holds no e5-small folder
        monkeypatch.setattr(settings, "EMBEDDING_MODEL_DIR", value)
        assert await eval_retrieval.run(argv(tmp_path, path)) == 0, value
        out = capsys.readouterr().out
        assert out.strip().startswith("skipped:") and needle in out, (value, out)
    assert not (tmp_path / "out").exists()


async def test_the_golden_file_is_checked_before_the_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "EMBEDDING_MODEL_DIR", "")
    assert await eval_retrieval.run(argv(tmp_path)) == 0
    assert "golden" in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad",
    [["--k", "0"], ["--k", "51"], ["--class-id", "not-a-uuid"], ["--subject-id", "1234"]],
)
async def test_bad_arguments_are_refused_before_anything_runs(tmp_path: Path, bad: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        await eval_retrieval.run([*argv(tmp_path, write_golden(tmp_path)), *bad])
    assert exc.value.code == 2


async def test_the_report_path_is_required(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc:
        await eval_retrieval.run(["--tenant", "t", "--class-id", str(uuid.uuid4()), "--subject-id", str(uuid.uuid4())])
    assert exc.value.code == 2


def test_only_the_scripts_use_the_operator_scope_and_this_script_does() -> None:
    """The 5d-1b guard (tests/test_retrieval_pure.py) forbids for_operator outside app/scripts. Here: the
    harness really is a script that uses it, and the pure module and the services never do."""
    users = []
    for path in APP_DIR.rglob("*.py"):
        rel = path.relative_to(APP_DIR).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Attribute) and node.attr == "for_operator") or (
                isinstance(node, ast.Name) and node.id == "for_operator"
            ):
                users.append(rel)
                break
    assert "scripts/eval_retrieval.py" in users
    assert all(u.startswith("scripts/") or u == "services/retrieval.py" for u in users), users
    assert not (APP_DIR / "services" / "retrieval_eval.py").read_text(encoding="utf-8").count("sqlalchemy"), (
        "the metric module is pure: no database code"
    )


def test_the_script_never_sets_a_threshold() -> None:
    """The threshold is chosen by the owner from the sweep: the script only reads the configured value."""
    tree = ast.parse((APP_DIR / "scripts" / "eval_retrieval.py").read_text(encoding="utf-8"))
    assert "RETRIEVAL_MIN_SIMILARITY" not in {
        t.attr for n in ast.walk(tree) if isinstance(n, ast.Assign) for t in n.targets if isinstance(t, ast.Attribute)
    }
    keywords = {kw.arg for n in ast.walk(tree) if isinstance(n, ast.Call) for kw in n.keywords}
    assert "min_similarity" not in keywords, "no RetrievalConfig(min_similarity=...) in the script"
