# ADR 002: Vector Store Selection

## Status
Accepted

## Context
The platform stores and retrieves semantic embeddings for educational syllabus materials, documents, and student queries.
We evaluated:
1. **pgvector inside PostgreSQL**: Co-located in PostgreSQL with HNSW / IVFFlat indexing and full-text search (hybrid retrieval).
2. **Dedicated vector database (Qdrant / Milvus / Weaviate)**: External vector engines built specifically for filtered vector search.
3. **Managed cloud vector service (Pinecone)**: Hosted SaaS vector database.

## Decision
We chose **Option 1: pgvector inside PostgreSQL** with HNSW indexing and hybrid full-text search (`tsvector` + reciprocal rank fusion).

### Rationale:
1. **Unified Datastore**: Vectors, chunks, metadata, permissions, and audit logs live in the same transactionally consistent database.
2. **Unified RLS**: Tenant isolation via Postgres RLS seamlessly scopes vector search without maintaining secondary tenant authorization lists across external services.
3. **Transactional Deletions**: Deleting a syllabus document transactionally removes chunks and vectors in a single commit.
4. **Target Corpus Scale**: Educational syllabus corpora per class/subject are relatively modest (tens to hundreds of pages per course), so selective filtering by `(tenant_id, class_id, subject_id)` creates very compact, rapid search spaces.

### Abstraction Boundary:
All retrieval logic is encapsulated behind a `Retriever` interface. If p95 latency exceeds 300ms or vector scale exceeds 10M records, a dedicated vector engine can be swapped in without modifying upstream application code.

