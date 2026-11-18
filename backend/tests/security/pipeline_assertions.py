"""
tests/security/pipeline_assertions.py
─────────────────────────────────────
Shared assertions for the chunk / embed / index pipeline (step 5c-2, design C2 "prove-it-fails"). Each protection
is checked by a helper, so tests/security/test_prove_it_fails_pipeline.py can switch the protection off and show
that the SAME helper then raises.
"""

import contextlib
import math
from collections.abc import Awaitable, Callable, Sequence

from app.services.chunker import PASSAGE_RESERVED_TOKENS, ChunkingConfig, TextChunk
from app.workers import errors


def assert_chunks_within_model_limit(
    chunks: Sequence[TextChunk], count_tokens: Callable[[str], int], config: ChunkingConfig | None = None
) -> None:
    """Every chunk's EMBEDDED text (heading, newline, content) plus the prefix and special tokens fits the model."""
    cfg = config or ChunkingConfig()
    assert chunks, "no chunks to check"
    for c in chunks:
        embedded = count_tokens(c.embedding_text) + PASSAGE_RESERVED_TOKENS
        assert embedded <= cfg.model_max_tokens, (
            f"chunk {c.ordinal} needs {embedded} tokens with the prefix, the model takes {cfg.model_max_tokens}: it would be truncated"
        )
        assert c.token_count <= cfg.max_tokens, f"chunk {c.ordinal} has {c.token_count} content tokens, the maximum is {cfg.max_tokens}"


async def assert_index_write_requires_ownership(
    attempt: Callable[[], Awaitable[None]], count_chunks: Callable[[], Awaitable[int]]
) -> None:
    """A worker that no longer owns the job runs the index stage: it must be stopped and write nothing."""
    with contextlib.suppress(errors.JobStopped):
        await attempt()
    written = await count_chunks()
    assert written == 0, f"a worker that no longer owned the job wrote {written} chunks"


def assert_quota_breach_writes_nothing(outcome: object, expected_failure: object, version_status: str, chunks: int) -> None:
    """A version that would take the tenant over its pages quota must end failed with no chunks."""
    assert outcome == expected_failure, f"the job ended {outcome}, expected {expected_failure}: the quota re-check did not run"
    assert version_status == "failed", f"the version ended {version_status}, expected failed"
    assert chunks == 0, f"{chunks} chunks were written for a version that is over the pages quota"


def assert_reuse_is_tenant_scoped(
    canary: Sequence[float], stored_vectors: Sequence[Sequence[float]], reused: int
) -> None:
    """Another tenant's vectors (the canary) must never end up on this tenant's chunks."""
    assert reused == 0, f"{reused} vectors were reused from another tenant"
    for vector in stored_vectors:
        distance = math.sqrt(sum((a - b) ** 2 for a, b in zip(vector, canary, strict=True)))
        assert distance > 0.01, "a chunk carries another tenant's canary vector"


def assert_statement_count_is_constant(small: int, large: int, *, limit: int = 25, growth: int = 4) -> None:
    """The index transaction runs a small constant number of statements however many chunks there are."""
    assert large < limit, f"the index transaction ran {large} statements for the large document (limit {limit})"
    assert large - small <= growth, f"statements grow with the chunks: {small} for the small document, {large} for the large one"


