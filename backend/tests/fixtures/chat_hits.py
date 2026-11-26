"""
Hand-built retrieval hits for the pure chat tests (6a-10a). Every text field is a canary the tests can search for, so
a test can prove that ids, file names and scores never reach a prompt.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.services.retrieval import Hit


def make_hit(rank: int = 1, **overrides: Any) -> Hit:
    values: dict[str, Any] = {
        "rank": rank,
        "chunk_id": uuid.uuid4(),
        "document_id": uuid.uuid4(),
        "document_title": f"Intro to React {rank}",
        "version_id": uuid.uuid4(),
        "version_no": 3,
        "version_status": "published",
        "original_filename": f"canary-file-name-{rank}.pdf",
        "page_start": 5,
        "page_end": 5,
        "heading_path": "Components > Props",
        "content": f"Props pass data from a parent component to a child component. Chunk {rank}.",
        "vector_rank": rank,
        "vector_similarity": 0.8731 - rank / 1000,
        "keyword_rank": None,
        "keyword_matched_terms": None,
        "fused_score": 0.0163934 - rank / 100000,
        "injection_flags": (),
        "file_kind": "pdf",
    }
    values.update(overrides)
    return Hit(**values)
