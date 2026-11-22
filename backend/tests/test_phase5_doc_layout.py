"""Layout of section 11 of docs/api/phase5.md.

The frontend drift guard finds sections by their number (11.1, 11.3, 11.4), so the numbering must stay
consecutive and each table must sit under the heading that names it. Plain-text checks only; no database.
"""

from __future__ import annotations

import re
from pathlib import Path

DOC = Path(__file__).resolve().parents[2] / "docs" / "api" / "phase5.md"
HEADING = re.compile(r"^(#{1,6}) (.*)$")


def _lines() -> list[str]:
    return DOC.read_text(encoding="utf-8").splitlines()


def _section_headings(prefix: str) -> list[tuple[int, str]]:
    """(line index, heading text) of every `### <prefix>.<n> ...` heading, in file order."""
    found = []
    for i, line in enumerate(_lines()):
        m = HEADING.match(line)
        if m and m.group(1) == "###" and re.match(rf"{re.escape(prefix)}\.\d+ ", m.group(2)):
            found.append((i, m.group(2)))
    return found


def _section_body(number: str) -> list[str]:
    """Lines of the `###` section whose heading starts with `number`, up to the next heading of level 1-3."""
    lines = _lines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith(f"### {number} "))
    end = len(lines)
    for j in range(start + 1, len(lines)):
        m = HEADING.match(lines[j])
        if m and len(m.group(1)) <= 3:
            end = j
            break
    return lines[start + 1:end]


def _tables(body: list[str]) -> list[list[str]]:
    """Tables in a block of lines: runs of consecutive lines that start with a pipe."""
    tables: list[list[str]] = []
    run: list[str] = []
    for line in body:
        if line.startswith("|"):
            run.append(line)
        elif run:
            tables.append(run)
            run = []
    if run:
        tables.append(run)
    return tables


def test_section_11_headings_are_consecutive() -> None:
    headings = _section_headings("11")
    numbers = [int(text.split(" ", 1)[0].split(".")[1]) for _, text in headings]
    assert numbers == list(range(1, len(numbers) + 1)), headings
    assert len(numbers) >= 5, headings


def test_test_search_error_table_has_its_own_error_codes_section() -> None:
    lines = _lines()
    row = next(i for i, ln in enumerate(lines) if ln.startswith("| `invalid_query`"))
    title = next(
        lines[j] for j in range(row, -1, -1) if HEADING.match(lines[j]) and lines[j].startswith("### ")
    )
    assert "error codes" in title.lower(), title
    assert "injection" not in title.lower(), title


def test_injection_codes_section_has_exactly_one_table() -> None:
    body = _section_body("11.3")
    assert len(_tables(body)) == 1, [t[0] for t in _tables(body)]
    assert any("`role_play`" in ln for ln in body)
    assert not any("`invalid_query`" in ln for ln in body)
