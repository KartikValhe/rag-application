"""
Step 5c-1: model-quality checks that need the REAL multilingual-e5-small files. Marked `real_model` and skipped
unless RUN_REAL_MODEL=1 (the skip reason is printed by pytest, see tests/conftest.py). Nothing here downloads:
the files must already be in EMBEDDING_MODEL_DIR (python -m app.scripts.fetch_model e5-small).
"""

from __future__ import annotations

import math

import pytest

pytestmark = pytest.mark.real_model


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@pytest.fixture(scope="module")
def real_embedder():
    from app.core.config import settings
    from app.ml.loader import get_embedder

    if not settings.EMBEDDING_MODEL_DIR:
        pytest.fail("EMBEDDING_MODEL_DIR is not set; see README, 'Embedding model'")
    return get_embedder()


def test_real_vectors_have_the_locked_dimension_and_unit_length(real_embedder) -> None:
    v = real_embedder.embed_query("What is photosynthesis?")
    assert len(v) == real_embedder.dim == 384
    assert math.isclose(math.sqrt(sum(x * x for x in v)), 1.0, abs_tol=1e-4)


@pytest.mark.parametrize(
    "query,relevant,irrelevant",
    [
        ("What is the capital of India?", "New Delhi is the capital of India.",
         "Plants make food using sunlight."),
        ("भारत की राजधानी क्या है?", "भारत की राजधानी नई दिल्ली है।", "पौधे सूर्य के प्रकाश से भोजन बनाते हैं।"),
        ("महाराष्ट्राची राजधानी कोणती आहे?", "महाराष्ट्राची राजधानी मुंबई आहे.",
         "झाडे सूर्यप्रकाश वापरून अन्न तयार करतात."),
    ],
)
def test_a_relevant_passage_scores_higher_than_an_irrelevant_one(real_embedder, query, relevant, irrelevant) -> None:
    q = real_embedder.embed_query(query)
    rel, irr = real_embedder.embed_passages([relevant, irrelevant])
    assert _cos(q, rel) > _cos(q, irr) + 0.02


def test_long_text_is_truncated_and_reported_with_the_real_tokenizer(real_embedder) -> None:
    before = real_embedder.truncated_count
    real_embedder.embed_passages(["word " * 2000])
    assert real_embedder.truncated_count == before + 1
