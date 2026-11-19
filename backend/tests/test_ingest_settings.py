"""
Settings of step 5c-2: chunking, embedding batch size, publish and archive, and the ingest timing that has to
carry a 1,000-chunk upload (about five minutes of embedding on the measured laptop) without a stolen lease.
"""

from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.chunker import ChunkingConfig

NEW_SETTINGS = (
    "CHUNK_TARGET_TOKENS",
    "CHUNK_MAX_TOKENS",
    "CHUNK_OVERLAP_TOKENS",
    "DOC_AUTO_PUBLISH_ENABLED",
    "DOC_ARCHIVE_FILE_RETENTION_DAYS",
    "DOC_MAX_ARCHIVED_VERSIONS",
    "EMBEDDING_BATCH_SIZE",
    "INGEST_LEASE_SECONDS",
    "INGEST_HEARTBEAT_SECONDS",
    "INGEST_MAX_JOB_SECONDS",
)


def fresh(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    """Neither the shell environment nor a .env file may change what these tests see."""
    for name in NEW_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    s = fresh(monkeypatch)
    assert (s.CHUNK_TARGET_TOKENS, s.CHUNK_MAX_TOKENS, s.CHUNK_OVERLAP_TOKENS) == (380, 460, 60)
    assert s.EMBEDDING_BATCH_SIZE == 8  # measured: bigger batches are not faster and cost memory
    assert s.DOC_AUTO_PUBLISH_ENABLED is False  # off by default (plan Q5; the scanner is a tripwire)
    assert s.DOC_ARCHIVE_FILE_RETENTION_DAYS == 30
    assert s.DOC_MAX_ARCHIVED_VERSIONS == 5


def test_the_chunk_settings_make_a_valid_chunking_config(monkeypatch: pytest.MonkeyPatch) -> None:
    s = fresh(monkeypatch)
    assert ChunkingConfig(s.CHUNK_TARGET_TOKENS, s.CHUNK_MAX_TOKENS, s.CHUNK_OVERLAP_TOKENS) == ChunkingConfig()


@pytest.mark.parametrize(
    "bad",
    [
        {"CHUNK_OVERLAP_TOKENS": 380},
        {"CHUNK_TARGET_TOKENS": 470},
        {"CHUNK_MAX_TOKENS": 600},
        {"INGEST_LEASE_SECONDS": 100, "INGEST_HEARTBEAT_SECONDS": 30},  # fewer than four renewals per lease
        {"INGEST_MAX_JOB_SECONDS": 60},  # below the lease
        {"DOC_MAX_ARCHIVED_VERSIONS": -1},
        {"DOC_ARCHIVE_FILE_RETENTION_DAYS": -1},
    ],
)
def test_nonsense_values_are_refused_at_start_up(bad: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError):
        fresh(monkeypatch, **bad)


def test_the_timing_allows_a_1000_chunk_upload_without_a_stolen_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """Measured on the laptop (real e5-small, one thread): 2.7 to 3.5 texts per second, so 1,000 chunks take
    about 5 minutes. The lease is renewed by the heartbeat (and after every batch), never by the stage, so what
    matters is: (1) the longest gap between two renewals fits the lease, (2) the whole job fits the lifetime cap."""
    s = fresh(monkeypatch)
    worst_rate = 2.7
    one_batch_seconds = s.EMBEDDING_BATCH_SIZE / worst_rate
    chunks = 1000
    job_seconds = s.DOC_PARSE_TIMEOUT_SECONDS + chunks / worst_rate + 60  # parse + embed + index and the rest
    assert job_seconds > 300  # the scenario really is a five minute job
    assert s.INGEST_HEARTBEAT_SECONDS + one_batch_seconds < s.INGEST_LEASE_SECONDS / 2  # a missed beat is survivable
    assert job_seconds < s.INGEST_MAX_JOB_SECONDS / 4  # and a job four times as big still fits under the cap
    # the 2,000,000 character ceiling (about 4,500 Hindi chunks) also fits
    assert s.DOC_PARSE_TIMEOUT_SECONDS + 4500 / worst_rate + 120 < s.INGEST_MAX_JOB_SECONDS


def test_the_new_settings_are_documented_with_placeholders_only() -> None:
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    for name in NEW_SETTINGS:
        assert f"\n{name}=" in example, f"{name} is missing from backend/.env.example"
    assert "EMBEDDING_BATCH_SIZE=8" in example
    assert "DOC_AUTO_PUBLISH_ENABLED=false" in example


# ── Test search (step 5d-2) ──────────────────────────────────────────────────

TEST_SEARCH_SETTINGS = ("DOC_TEST_SEARCH_RATE_USER", "DOC_TEST_SEARCH_RATE_WINDOW_SECONDS")


def test_test_search_rate_defaults_bounds_and_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in TEST_SEARCH_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    s = fresh(monkeypatch)
    assert (s.DOC_TEST_SEARCH_RATE_USER, s.DOC_TEST_SEARCH_RATE_WINDOW_SECONDS) == (60, 600)  # design A5
    for bad in ({"DOC_TEST_SEARCH_RATE_USER": 0}, {"DOC_TEST_SEARCH_RATE_WINDOW_SECONDS": 0}):
        with pytest.raises(ValueError):
            fresh(monkeypatch, **bad)
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "\nDOC_TEST_SEARCH_RATE_USER=60\n" in example
    assert "\nDOC_TEST_SEARCH_RATE_WINDOW_SECONDS=600\n" in example


def test_query_embedding_cap_defaults_bounds_and_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review of 5d-2, finding 5: two query embeddings at once per process, a 5 s wait for a place."""
    for name in ("RETRIEVAL_EMBED_CONCURRENCY", "RETRIEVAL_EMBED_QUEUE_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    s = fresh(monkeypatch)
    assert (s.RETRIEVAL_EMBED_CONCURRENCY, s.RETRIEVAL_EMBED_QUEUE_SECONDS) == (2, 5.0)
    for bad in ({"RETRIEVAL_EMBED_CONCURRENCY": 0}, {"RETRIEVAL_EMBED_QUEUE_SECONDS": -1}, {"RETRIEVAL_EMBED_QUEUE_SECONDS": 61}):
        with pytest.raises(ValueError):
            fresh(monkeypatch, **bad)
    example = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "\nRETRIEVAL_EMBED_CONCURRENCY=2\n" in example
    assert "\nRETRIEVAL_EMBED_QUEUE_SECONDS=5\n" in example


def test_auto_publish_stays_off_and_the_comment_says_why() -> None:
    """Review of 5d-2, finding 10: the setting's comment no longer claims the scanner is missing."""
    source = (Path(__file__).resolve().parents[1] / "app" / "core" / "config.py").read_text(encoding="utf-8")
    block = source[source.index("# A ready version without injection flags") : source.index("DOC_AUTO_PUBLISH_ENABLED: bool")]
    assert "OFF until the injection scanner" not in block
    assert "tripwire" in block and "B76" in block
