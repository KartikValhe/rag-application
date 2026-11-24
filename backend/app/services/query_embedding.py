"""
app/services/query_embedding.py
───────────────────────────────
The query embedder that every search entry point shares (test search today, the chat later): how to get the
process-wide model, the guard that turns every failure into a stable error, and the start-up warm-up.

  process_query_embedder()    the process-wide model as a QueryEmbedder, loaded on first use (seconds; B96)
  guarded_query_embedder()    loads it through a loader and turns any failure (loading or embedding) into
                              SearchUnavailable("search_unavailable"); only the exception class is logged
  warm_up_embedder()          loads the model once at start-up (EMBEDDING_WARM_AT_STARTUP); never raises

The cap on queries embedded at once ("search_busy") stays in retrieval.embed_query: it is applied to the
embedder this module returns. Routes map SearchUnavailable to their own 503 response.

Importing this module never loads a machine-learning library: app.ml.loader is imported inside the functions.
"""

from collections.abc import Awaitable, Callable

import structlog

from app.services import retrieval

logger = structlog.get_logger(__name__)

# HOW to get the query embedder, not the embedder itself, so a route can load it only after its permission,
# scope, rate-limit and query checks. Tests replace it.
QueryEmbedderLoader = Callable[[], Awaitable[retrieval.QueryEmbedder]]


async def process_query_embedder() -> retrieval.QueryEmbedder:
    """The process-wide model, loaded on the first search of the process (or at start-up, see warm_up_embedder)."""
    from app.ml import (
        loader,  # imported here: importing the API never loads a machine-learning library
    )

    return retrieval.QueryEmbedder.from_embedder(await loader.get_embedder_async())


async def guarded_query_embedder(
    load: QueryEmbedderLoader, *, log_event: str = "query_embedder_unavailable"
) -> retrieval.QueryEmbedder:
    """The loaded embedder, with every failure (loading or embedding) turned into
    SearchUnavailable("search_unavailable"). Only the exception class is logged under `log_event`: never a
    path, a library message or the query."""
    try:
        loaded = await load()
    except Exception as exc:
        logger.error(log_event, stage="load", error=type(exc).__name__)
        raise retrieval.SearchUnavailable("search_unavailable") from None

    async def embed(query: str) -> list[float]:
        try:
            return await loaded.embed(query)
        except Exception as exc:
            logger.error(log_event, stage="embed", error=type(exc).__name__)
            raise retrieval.SearchUnavailable("search_unavailable") from None

    return retrieval.QueryEmbedder(model_key=loaded.model_key, dim=loaded.dim, embed=embed)


async def warm_up_embedder() -> bool:
    """Load the process-wide model now (the app lifespan calls this when EMBEDDING_WARM_AT_STARTUP is on), so
    the first search after a restart does not pay the load. Holds no database connection. A failure is
    logged (the exception class only) and returned as False, never raised: the app starts anyway and the
    first search retries the load (the loader does not cache a failure)."""
    from app.ml import loader

    try:
        await loader.get_embedder_async()
    except Exception as exc:
        logger.error("embedder_warm_up_failed", error=type(exc).__name__)
        return False
    logger.info("embedder_warm_up_done")
    return True
