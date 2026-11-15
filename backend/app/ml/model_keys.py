"""
app/ml/model_keys.py
────────────────────
The ONE place that defines the embedding model keys stored in
`chunk_embeddings.embedding_model`.

A key is the Hugging Face repo plus the pinned commit (design section 0d).
Migration 0008 uses `E5_SMALL_MODEL_KEY` in the predicate of the partial HNSW index,
and the retriever and embedder import the same constant, so the index and the queries
can never drift apart. Changing the revision means a new index in a new migration and
a re-index, never an edit of this value in place.
"""

E5_SMALL_REPO = "intfloat/multilingual-e5-small"
E5_SMALL_REVISION = "614241f622f53c4eeff9890bdc4f31cfecc418b3"
E5_SMALL_MODEL_KEY = f"{E5_SMALL_REPO}@{E5_SMALL_REVISION}"
E5_SMALL_DIM = 384
