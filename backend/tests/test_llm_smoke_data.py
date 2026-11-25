"""
Step 6a-6: the sample material of app/scripts/llm_smoke.py (app/scripts/llm_smoke_data.py).

It is made-up teaching text, in English and Marathi: no real student, teacher or institution appears. The owner
reads the Marathi by hand, so these tests only guard its SHAPE (counts, ids, marking, script).
"""

from __future__ import annotations

import re
from collections import Counter

import pytest

from app.scripts import llm_smoke_data as data

LANGS = ("en", "mr")


def _devanagari_share(text: str) -> float:
    # Devanagari characters (letters and vowel signs) over those plus Latin letters; digits, spaces and
    # punctuation do not count either way
    devanagari = sum(1 for c in text if 0x0900 <= ord(c) <= 0x097F and not c.isdigit())
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    return devanagari / (devanagari + latin) if devanagari + latin else 0.0


def test_the_module_says_it_is_sample_data() -> None:
    assert "SAMPLE" in (data.__doc__ or "")
    assert "SAMPLE" in data.SAMPLE_NOTICE


@pytest.mark.parametrize("lang", LANGS)
def test_six_short_passages_per_language(lang: str) -> None:
    passages = data.PASSAGES[lang]
    assert len(passages) == 6
    for p in passages:
        assert p.location.startswith("SAMPLE"), p.title  # every source is marked as sample, also inside the prompt
        assert 40 <= len(p.text) <= 700, p.title
        assert p.title and p.section


@pytest.mark.parametrize("lang", LANGS)
def test_the_near_topic_trap_material_exists(lang: str) -> None:
    joined = " ".join(p.title + p.text for p in data.PASSAGES[lang])
    # framework names stay in Latin letters in the Marathi passages, as in real Marathi course notes
    assert "Angular" in joined and "Redux" in joined and "React" in joined


@pytest.mark.parametrize("lang", LANGS)
def test_twenty_questions_in_the_agreed_mix(lang: str) -> None:
    questions = data.QUESTIONS[lang]
    assert len(questions) >= 20
    kinds = Counter(q.kind for q in questions)
    assert kinds == {"answerable": 12, "near_topic": 4, "unrelated": 4}
    assert len({q.id for q in questions}) == len(questions)
    assert all(re.fullmatch(rf"{lang}-\d{{2}}", q.id) for q in questions)
    for q in questions:
        assert q.expected == ("ANSWERABLE" if q.kind == "answerable" else "NOT_IN_SOURCES")
        assert q.text.strip().endswith("?"), q.id


@pytest.mark.parametrize("lang", LANGS)
def test_a_short_run_still_has_every_kind(lang: str) -> None:
    # --count 10 must not be ten answerable questions: the three kinds are interleaved
    first = Counter(q.kind for q in data.QUESTIONS[lang][:10])
    assert first["near_topic"] >= 1 and first["unrelated"] >= 1 and first["answerable"] >= 5


def test_marathi_is_marathi_and_english_is_english() -> None:
    for p in data.PASSAGES["mr"]:
        assert _devanagari_share(p.text) > 0.5, p.title
    for q in data.QUESTIONS["mr"]:
        assert _devanagari_share(q.text) > 0.3, q.id  # the plan's detector (5.7) uses the same 30% line
    for p in data.PASSAGES["en"]:
        assert _devanagari_share(p.text) == 0.0, p.title
    for q in data.QUESTIONS["en"]:
        assert _devanagari_share(q.text) == 0.0, q.id


@pytest.mark.parametrize("lang", LANGS)
def test_no_source_or_question_looks_like_real_data(lang: str) -> None:
    text = " ".join(p.title + p.text + p.section for p in data.PASSAGES[lang]) + " ".join(q.text for q in data.QUESTIONS[lang])
    assert not re.search(r"@|https?://|\b\d{10}\b", text)


def test_sources_hold_no_instruction_to_the_model_and_no_label_look_alike() -> None:
    # this is a plain smoke set: injection material belongs to the red-team set (6a-13), not here
    for lang in LANGS:
        for p in data.PASSAGES[lang]:
            assert not re.search(r"\[S\d|ANSWERABLE|NOT_IN_SOURCES|<source", p.text + p.title + p.section)
