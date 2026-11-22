# Phase 5d plan: retrieval, test search, injection scanner, retrieval-only evaluation

Status: APPROVED 2026-11-19 · 5d-1 built, review fixes 5d-1b built, 5d-2 built, 5d-2b fixes built, 5d-3 built (section 14); the harness has not been run on real data yet.
Read with: `docs/phase5-design.md` A4-A6 and C2, `docs/backlog.md`, `CLAUDE.md`.
There is no LLM call anywhere in 5d.

## 0. What I checked before planning (facts, not assumptions)

| # | Fact | How it was checked |
|---|---|---|
| F1 | On this image the `simple` parser keeps **whole Devanagari words** (matras, virama, anusvara and conjuncts stay inside the word). `to_tsvector('simple','महाराष्ट्राची राजधानी मुंबई आहे. प्रकाशसंश्लेषण क्रिया पानांमध्ये होते.')` gives `'महाराष्ट्राची' 'राजधानी' 'मुंबई' 'आहे' 'प्रकाशसंश्लेषण' 'क्रिया' 'पानांमध्ये' 'होते'`. The danda `।` separates words. Hindi gives the same result. `ts_debug` classifies each word as one `word` token | Read-only `SELECT`s on `ragplatform_test` (PostgreSQL 16.16, Debian 12, `pgvector/pgvector:0.8.7-pg16-bookworm`). Database locale: `datcollate = datctype = en_US.utf8`, encoding UTF8 |
| F2 | The parser classifies letters with the database's `LC_CTYPE`. CI uses the same image with the same default locale, so it should behave the same, but a test must prove it on every run. **Supabase's `LC_CTYPE` is UNVERIFIED** (often `C.UTF-8`) | Same probe, plus `ci.yml` (no locale overrides) |
| F3 | `websearch_to_tsquery` combines words with **AND**. The natural question `प्रकाशसंश्लेषण म्हणजे काय?` does **not** match the sentence above, because `म्हणजे` and `काय` are not in it. The same applies to "what is photosynthesis" in English. The design's keyword query would therefore miss most real questions | Same probe: `@@` returns `f` |
| F4 | Inflection: the query word `पाने` does not match `पानांमध्ये`. Exact-word full-text search does not handle Marathi suffixes; only the vector side (or trigrams) can | Same probe |
| F5 | `pg_trgm` 1.6 is **available but not installed** in the test DB. `vector` 0.8.7 is installed | `pg_available_extensions` |
| F6 | A code such as `ERR_X9` becomes the lexemes `err` and `x9`; `python3.12` stays one lexeme | Same probe |
| F7 | `clean_text` (the ingest cleaning) already removes zero-width and bidi characters, so they **never reach `chunks.content`**. The scanner rule in design A6 ("zero-width or bidi control characters") can never fire at chunk level | `app/services/document_text.py` |
| F8 | `app_tenant` already has `SELECT, INSERT, UPDATE, DELETE` on `chunks` and `chunk_embeddings` (0008; the index stage writes as `app_tenant`). Retrieval needs SELECT only, so **5d adds no grant**. No exact privilege-inventory test exists for these two tables yet | 0008, `tests/` grep |
| F9 | `tests/fixtures/golden_set.csv` **does not exist** and **no golden-set draft exists in `docs/`** (searched for "Expected location", "Should answer", "golden_set") | Glob + Grep |
| F10 | `tests/fixtures/real_corpus` holds `en-01.pdf`, `hi-01.docx`, `mr-01.pptx`. `en-01.pdf` is **refused at upload** (`pdf_embedded_file`, backlog B80), so it cannot be "ingested and published" unless something changed. B61 says the real e5-small files have **never been downloaded** | `ls`, backlog B61/B80 |
| F11 | No flags route (`GET .../flags`) and no chunk-exclude route exist yet; design C1 lists both under 5d | `api/v1/documents.py` |

## 1. Where the prompt and the design doc disagree (I have not picked silently)

For each conflict below I give my recommendation. Your "go" approves the recommendation unless you say otherwise. The ones I cannot settle myself are repeated in section 12.

| # | Prompt says | Design says | Recommendation |
|---|---|---|---|
| C1 | Retrieval returns only PUBLISHED versions, "never ready-but-unpublished" | A5: test search has `include_drafts=true` (published plus `ready` versions); A2: a flagged version stays `ready` and is "visible to teachers in test search" | The retrieval service is published-only by default. Test search accepts `include_drafts` (default **false**). When it is true, it adds `ready` versions only (never queued, processing, failed or archived), for teachers and admins only, and every hit carries `version_status`. Students and the future chat can never set it (it is not a field of the read scope). **Open question Q1** |
| C2 | Sufficiency threshold is a **setting** with a conservative default | A5: threshold stored per model in `models.lock.json`, start 0.82 (UNVERIFIED); rule = `max_sim >= T OR keyword coverage >= 60%` | Settings (the prompt's rule): `RETRIEVAL_MIN_SIMILARITY` (default **0.85**, marked UNCALIBRATED in the code comment, the API doc and the response) and `RETRIEVAL_MIN_KEYWORD_COVERAGE` (default **off**). F3 shows why: an OR keyword query plus "what is the" words would make the 60% coverage clause pass on unrelated chunks. The harness sweeps both, and you choose from the data. **Q3** |
| C3 | Eval harness `app/scripts/eval_retrieval.py`, CSV golden set, your real corpus, read-only, skipped in CI | A6: `backend/eval/` + `tests/eval/`, our own synthetic corpus `tests/fixtures/rag/`, `golden_v1.json` anchored by text, a CI gate against `eval/baseline_fake.json` (that gate is listed under 5e) | Follow the prompt for 5d-3. The design's CI-gated synthetic eval with a stored baseline stays in **5e** (recorded as a deviation). The synthetic quality tests of section 9 cover CI in 5d |
| C4 | Scanner runs "at the index stage" | A3: step 6, its own stage between chunk and embed (`scanning` is already an allowed `stage` value) | Run the pure scanner in the **index stage, before its transaction opens** (in a thread), and write the flags inside the existing transaction. This adds no new stage, leaves the worker and `IngestStages` unchanged, and keeps the statement count at 14 |
| C5 | Rate-limited "like retry" | A5: 60 per 10 minutes per user | Use the same mechanism as retry (`SlotManager` window, checked after the 404 checks, fail closed with 503 outside development) with the design's numbers: `DOC_TEST_SEARCH_RATE_USER=60` per `DOC_TEST_SEARCH_RATE_WINDOW_SECONDS=600` |
| C6 | Not mentioned | A5 step 6: cache query vectors in Redis for 24 h | Defer to backlog. It is an optimisation that adds a Redis dependency to the search path; e5-small embeds one query in milliseconds once loaded |
| C7 | Not mentioned | C1/A6: flags route and chunk-exclude route in 5d | **Q4** |
| C8 | Keyword side = full text | A5: `websearch_to_tsquery` (AND) | **OR** query over the query's own lexemes (F3). The lexemes come from Postgres (`to_tsvector('simple', :q)`), so query and content are tokenised by the same parser |

## 2. Module layout

| File | New / changed | Content |
|---|---|---|
| `app/services/retrieval.py` | new | `RetrievalScope`, `build_retrieval_scope`, `RetrievalConfig.from_settings`, `normalize_query`, `or_tsquery_text` (pure), `rrf_fuse` (pure), `decide_sufficiency` (pure), `PgHybridRetriever.search`, `Hit`, `RetrievalResult` |
| `app/services/injection_scan.py` | new | Rule table, `scan_text(text) -> tuple[str, ...]`, `scan_chunks(chunks) -> list[tuple[str, ...]]` (pure, bounded regexes) |
| `app/services/retrieval_eval.py` | new | Pure metric code (golden-set loader, location matcher, hit@k, MRR, refusal accuracy, threshold sweep), so the unit tests need no database |
| `app/scripts/eval_retrieval.py` | new | Read-only CLI around the two modules above |
| `app/schemas/document.py` | changed | `TestSearchRequest`, `TestSearchResponse`, `TestSearchHit` (`extra="forbid"`, every key always present) |
| `app/api/v1/documents.py` | changed | `POST /documents/test-search`; a `get_query_embedder` dependency (tests override it with the FakeEmbedder or a scripted embedder) |
| `app/services/document_upload.py` | changed | `check_test_search_rate(user_id)`, the same pattern as `check_retry_rate` |
| `app/workers/pipeline.py` | changed | Index stage: scan, then write `injection_flags` per chunk and `flagged_chunk_count` |
| `app/core/config.py`, `backend/.env.example` | changed | `RETRIEVAL_*`, `DOC_TEST_SEARCH_*` (placeholders only; no `.env` read) |
| docs | changed | `docs/api/phase5.md` (test search, flags behaviour, codes), `docs/phase5-design.md` (5d records), `docs/backlog.md` |

No migration is planned (section 4).

## 3. Exact SQL shape

All statements run in **one** `tenant_session_context(tenant_id)` transaction as `app_tenant` (RLS on), with the tenant id taken from the authenticated user. Every value is a bind parameter. The only text formatted into SQL is `{schema}`, the pgvector schema read from `pg_extension`, which is the existing pattern. On the HNSW path `{dim}` is also formatted in: an `int` from the embedder (checked `isinstance(dim, int)` and range 1..2000), never from a request.

**Statement 1, the query's lexemes** (same parser as the content):
```sql
SELECT tsvector_to_array(to_tsvector('simple', :q))
```
Python builds the OR query text `'lex1' | 'lex2' | ...`, escaping `\` and `'` inside each lexeme, at most 32 lexemes. The text goes to Postgres only as the bind parameter `:tsq`. If there are no lexemes, the keyword side is skipped.

**Statement 2, candidate count** (picks the exact or the HNSW path):
```sql
WITH scope(class_id, subject_id) AS (
  SELECT * FROM unnest(CAST(:class_ids AS uuid[]), CAST(:subject_ids AS uuid[]))
), docs AS MATERIALIZED (
  SELECT DISTINCT d.id AS document_id
  FROM app.documents d
  JOIN app.document_classes dc ON dc.tenant_id = d.tenant_id AND dc.document_id = d.id
  JOIN scope s ON s.class_id = dc.class_id AND s.subject_id = dc.subject_id
  WHERE d.tenant_id = :t AND dc.tenant_id = :t
    AND d.deleted_at IS NULL
    AND dc.subject_id = d.subject_id                -- PAIR match: the document's own subject
), cand AS MATERIALIZED (
  SELECT c.id
  FROM app.chunks c
  JOIN docs ON docs.document_id = c.document_id
  JOIN app.document_versions v ON v.tenant_id = c.tenant_id AND v.id = c.document_version_id
  WHERE c.tenant_id = :t AND v.tenant_id = :t
    AND v.status = ANY(CAST(:statuses AS text[]))   -- ['published'] or ['published','ready']
    AND NOT c.excluded
)
SELECT count(*) FROM cand
```

**Statement 3, the search, exact path** (count ≤ `RETRIEVAL_EXACT_MAX_CANDIDATES`, default 50,000). It reuses the same CTEs and returns both ranked lists in one round trip:
```sql
..., vec AS (
  SELECT e.chunk_id AS id,
         row_number() OVER (ORDER BY e.embedding OPERATOR({schema}.<=>) CAST(:qv AS {schema}.vector), e.chunk_id) AS rnk,
         1 - (e.embedding OPERATOR({schema}.<=>) CAST(:qv AS {schema}.vector)) AS sim
  FROM cand JOIN app.chunk_embeddings e ON e.tenant_id = :t AND e.chunk_id = cand.id
  WHERE e.embedding_model = :model AND e.embedding_dim = :dim
  ORDER BY rnk LIMIT :vk                              -- RETRIEVAL_VECTOR_K = 20
), kw AS (
  SELECT c.id,
         row_number() OVER (ORDER BY ts_rank_cd(c.content_tsv, q.q, 1) DESC, c.id) AS rnk,
         cardinality(ARRAY(SELECT unnest(tsvector_to_array(c.content_tsv))
                           INTERSECT SELECT unnest(CAST(:lexemes AS text[])))) AS matched
  FROM cand JOIN app.chunks c ON c.tenant_id = :t AND c.id = cand.id,
       to_tsquery('simple', :tsq) AS q(q)
  WHERE c.content_tsv @@ q.q
  ORDER BY rnk LIMIT :kk                              -- RETRIEVAL_KEYWORD_K = 20
)
SELECT 'v' AS side, id, rnk, sim, NULL::int AS matched FROM vec
UNION ALL
SELECT 'k', id, rnk, NULL, matched FROM kw
```

**HNSW path** (count > cap): run `SET LOCAL hnsw.iterative_scan = relaxed_order` and `SET LOCAL hnsw.ef_search = :ef` (`RETRIEVAL_HNSW_EF_SEARCH`, default 100) first. The vector part then orders `e.embedding::{schema}.vector({dim}) <=> :qv` with `e.embedding_model = :model` (the exact expression and predicate of the partial index `ix_chunk_embeddings_hnsw_e5_small`) and filters `EXISTS (SELECT 1 FROM cand WHERE cand.id = e.chunk_id)`. An outer query sorts again, because `relaxed_order` may return rows slightly out of order. The keyword part is the same as on the exact path. A test lowers the cap to 0 to run this path; it checks the results, not the plan (EXPLAIN on tiny tables is not reliable). The index use is checked by hand once and recorded.

**RRF, in Python** (pure, unit-tested): `score = Σ 1 / (RRF_K + rank)` over the sides where the chunk appears, `RRF_K = 60`. Ties are broken by best single rank, then chunk id (deterministic). Keep the top `RETRIEVAL_FINAL_K` (6; test search takes `k` ≤ 10).

**Statement 4, hit details** for the final ids only (title, version id/no/status, pages, heading, content, `injection_flags`). It filters by the same tenant and by `id = ANY(:ids)`, and repeats the status and `excluded` filters as a second guard.

## 4. Index choices and the Devanagari decision

- **Vector:** exact search over the pre-filtered candidates (design A5). The existing partial HNSW index (m=16, ef_construction=64) is used only above the cap, with `ef_search` 100 and `relaxed_order` iterative scan (pgvector 0.8.7 locally; Supabase UNVERIFIED, design 0d).
- **Keyword:** the existing generated `content_tsv` (`simple`) and its GIN index `ix_chunks_content_tsv`. **No migration.**
- **Devanagari decision:** keep `simple`. On this image it does **not** split Marathi or Hindi words (F1), so the risk the prompt names does not occur here. This is proven by tests on every run (section 9, `test_retrieval_devanagari.py`), which also record `datctype`, so a CI image or a database with a different locale fails loudly instead of degrading silently.
- **What `simple` cannot do** is match inflected forms (F4). I do **not** add `pg_trgm` in 5d-1: that needs `CREATE EXTENSION` (superuser, `init.sql` and Supabase) plus a migration with a trigram GIN index, and the prompt says to justify it with golden-set numbers, which I do not have yet (F9). In 5d-3 the harness reports keyword-side hit@k per language and how often the vector side alone found the right chunk. If Marathi keyword recall is clearly the bottleneck, I come back with a separate trigram proposal (migration shown first).
- **Supabase:** add `SELECT datctype FROM pg_database WHERE datname = current_database(); SELECT to_tsvector('simple','प्रकाशसंश्लेषण क्रिया पानांमध्ये होते');` to the owner's staging checklist (next to the pgvector check in design 0d).

## 5. Query handling, scope and sufficiency

- **Normalise:** `clean_text` from `document_text.py` (the same function ingest uses: NFC, invisible characters dropped, whitespace collapsed). Empty after cleaning gives 422 `invalid_query`. Longer than `RETRIEVAL_MAX_QUERY_CHARS` (500, measured after cleaning) gives 422 `query_too_long`. The query text is never logged and never audited.
- **Scope:** `build_retrieval_scope(db, user, *, purpose, class_id=None, subject_id=None, include_drafts=False)` is the **only** constructor used by API code. It calls `document_scope.authorized_pairs` (pairs, never a cross product) and narrows by the optional ids. A requested pair, class or subject outside the set, or an empty result after narrowing, gives the generic 404. Students get `read` pairs; `include_drafts=True` is refused for `purpose="read"` (`ValueError`, a bug, not a user error). The tenant id always comes from `user.tenant_id`; the request has no tenant field.
- **Operator scope for the CLI only:** `RetrievalScope.for_operator(db, tenant_id, class_id, subject_id)` checks that the pair exists in `class_subjects` of that tenant. A test fails if any module under `app/api` imports it.
- **Sufficiency (pure function):** `sufficient = max_vector_sim >= RETRIEVAL_MIN_SIMILARITY` (or `keyword coverage >= RETRIEVAL_MIN_KEYWORD_COVERAGE` when that setting is on). The reason is one of `empty_scope`, `no_candidates`, `below_threshold`, `similarity`, `keyword_coverage`. Below the threshold the response says "Not enough information in your class material." The response always carries the threshold and `threshold_calibrated: false` until a measured value replaces the default.

## 6. Endpoint contract

`POST /api/v1/documents/test-search`, `require("document:view")` (tenant admins and teachers; students 403; default-deny holds).

Request (`extra="forbid"`): `{query: str (1..500), class_id?: uuid, subject_id?: uuid, include_drafts: bool = false, k: int 1..10 = 6}`.

Order of checks: permission, then scope (generic 404 for a pair, class or subject the caller does not own), then the rate limit (429 `test_search_rate_limit_exceeded` with `Retry-After`; 503 when Redis is down outside development), then the query (422), then the embedder (503 `search_unavailable` if the model cannot load; no silent keyword-only fallback).

Response 200:
```
{ sufficient: bool, reason: str, message: str|null, threshold: float, threshold_calibrated: bool,
  keyword_coverage_threshold: float|null, model: str, candidate_count: int,
  hits: [{ rank, chunk_id, document_id, document_title, version_id, version_no, version_status,
           page_start, page_end, heading_path, content,
           vector_rank: int|null, vector_similarity: float|null,
           keyword_rank: int|null, keyword_matched_terms: int|null, fused_score: float,
           flagged: bool, injection_flags: [str] }] }
```

- The body limit is already covered by the area rule (`/api/v1/documents` = 64 KB, JSON included); a test asserts the registry answer for this path. Security headers come from the global layer; a test asserts them on 200, 404 and 429.
- Route matrix row: `("POST", "/api/v1/documents/test-search"): ("document:view", (200, 200, 404, 403, 401))`, where T' is a teacher asking for a pair they do not own.
- No audit entry (it is a read). Logs carry counts and timings only.

## 7. Injection scanner rules (flag, never drop)

Matching runs on a casefolded, NFKC copy of `heading_path + "\n" + content`; the stored text is never changed. **Also scanned (5d-1b review):** the document's `title` and the version's `original_filename`. Retrieval returns both with every hit and the chat layer will put them in front of a model, so they are untrusted text exactly like the content (section 14). Regexes use bounded repetition only (the same ReDoS rule as `upload_security.py`), and there is a linear-time test on 2 MB of hostile text.

| Code | Detects (en / hi / mr examples) |
|---|---|
| `instruction_override` | "ignore / disregard / forget (all) (previous / prior / above / earlier) (instructions / prompts / rules)", "override your instructions"; hi "पिछले (सभी) निर्देशों को अनदेखा / नज़रअंदाज़ करो", "ऊपर के निर्देश भूल जाओ"; mr "मागील / आधीच्या / वरील (सर्व) सूचना / निर्देश दुर्लक्ष करा / विसरा" (nukta and non-nukta spellings both) |
| `role_markup` | `<\|im_start\|>`, `<\|im_end\|>`, `<\|system\|>`, `[INST]`, `<<SYS>>`, `### system`, `### instruction`, a line starting with `system:` or `assistant:` |
| `role_play` | "you are now", "from now on you (are / will)", "act as (an AI / assistant / DAN)", "pretend (to be / you are)", "developer mode", "jailbreak"; hi "अब से तुम", mr "आतापासून तू" |
| `prompt_extraction` | "(reveal / show / print / repeat) (your / the) (system) (prompt / instructions)"; hi "अपने निर्देश बताओ", mr "तुझ्या सूचना सांग" |
| `model_directive` | text addressed to a model: "(AI / assistant / chatbot / language model / LLM / ChatGPT / Claude / Gemini)[,:] (you must / please / always / never)", and "if (a / the) (student / user) asks ... (say / answer / respond / reply)" |
| `tool_call` | `<tool_call>`, `<function_call>`, `"function_call"`, `{"name": "...", "arguments"`, `<\|tool\|>` |
| `link_directive` | a URL near an imperative (visit / click / open / send / fetch / go to; hi खोलो / भेजो; mr उघडा / पाठवा) or a markdown image `![...](http...)` (an exfiltration channel) |
| `encoded_blob` | a base64-looking run of 200 or more characters |

- **Not detectable per chunk:** zero-width and bidi characters (stripped at ingest, F7; a test proves they never reach a chunk); PDF hidden text (B56); PPTX hidden slides (B53; the parser marks no slide as hidden). These stay in the backlog with this reason.
- **Write path:** the index stage computes the flags in a thread before its transaction. `INSERT_CHUNKS_SQL` gains `injection_flags` (comma-joined codes per chunk in a `text[]`, then `string_to_array`; codes never contain a comma). `flagged_chunk_count` = chunks with at least one code. The statement count stays 14 (tested).
- **Publish:** a flagged version needs `acknowledge_flags=true` (existing behaviour and tests). Since 5d-2b also a version never scanned (`flagged_chunk_count` NULL), see section 14.
- **Retrieval behaviour (design A6, B5):** flagged chunks of a published version **are returned**, with `flagged: true` and the codes, so Phase 6 can wrap them as untrusted and Phase 7 can apply strict mode. `excluded` chunks are never returned. Students can only reach a flagged chunk after a teacher acknowledged it at publish.
- **Limits (written in `docs/api/phase5.md`):** this is a tripwire, not a guarantee. Paraphrases, other languages, text in images, split-across-chunk phrases and anything we did not think of pass unflagged. Educational text **about** prompts (computer-science syllabi) will be flagged; that is the price of "flag, don't drop". The 5d-3 report gives the flagged count on your real files.

## 8. Settings

| Setting | Default | Note |
|---|---|---|
| `RETRIEVAL_VECTOR_K` / `RETRIEVAL_KEYWORD_K` | 20 / 20 | design A5 |
| `RETRIEVAL_FINAL_K` | 6 | design A5 |
| `RETRIEVAL_RRF_K` | 60 | design A5 |
| `RETRIEVAL_MAX_QUERY_CHARS` | 500 | design A5 (test search) |
| `RETRIEVAL_MIN_SIMILARITY` | 0.84 | 5d-5: **calibrated** 2026-11-20 for e5-small (40 golden questions; `docs/api/phase5.md` 12.9). Was 0.85, uncalibrated |
| `RETRIEVAL_CALIBRATED_MODEL_KEY` | the e5-small key | 5d-5: `threshold_calibrated` is true only when the active model key equals it |
| `RETRIEVAL_MIN_KEYWORD_COVERAGE` | off (`0` = disabled) | C2; above 0 needs a keyword weight above 0 (5d-5) |
| `RETRIEVAL_EXACT_MAX_CANDIDATES` | 50,000 | design A5 |
| `RETRIEVAL_HNSW_EF_SEARCH` | 100 | design A5 |
| `DOC_TEST_SEARCH_RATE_USER` / `..._WINDOW_SECONDS` | 60 / 600 | design A5 |
| `RETRIEVAL_EMBED_CONCURRENCY` / `RETRIEVAL_EMBED_QUEUE_SECONDS` | 2 / 5 | 5d-2b: query embeddings at once per API process; over the cap a search waits, then 503 `search_busy` |
| `RETRIEVAL_KEYWORD_MAX_DF` | 1.0 (off) | 5d-4: drop keyword words found in more than this share of the candidate chunks |
| `RETRIEVAL_RRF_VECTOR_WEIGHT` / `RETRIEVAL_RRF_KEYWORD_WEIGHT` | 1.0 / 0.0 | 5d-4: weights of the two sides in the fusion (vector above 0; keyword 0 = vector only). 5d-5: keyword default 0 (was 1.0); at 0 no keyword statement runs |
| `RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS` | 1 | 5d-4: a chunk needs this many matched (remaining) words to enter the keyword ranking |

The similarity threshold belongs to the active model (`EMBEDDING_MODEL_ID`). The response names the model, and the docs say a model change needs recalibration.

## 9. Tests (written first; X = fails today, shown failing before the code)

Most new tests fail today with an ImportError. Where possible I will make them fail on an assertion (for example a stub module that returns everything), so the failure shows the right reason. "char" = a characterisation test that passes today on purpose (it pins a fact the design depends on).

**5d-1, retrieval and scope** (fixture `tests/fixtures/retrieval_world.py`: two tenants from `academic_world`, multi-chunk documents with unique canary sentences in English and Marathi, a `ScriptedEmbedder` that maps chosen texts to chosen vectors for crafted vector cases, and the `FakeEmbedder` for the rest)

| Test | Tier | X |
|---|---|---|
| `test_retrieval_pure.py`: `rrf_fuse` maths, ties and determinism; `or_tsquery_text` escaping (quotes, backslash, `& \| ! ( ) : * <->`), 32-lexeme cap; `normalize_query` (NFC, invisible characters, empty, too long); `decide_sufficiency` reasons | fast | X |
| `test_retrieval_devanagari.py`: real Marathi and Hindi sentences; whole-word lexemes asserted exactly; a whole Marathi word query finds its chunk through the keyword side; danda splits; records and prints `datctype` | db | char (lexemes) / X (retrieval through the service) |
| `test_retrieval_scope.py`: **pair test** (doc for (10-A, Math) is not returned for (10-A, Physics) nor (10-B, Math)); a teacher with 10-A Math and 10-B Physics never reaches 10-A Physics; a shared document is visible from both linked classes and not from 11-A; students get only enrolled-class pairs; narrowing to a pair not owned gives 404; empty scope | db, security | X |
| `test_retrieval_leaks.py`: cross-tenant canaries under RLS (tenant B's identical sentence never appears); queued, processing, ready (read purpose), failed, archived and soft-deleted versions are invisible; `excluded` chunks are invisible; **atomic replace**: publish v2 and v1's canary disappears while v2's appears, with no window in which both or neither show (second connection at a barrier, the 5c-2 pattern, no sleeps); other embedding model rows are ignored | db, security | X |
| `test_retrieval_quality.py`: the right chunk ranks first; RRF beats each side alone on a crafted case; a rare code (`ERR_X9`-style) is found by the keyword side while the scripted embedding misses it; a paraphrase is found by the vector side with no shared words; HNSW path (cap lowered to 0) returns the same top hits as the exact path | db | X |
| `test_retrieval_grants.py`: exact privileges of `app_tenant`, `app_worker`, `app_platform` on `chunks` and `chunk_embeddings` (no new grant; worker none); RLS ENABLE + FORCE still on both | db, security | char |
| `test_retrieval_sql_safety.py`: hostile queries (`'; DROP TABLE`, quotes, backslashes, tsquery syntax, 500 Devanagari characters) return results without an error; a source check that the retrieval SQL constants format only `{schema}` / `{dim}` | fast + db | X |
| `security/test_prove_it_fails_retrieval.py` (helpers in `security/retrieval_assertions.py`): with each protection switched off, the helper raises: `authorized_pairs` returns all pairs; pairs replaced by a class x subject cross product; the status filter widened to every status; `include_drafts` forced on for read; the deleted filter removed; the excluded filter removed; the tenant session replaced by the superuser session plus the tenant filter dropped | security | X |

**5d-2, endpoint and scanner**

| Test | Tier | X |
|---|---|---|
| `test_document_route_matrix.py`: the new row (the existing "every route needs a row" guard fails first) | existing tiers | X |
| `test_test_search_route.py`: admin and teacher 200, student 403, no token 401; a foreign pair, class or subject gives the same generic 404 as the other document routes; client-sent ids are only narrowing and never widen; `include_drafts` shows `ready` only and only to teacher/admin; scores and flags present; 422 codes; rate limit by count with a small setting (no sleeps), checked after the 404; 503 when Redis is down; 503 `search_unavailable` when the embedder fails; body-limit registry answer; security headers on 200, 404 and 429 | db (+ security for scope) | X |
| `test_injection_scan.py`: every rule fires on its en/hi/mr examples; the scanner also runs on `document_title` and `original_filename` (a hostile title or file name is flagged; where that flag is stored, per version, and what a title change does to it are decided in the 5d-2 plan); clean syllabus paragraphs (English, Marathi, a Python lesson that says "print the output") do not; NFKC and full-width tricks; linear time on 2 MB of hostile text; the input string object is unchanged | fast | X |
| `test_index_stage.py` additions: flags are written per chunk, `flagged_chunk_count` matches, chunk text is byte-identical to the unscanned chunker output, the statement count is still 14, auto-publish is skipped when anything is flagged | db | X |
| `test_retrieval_flags.py`: a flagged chunk of a published (acknowledged) version is returned with `flagged: true` and its codes; an unacknowledged flagged version cannot be published (existing 409) and so is never seen through the read scope | db | X |
| Prove-it additions: scanner switched off means the flagged-count helper raises; a scanner that alters text means `assert_chunk_text_unchanged` raises; test search with pair validation skipped means the foreign-canary helper raises | security | X |

**5d-3, harness**

| Test | Tier | X |
|---|---|---|
| `test_retrieval_eval_metrics.py`: hit@1/3/5, MRR, refusal accuracy, per-language split, sweep rows, and the location matcher (page, page range, slide, heading substring) on a tiny synthetic golden set | fast | X |
| `test_eval_retrieval_script.py`: missing corpus or golden file gives a clear message and exit 0 with "skipped"; a malformed CSV gives a clear error; the script opens a `READ ONLY` transaction (a write inside it is refused) | fast + db | X |

New files go into `tests/tier_map.py`. Gate impact: estimated 30-60 s more (the security-marked leak and prove-it files); nothing timing-sensitive (B79): no sleeps, no real-clock assertions, barriers instead of waits.

## 10. Evaluation harness (5d-3)

`python -m app.scripts.eval_retrieval --tenant <slug> --class-id <uuid> --subject-id <uuid> [--golden tests/fixtures/golden_set.csv] [--k 10] --report <path.json]`

- It resolves the tenant by slug through the platform pool (read-only), then opens a tenant session with `SET TRANSACTION READ ONLY`, uses `RetrievalScope.for_operator`, and loads the real embedder from `EMBEDDING_MODEL_DIR`.
- It skips cleanly (exit 0, a clear message) when the golden file, the corpus directory or the model is missing.
- Golden CSV columns: `#, Language, Question, Document, Expected location, Should answer`. `Document` is matched against the document title or the version's original file name. `Expected location` is parsed as a page, page range or slide number (hit = the chunk's page range overlaps it) or otherwise as a heading text (hit = casefolded NFC substring of `heading_path`). The exact formats depend on your file (**Q2**).
- Metrics: hit@1/3/5 and MRR@k for "should answer" rows; answer rate (`sufficient=true`) for those rows; refusal accuracy for "should not answer" rows; everything split by `Language`; a per-question table (top-5 ranks, vector similarity, keyword matches, fused score, hit position, sufficient).
- Sweep: one retrieval per question, then pure arithmetic over the stored maximum similarity (and keyword coverage) for thresholds 0.70 to 0.95 in steps of 0.01: refusal accuracy against answer rate. The default is chosen by you from this table; nothing is hard-coded from guesses.
- Also reported: keyword-only and vector-only hit@k (input for the trigram decision, section 4) and the flagged chunk count of the corpus.

## 11. Risks

1. **The real model has never run** (B61). The vector side is only tested with stand-ins until `fetch_model` plus the harness run; e5 similarities cluster high, so the threshold is very sensitive.
2. **The golden set and corpus are not ready** (F9, F10). 5d-3 can build and unit-test the harness, but cannot produce numbers without them.
3. **Keyword ranking has no IDF**, so with an OR query, common words ("what", "is", "काय") add noise. RRF dampens it; the harness measures it. A small question-word stoplist is a measured option, not a 5d default.
4. **Marathi inflection** misses on the keyword side (F4); trigrams are the candidate fix after measurement.
5. **Supabase locale and pgvector version are UNVERIFIED** (F2, design 0d).
6. **Scanner false positives** on computer-science material lead to more manual publishes; **false negatives** are certain (section 7).
7. **API memory and first-query latency:** the lazy e5-small load (about 0.5 GB, seconds of hashing) happens on the first test search of each process (B66).
8. **The HNSW path is rarely exercised**; it is only covered by the forced-cap test.
9. **Flagged chunks reach students after acknowledgment**; Phase 6 must wrap them (the response marks them).

## 12. Open questions (5)

- **Q1. Drafts in test search (C1).** May test search include `ready` (unpublished) versions when the teacher asks (`include_drafts`, default false, ready only)? The design wants it so a teacher can check a flagged version before publishing; your prompt says published only. My recommendation is yes, as described.
- **Q2. Golden set and corpus (F9, F10).** There is no `golden_set.csv` and no draft in `docs/`, and I will not invent expected locations. Please send the CSV, or the questions so you can fill in the locations, and say how `Expected location` is written (for example `p. 3`, `pp. 3-4`, `slide 5`, `Unit 2 > Loops`). Also: `en-01.pdf` is refused (B80), so which files are actually published, and in which database? I will not run anything against `ragplatform`. Either you run the harness there, or I ingest the corpus into `ragplatform_test` with the real model, which needs `fetch_model e5-small` to have been run (B61).
- **Q3. Sufficiency rule (C2).** Is it OK to use two settings, `RETRIEVAL_MIN_SIMILARITY` = 0.85 (uncalibrated) and the keyword-coverage clause off by default, instead of the design's lock-file threshold of 0.82 OR 60% coverage, until the sweep decides?
- **Q4. Review routes (C7).** Add `GET /documents/{id}/versions/{vid}/flags` (read-only, `document:view`, in the matrix) in 5d-2, so a teacher can see what they are acknowledging? I recommend yes. I recommend deferring the chunk-exclude `PATCH` to the backlog.
- **Q5. Auto-publish.** Once the scanner exists, should `DOC_AUTO_PUBLISH_ENABLED` stay `false` by default? I recommend keeping it false in 5d: B76 (lock upgrade under concurrent deletes) is unmeasured, and the scanner's false-positive rate on real files is unknown until 5d-3.

## 13. Build order (after "go")

- **5d-1:** retrieval service, scope (pairs), Devanagari tests, leak, grant and prove-it tests, settings. No route.
- **5d-2:** test-search route, matrix row, rate limit, scanner, index-stage flag write, flag tests (plus the flags route if Q4 = yes).
- **5d-3:** `retrieval_eval.py`, `eval_retrieval.py`, metric tests, `docs/api/phase5.md`, the design records, backlog entries (B56/B53 reasons, query-vector cache, trigram decision, chunk exclude, Supabase locale check, B89 re-check).

Each batch ends with `run_tests gate`, `ruff check .` and `mypy app`, the exact summary lines, and nothing committed.

## 14. Build record

**5d-1 (2026-11-19), approved answers:** Q1 yes, Q2 the owner runs the harness on the dev database, Q3 yes, Q4 yes (flags route in 5d-2, exclude route to B94), Q5 yes; C1-C8 approved. Differences from sections 3 and 5, all smaller than the plan:
- Statement 4 is merged into statement 3: the search statement returns the details of every chunk either side found, so ranks and details come from ONE snapshot (a publish between two statements could otherwise drop a hit). Three statements in total (count, lexemes, search), plus `set_config` on the HNSW path.
- The OR query keeps up to **64** distinct lexemes, not 32: a 500-character question has about 60 words, and the lexemes come back in alphabetical order, so a cap of 32 would drop words arbitrarily.
- Bind values reach Postgres as text and are cast in SQL (`CAST(CAST(:qv AS text) AS vector)`), because asyncpg has no codec for `vector`.
- HNSW check by hand on the test database: the statement can use `ix_chunk_embeddings_hnsw_e5_small` (ordered index scan, semi-join on the candidates) once cheaper paths are removed; on small tables the planner picks the primary key or a sequential scan. Verification at scale is backlog B95.

**5d-1b (2026-11-19), fixes from the independent review of 5d-1.** No migration, no grant, no route.
- **Scope cannot be forged or widened.** `RetrievalScope` is issued only by `build_retrieval_scope`, `for_operator` and `scope.narrow` (a context variable that only the issuer sets; `__post_init__` refuses without it). `dataclasses.replace`, `copy`, `deepcopy` and `pickle` raise, and `search()` refuses any object the module did not issue (an identity set, so an `object.__new__` forgery is refused too). `narrow(class_id, subject_id)` only intersects; an empty intersection is the generic 404. The scope records its `purpose`; `for_operator` gives `read`. The guard test now scans all of `app/` except `app/scripts` for `for_operator` and the private issuer.
- **Sufficiency agrees with the hits.** The top vector-ranked chunk is always in the hits (it replaces the lowest fused hit), and `max_similarity` and keyword coverage come from the returned hits only, with non-finite similarities dropped.
- **Time limits.** `statement_timeout` = `RETRIEVAL_STATEMENT_TIMEOUT_MS` (5,000) is set transaction-locally before the lexeme and count statements, and the caller's value is restored afterwards. A statement over it raises `SearchUnavailable("search_timeout")` (503; the message is the code; the SQL error is not chained). The keyword side ranks at most `RETRIEVAL_KEYWORD_MAX_CANDIDATES` (2,000) matching chunks: **the lowest chunk ids** (deterministic, and cheap next to `ts_rank_cd`). Trade-off: above 2,000 matches, a better-scoring chunk with a higher id is not ranked by keyword (the vector side still sees it).
- **Chat entry point.** `search_for_chat` accepts only a read scope of published versions and returns no hits and no ranked ids when the result is insufficient. `search()` with `manage` stays for test search. `Hit` documents every text field as untrusted (also `docs/api/phase5.md` section 10); the 5d-2 scanner also scans `document_title` and `original_filename` (section 7 and the 5d-2 test table).
- **Validation.** `k` must be an int from 1 to 50 (`ValueError`). A query vector of the wrong size, with a non-finite value or of zero norm is `SearchUnavailable("search_unavailable")`. NaN distances (a stored zero vector) are dropped in SQL on both paths; on the HNSW path after the index scan, so a zero vector can take one of the 20 places. A query without any lexeme (`???`) is `invalid_query` before anything is embedded (the lexeme statement now runs before the count). `RETRIEVAL_HNSW_EF_SEARCH >= RETRIEVAL_VECTOR_K` and `RETRIEVAL_KEYWORD_MAX_CANDIDATES >= RETRIEVAL_KEYWORD_K` are checked at settings load and in `RetrievalConfig`.
- **HNSW.** The settings stay `set_config(..., true)`: transaction-local (a test shows the next transaction starts from the default). `hnsw.iterative_scan` is read back in the same statement and must be `relaxed_order`; the extension version must be 0.8.0 or newer (on a fresh connection pgvector's library may not be loaded yet, so the read-back alone could see a placeholder). Planner check on tiny tables: with only `enable_seqscan = off` the planner still picks the primary key plus a sort; with `enable_sort = off` as well it uses `ix_chunk_embeddings_hnsw_e5_small` (tested). Real scale stays B95.
- **Session tenant.** `search()` checks the session's `app.tenant_id` against the scope's tenant before anything else.
- **Snapshot race, measured.** A publish committed between the count statement and the search statement shows the new version only (READ COMMITTED takes one snapshot per statement, and the search is one statement), never neither (`test_a_publish_committed_between_the_count_and_the_search_shows_one_version`). No design change was needed; only the count can be stale, which changes the path choice, not the result. The earlier test claimed more than it showed and now says what it shows: an uncommitted publish is invisible.
- **Statements per search:** tenant check, read and set `statement_timeout`, lexemes, count, extension schema and version, (HNSW: one settings statement with the read-back), search, restore `statement_timeout`.
- **Tests:** leak checks now cover the hits and both ranked lists and assert that neither list was cut. Every leak scope has 8 keyword-only distractors that fill the six hits. Prove-it 7 says what RLS, the tenant filter and the pair scope each add; 7b switches off only the tenant filter (RLS bypassed, broken pair scope); 8 catches a leak at vector rank 9. New files: `test_retrieval_negative.py` (revoking an assignment, an enrollment and an offered pair; hostile input; the same model key with another dimension on both paths; v1 published + v2 ready; a scope used in another tenant's session) and `test_retrieval_limits.py` (timeout with prove-it, keyword cap with prove-it, NaN distances, HNSW read-back and planner).
- **Accepted, in the backlog:** the cold-start model load holds a pooled connection (B96).

**5d-2 (2026-11-19): test search, flags route, injection scanner, index-stage flag write.** No migration, no grant, no new dependency, no LLM call, no student route, no model warm-up (B96 stays for Phase 8). API reference: `docs/api/phase5.md` section 11.

- **Scanner** (`app/services/injection_scan.py`): the eight codes of section 7 in that order, with English, Hindi and Marathi examples for every rule. Matching runs on a copy: format characters (Cf) removed, Unicode tag characters (U+E0020-E007E) turned back into the ASCII they hide ("ASCII smuggling"; an addition), then NFKC, casefold and NFKC. The text is never changed (tested with a deep copy and byte comparison).
- **Linear time, shown without a clock (B79).** Each rule is a set of alternatives that start with a literal trigger (`ignore`, `<|`, `http`, `पिछल` ...). A pure-literal trigger pattern finds the next trigger, the rule is matched anchored there and may read at most 528 characters, and the next trigger search starts one character later. A test records every regex call of a 2 MB scan: the trigger searches cover disjoint parts of the text and every anchored match stays within the bound. A structural test checks that every repetition is bounded (at most 200) and every match is at most 512 characters long. `encoded_blob` (no literal start) only starts at the beginning of a base64 run. Nine 2 MB hostile inputs are scanned for real, each with one real phrase at the end that must still be found. The scanner test file takes about 12 s.
- **Rule refinements against false positives** (all inside the plan's rule table):
  - `role_play`: "you are now" fires only when a role word follows (a, an, the, DAN, free, "in developer mode" ...), so "you are now ready to solve" is not flagged.
  - `prompt_extraction`: "print / show **the** prompt" needs system, initial, hidden ... in between, because Python lessons say "input() prints the prompt"; "your prompt / instructions" always fires. In Marathi only the informal तुझ्या / तुझे fires, because "तुमच्या सूचना सांगा" means "tell us your suggestions".
  - `encoded_blob` needs at least one digit, `+` or `/`, so a DNA sequence is not flagged.
  - `role_markup`: `system:` / `assistant:` fire only at column 0 of a line, and `##...system` fires after any whitespace.
  - `model_directive`: Hindi and Marathi alternatives added (the plan listed English only).
- **Index stage:** `scan_chunks` runs in `asyncio.to_thread` before the transaction opens (tested: in a worker thread, with no index statement run yet). The codes travel in the existing chunk INSERT as one comma-joined text per chunk (`string_to_array`), so the statement count is still **exactly 14** for 100 and 1,000 flagged chunks. That is now asserted; the earlier test only checked `< 25`. `flagged_chunk_count` is the number of chunks with codes. `document:ingested` details gain `flagged_chunk_count`, a count only. Auto-publish skips any version with flags (tested with real hostile text instead of a hand-set count), and `DOC_AUTO_PUBLISH_ENABLED` stays false.
- **Title and file name** are scanned when a test-search or flags response is built (`title_flagged`, `filename_flagged`). No column; a PATCHed title changes the answer (tested). They do not affect publish or auto-publish (backlog B98).
- **Test search** (`POST /api/v1/documents/test-search`), in this order: permission, scope (`build_retrieval_scope(purpose="manage")` plus `narrow`, the generic 404), rate limit (`check_test_search_rate`), query (`normalize_query` plus a new public `retrieval.query_lexemes`, so `???` is a 422 before the model loads), model, search. Three short steps:
  1. scope, rate limit and query checks in one transaction;
  2. the model load with **no transaction open** (this route no longer holds a connection during a cold load; `retrieval.search` and the chat path are unchanged, B96 updated);
  3. the search in a second transaction.

  `get_query_embedder` returns a loader, not the model, so the model cannot load before the checks. Any load or embed failure is `503 search_unavailable` (only the exception class is logged). `search_timeout` is a 503 with a fixed message. Logs carry counts and timings only. Not audited.
- **Schema:** `query` is a strict string capped at 4,000 characters (FastAPI's own 422). The contract limits (1-500 after cleaning) are checked by the route with `invalid_query` / `query_too_long`, after the scope and the rate limit, as section 6 orders. `include_drafts` is a strict boolean and `k` a strict integer from 1 to 10.
- **Bug found by the hostile-query test (fixed, regression-tested):** a JSON string with a lone surrogate (`"\ud800"`) ended as a generic **500** on every JSON route. Pydantic refuses it (`string_unicode`), and FastAPI's default 422 handler then failed to encode the echoed input. `app/main.py` now has a validation handler with the same body shape that puts U+FFFD in place of the surrogate, so the answer is a 422 (also checked on `/auth/login`). A lone surrogate is therefore a 422, not a cleaned query as section 5 assumed.
- **Flags route** (`GET .../versions/{version_id}/flags`): as planned in Q4. Added: paging (`after_ordinal`, `limit` 1-200, default 50, `next_after_ordinal`), `excluded` per item and `flags_acknowledged` per version. The excerpt (at most 300 characters) starts 80 characters before the first match (`first_match_offset` maps back to the original text) or at the chunk start.
- **Route matrix:** both rows, and the "every route needs a row" guard failed first. Test search's T' asks for 10-A Math, a pair T2 does not teach.
- **Settings:** `DOC_TEST_SEARCH_RATE_USER=60`, `DOC_TEST_SEARCH_RATE_WINDOW_SECONDS=600` (`config.py`, `.env.example`, bounds tested).
- **Real corpus** (`tests/fixtures/real_corpus`, parsed in-process and chunked with the FakeEmbedder's word counter, so real-tokenizer chunk counts differ): `en-01.pdf` 0 of 6 chunks flagged, `hi-01.docx` 0 of 38, `mr-01.pptx` 0 of 12; no file name flagged. Three files are far too few to judge the false-positive rate (B99).
- **Not detectable per chunk** (B53 and B56 updated): hidden PPTX slides and hidden PDF text are scanned like visible text. A matching line is flagged, but nothing marks it as hidden.
- **Tests first.**
  - Scanner: 93 failed against a do-nothing stub; 9 passed by design (clean texts, the empty input, the zero-width characterisation).
  - Index stage: 4 failed, for these reasons: a flagged version was auto-published, no flags were stored, and the scanner was never called. The 14-statement test and the default-off test passed (guards).
  - Routes: the matrix guard failed with "matrix rows without a route", the flags tests got 404, and the test-search tests got 405 (38 failed). Passing before the routes existed: prove-it 1-2 (the scanner existed by then), the out-of-class student test, and the body-limit registry check (the area rule already covered the path).
  - New files: `test_injection_scan.py` (fast, security), `test_test_search_route.py`, `test_document_flags_route.py`, `test_retrieval_flags.py`, `security/test_prove_it_fails_scanner.py` (db, security). Additions to `test_index_stage.py`, `test_document_route_matrix.py` and `test_ingest_settings.py`. Helpers: `tests/security/scanner_assertions.py` and `tests/fixtures/search_api.py` (the dependency override).
- **Prove-it** (each protection off, the same helper raises):
  1. the scanner returns nothing: `assert_flags_written`;
  2. a scanner that edits chunk text: `assert_chunk_text_unchanged`;
  3. test search builds the scope from the request ids: `assert_no_foreign_canary`;
  4. the rate limit is removed: `assert_rate_limited`;
  5. the flags route skips `load_document_for`'s scope: `assert_foreign_version_hidden`.

**5d-2b (2026-11-19): fixes from the independent review of 5d-2.** No migration, no grant, no new dependency. API reference: `docs/api/phase5.md` sections 10, 11.1-11.5.

| # | Finding | Result | Proving tests |
|---|---|---|---|
| 1 | Titles and file names scanned raw | Fixed: `scan_metadata` = `clean_text` then scan; `normalize_for_scan` drops the same invisible set (`document_text.invisible_code_points`) and collapses whitespace runs like `clean_text` | `test_a_title_is_scanned_after_ingest_cleaning_and_both_copies_agree`, `test_the_scan_copy_drops_every_character_clean_text_drops_and_reveals_tag_text`, `test_whitespace_runs_collapse_and_line_breaks_survive` |
| 2 | Cheap bypasses | Fixed (20 of 20 reported, 0 of 20 before): `İ`, accents and marks after Latin letters (never Devanagari signs), Cyrillic and Greek look-alikes, separators `_ - . , / : ;`, `you're now`, `<img`, spaced chat tokens, five filler words. Trigger + bounded window kept; linear-time tests re-run with five new 2 MB inputs | `test_the_reported_cheap_bypasses_are_caught`, `test_marks_are_stripped_after_latin_letters_only`, `test_look_alikes_map_to_latin_only_outside_devanagari`, `test_one_2mb_scan_reads_each_part_...`, `test_two_megabytes_of_hostile_text_...` |
| 3 | False positives on ordinary text | Fixed: rules narrowed as listed in `docs/api/phase5.md` 11.3. Corpus of 54 ordinary sentences (en/hi/mr, CS lessons on prompts and chatbots): 0 flagged (21 before) | `test_the_ordinary_syllabus_corpus_is_not_flagged` (prints the rate) |
| 4 | Versions indexed before 5d-2 never scanned | Fixed: publish treats `flagged_chunk_count` NULL as "not scanned: acknowledgement required" (409 `reason: not_scanned`); operator CLI `app/scripts/rescan_flags.py` (dry run by default, `--apply`, tenant slug). **The owner's dev database has such versions: run `python -m app.scripts.rescan_flags <slug>` there, then `--apply`** (not run by me) | `test_a_version_never_scanned_needs_acknowledgement_and_test_search_still_finds_it`, `tests/test_rescan_flags.py`, prove-it 7 |
| 5 | Unbounded embedding load | Fixed: per-process cap (`RETRIEVAL_EMBED_CONCURRENCY` 2, an asyncio semaphore and a thread pool of its own), embedded BEFORE the search transaction, over the cap a wait of up to `RETRIEVAL_EMBED_QUEUE_SECONDS` (5) then 503 `search_busy` | `test_parallel_test_searches_stay_under_the_embedding_cap`, `test_over_the_cap_...search_busy...`, `test_the_query_is_embedded_before_the_search_transaction_opens`, prove-it 6 |
| 6 | Scope and search in two transactions | **Accepted**, documented below and in `docs/api/phase5.md` 11.1 | `test_an_assignment_revoked_between_the_scope_and_the_search` |
| 7 | Docs overstated `Hit.flagged` | Fixed: chunk text only; title and file-name flags exist only in the two API responses; a title edited after publish is not re-reviewed (B98) | docs only |
| 8 | 422 echoed the input | Fixed: `app/main.py` keeps `type`, `loc`, `msg` and `ctx` with schema limits only. No use of `input` in `frontend/src` or the docs | `tests/test_validation_errors.py`, prove-it 8 |
| 9 | Flags excerpt showed the first content match only | Fixed: `matches` per code with `source` (heading_path / content) and a span of at most 300 characters; untrusted text | `test_every_code_shows_where_it_matched_also_in_the_heading_only`, `test_review_matches_give_each_code_its_source_and_a_capped_span`, `test_find_matches_returns_the_span_in_the_original_text` |
| 10 | Stale comment on `DOC_AUTO_PUBLISH_ENABLED` | Fixed: says why it stays false (tripwire, unmeasured rates, B76) | `test_auto_publish_stays_off_and_the_comment_says_why` |

- **Accepted gap (finding 6): scope versus search.** Test search reads the caller's pairs in its first transaction, embeds with no transaction of its own open, and searches in a second transaction with those pairs. An assignment revoked between the two still applies to that ONE request; the next request is refused. Equivalent to a request that finished a moment before the revocation. Closing it would need the embedding inside the scope transaction, which holds a pooled connection during the model call (B96). The permission dependency's own session is open for the whole request anyway (B100, found here).
- **Rule changes (finding 3), each from the corpus:** `link_directive` needs an exfiltration sign (send / upload / forward / submit-to verb, a data-carrying query parameter or placeholder, an image tag); `role_play` needs a model-ish role after "you are now", "pretend", "act as", "from now on you are", and "developer mode" / "jailbreak" only next to AI, model, prompt or instructions words; the bare Hindi and Marathi "from now on you" rules need a role noun; `model_directive` needs the text to address a model, or "the chatbot" in an "if a student asks" note (Hindi and Marathi: addressed form only); `role_markup` drops `assistant:` and `##... user/assistant`, keeps chat tokens, `### system`, the Alpaca `### Instruction:` / `### Response:` markers and `system:` at a line start; `encoded_blob` needs a letter too; `instruction_override` needs an instruction or prompt noun (or "your rules"). Three 5d-2 test examples changed with the rules (`disregard prior rules` -> `disregard your prior rules`, `Pretend you are the examiner`, `Enable developer mode.` / `This is a jailbreak.` now carry a model word); every other example is unchanged and still flagged.
- **Spans (finding 9):** `find_matches` maps a match in the scan copy back to the original text by a binary search over prefixes (the copy of a prefix only grows with it), so spans stay right through removed marks, collapsed spaces, invisible characters and ligatures (tested). `first_match_offset` and `review_excerpt` use it.
- **Test fixes from the review:** the lone-surrogate test now proves the embedder was never called (and a valid query in the same test reaches it); the str-unchanged half of the input test is gone (the chunk half stays); the look-behind test checks behaviour (where a match may start, and the known limit when a run's first 200 characters have no digit); the zero-width test now checks the scanner on cleaned input; the body-limit test sends a real 64 KB + 13 byte body (413) and one just under (200); a second and third prove-it break the REAL scope function (`purpose="read"`: a student with document:view reaches class material; `narrow` dropped: no 404).
- **New negative tests:** rate limit before validation (limit 1: `???` is 422, the next valid query 429); NULL count publish and test search; assignment revoked in the gap; heading-only flag span; a student with document:view through a custom role (empty scope / 404 on test search, 404 on the flags route); parallel searches under the cap.
- **Tests first:** against stubs with the 5d-2 behaviour, 21 scanner tests and 23 route, flags, publish, 422, rescan and prove-it tests failed for the stated reasons (the corpus test printed 21 of 54 flagged, the bypass test 0 of 20; publish of a NULL-count version returned 200; the 422 bodies echoed the password; the cap settings did not exist). Passing before the fix, by design: the student-with-document:view tests (the manage scope already gave students nothing), the revoked-assignment test (it pins the accepted behaviour), prove-it 3b and 3c (the real scope function was already right).
- **Gate split:** `tests/test_test_search_route.py` (db, security: who may search, scope, isolation, rate-limit order, student, revoked assignment) and `tests/test_test_search_contract.py` (db only: the 422 matrix, k, the 503s, the embedding cap, keys and scores, the 429 shape, logging, headers, body limit); likewise `tests/test_document_flags_route.py` (gate: 404, scope, student) and `tests/test_document_flags_contract.py` (db only). The 2 MB scanner test scans each input once and checks that the only match is the appended phrase at the end (it was two scans). Gate: 9:22 before, 8:59 after (539.55 s), on the developer machine.


**5d-3 (2026-11-19): retrieval-only evaluation harness.** No migration, no grant, no new dependency, no LLM call, no route, no change to any retrieval setting (`RETRIEVAL_MIN_SIMILARITY` stays 0.85, uncalibrated). Not committed. How to run it and read it: `docs/api/phase5.md` section 12.

- **Built:** `app/services/retrieval_eval.py` (pure: golden loader, location and document matcher, hit@k, MRR, answer rate, refusal accuracy, per-language split, the 0.70-0.95 sweep with its two marked thresholds, the JSON report and the plain-English summary) and `app/scripts/eval_retrieval.py` (the command). No chunk text can reach the report: `ChunkRef` has no content field.
- **Read-only:** both transactions (the tenant lookup and the search) start with `SET TRANSACTION READ ONLY`; the scope is `RetrievalScope.for_operator` (published only). Prove-it: with the statement removed, 3 tests fail (`assert ['off'] == ['on']`).
- **Skips (exit 0):** golden file, `EMBEDDING_MODEL_DIR` / model folder, or no published documents with chunks. The documents check runs before the model is loaded. Errors (exit 2) name the golden-file row: a malformed row, a question with no searchable word or over 500 characters (found before the model loads), an unknown tenant, a pair the tenant does not offer.
- **Decisions, in the plan's spirit:**
  - `Document` matches the title, the file name or the file name without its extension, exactly (no partial names).
  - Outcomes per should-answer question: `document_not_in_scope`, `no_candidate`, `wrong_chunk`, `below_threshold`, `ok` (checked in that order).
  - A balanced-threshold tie goes to the highest threshold; the 90% line uses exact fractions; the sweep values are whole hundredths, so no float drift.
  - The keyword-only and vector-only rankings hold chunk ids only, so the command looks up their places (title, pages, heading; never content) with one read-only SELECT per question.
  - A second sweep over the keyword coverage alone is in the JSON report.
  - Backlog B97 check: the report states the keyword cap and the chunk count of the scope.
- **Test files:** the plan listed one script test file; the "documents missing" and READ ONLY cases need the database, and one file has one tier, so there are two: `tests/test_eval_retrieval_script.py` (fast) and `tests/test_eval_retrieval_db.py` (db). Plus `tests/test_retrieval_eval_metrics.py` (fast). The 5d-1b guard (`for_operator` only from `app/scripts`) still passes and now has a companion check.
- **Tests first:** both fast files failed at collection with `ImportError: cannot import name 'retrieval_eval' / 'eval_retrieval'`, the stated reason (no stub). One assertion of mine was wrong (the heading reads "Keyword only", not "keyword only") and was fixed in the test.
- **No golden set:** `tests/fixtures/golden_set.csv` does not exist (F9) and no row was invented; every test uses a synthetic CSV.
- **Not done, recorded:** `threshold_calibrated` is not a setting (B105); the numbers need the owner's run on the real model (B61) and the real files.


**5d-4 (2026-11-20): keyword pruning, weighted fusion, `--compare`.** No migration, no grant, no new dependency, no LLM call, no route, no trigram search. **No default changed:** `RETRIEVAL_MIN_SIMILARITY` stays 0.85 and `threshold_calibrated` stays false. Not committed. Why: the first real evaluation (14 questions, 100 chunks, e5-small) showed the hybrid search worse than the vector side alone (Marathi hit@1 40% hybrid against 100% vector only; overall 67% against 92%; keyword-only Marathi 20%), backlog B106. Usage: `docs/api/phase5.md` section 12.8.

- **Hypothesis, stated as one:** the OR keyword query holds very common words (ची, आहेत, कोणती, काय, what, is, the) that match most chunks; `ts_rank_cd` has no IDF; and RRF gives the noisy keyword ranks the same weight as the vector ranks. 5d-4 builds the levers and the table that tests it. It decides nothing.
- **Four settings, all neutral by default** (`config.py`, `.env.example`, `RetrievalConfig`): `RETRIEVAL_KEYWORD_MAX_DF` (0 to 1, 1.0 = off), `RETRIEVAL_RRF_VECTOR_WEIGHT` (above 0) and `RETRIEVAL_RRF_KEYWORD_WEIGHT` (0 or more; 0 = vector only), `RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS` (1 to 64).
- **Pruning, no extra round trip.** With `max_df` below 1 the statement that already counts the candidates (`count_and_frequencies_sql`) also returns, per query lexeme, the number of candidate chunks that contain it (the lexemes travel as bind parameters, quoted by the same `_quote_lexeme`). The pure `prune_lexemes` drops a lexeme found in MORE than `max_df` of the candidates (3 of 10 stays at 0.3); a lexeme found nowhere stays (it matches nothing). `keyword_lexemes` returns `[]` when nothing is left or fewer than `min_matched_terms` are left, and then `search_sql(keyword=False)` leaves the keyword side out: no `ts_rank_cd` statement runs and the answer comes from the vector side. No stopword list, language independent (tested with Marathi and English function words). A rare identifier survives (tested). Frequencies are counted over the candidate chunks of the caller's scope, not the tenant (tested: a word that is rare in this pair and common in another pair of the same tenant is kept).
- **Default statements untouched.** With the defaults the SQL text contains none of the new machinery (tested), the same number of statements run, and the hit ids and fused scores are the same as before 5d-4: the regression test compares the scores bit for bit with the unweighted formula and pins the hit order, and that order was captured from the unchanged code (the test passes on HEAD up to the first new-setting argument).
- **Weighted RRF:** `score = sum(weight_side / (RRF_K + rank))`. With 1.0 and 1.0 the numbers are identical to `1.0 / (k + rank)`. A side with weight 0 has no say: chunks only that side lists are left out, so keyword weight 0 equals the vector side alone (and vector weight 0 the keyword side alone in the pure function). The config refuses a vector weight of 0 (sufficiency reads the vector side), and `select_hits` no longer assumes the top vector chunk is in the fused list.
- **Min matched terms** adds a `cardinality(intersect) >= :min_terms` filter to the keyword CTE only when above 1, before the candidate cap; the `matched` column and the coverage clause count the surviving lexemes only (coverage divides by the surviving count; identical at the default).
- **`--compare`** (`eval_retrieval.py`, pure parts in `retrieval_eval.py`): one READ ONLY transaction; each question embedded once (the per-question `QueryEmbedder` answers for that question only and is reused by every configuration); the same `retrieval.search` per run with the config overridden by `dataclasses.replace` (no SQL copy); rows `current settings`, `vector only` (keyword weight 0), then the grid (default 3 x 5 x 2 = 30 points; `--grid vw=.. df=.. mt=..` for a smaller one, at most 200 points). The table has hit@1, hit@3, hit@5 and MRR for ALL and per language, answered and refused counts at the current threshold, and `+W/-L` hit@1 changes against `current settings`. The report is numbers only. A normal run is unchanged except that it states the four settings it used.
- **Overfitting guard:** the golden-set sizes are printed, with `too few to trust small differences` below 20 should-answer or 8 should-not-answer rows (the first real set has 14 questions, so both warnings will show).
- **Tests first** (written before any code; run against the unchanged code in the test database): the four new files gave **68 failed, 1 passed, 11 errors**. Reasons: `rrf_fuse() got an unexpected keyword argument 'vector_weight'`, `RetrievalConfig.__init__() got an unexpected keyword argument 'keyword_max_df'`, missing `prune_lexemes` / `keyword_lexemes` / `build_grid` / `parse_grid` / `hit1_changes` / `sample_warnings` / `build_compare_report` / `format_compare`, `'Namespace' object has no attribute 'compare'`; the 11 errors are the compare-DB tests, whose setup sets the new settings (`Settings` has no attribute `RETRIEVAL_KEYWORD_MAX_DF`). The one test that passed is the guard that the default statements carry no new machinery. One test of mine was wrong and was fixed in the test (a vector weight cannot lift a chunk that is only on the vector side above one on both sides; the arithmetic is now in the test).
- **Test-design finding:** `ts_rank_cd` ties are broken by chunk id, and chunk ids are random, so a pinned hit order was unstable between runs. The crafted corpus now gives its chunks ids that sort in corpus order.
- **Prove-it:** pruning switched off (`prune_lexemes` keeps everything) fails the crafted check; weights ignored (`rrf_fuse` without them) fails the weight check; the min-matched filter removed from the SQL fails its check. Each passes with the feature on first (`on_then_off`).
- **New files:** `test_retrieval_fusion_pure.py` and `test_retrieval_compare_pure.py` (fast), `test_retrieval_pruning.py` and `test_eval_retrieval_compare_db.py` (db), all in `tests/tier_map.py`. Existing retrieval, leak, scope, grant, negative, quality, limits, flags, contract and prove-it tests are unchanged and green.
- **Not done, recorded:** no value is chosen (the owner runs `--compare` on the real set, B106); the trigram decision (B92) waits for those numbers; the golden set is small.
- **Checks (2026-11-20, Windows):** `run_tests gate`: `=== 1767 passed, 11 skipped, 777 deselected, 4 xfailed in 560.02s (0:09:20) ===`; `ruff check .`: `All checks passed!`; `mypy app`: `Success: no issues found in 96 source files`. The gate holds only fast and security files, so the db tests of the touched modules ran separately: 225 passed (every `test_retrieval_*` file, the retrieval prove-it file, the test-search contract and the ingest-settings tests, and the two new retrieval files), 111 passed for the harness files, then the pruning file alone with its HNSW case: 22 passed. `run_tests full` was not run.


**5d-5 (2026-11-20): final retrieval calibration.** No migration, no grant, no new dependency, no LLM call, no route. Not committed. Evidence: the owner's harness runs on the dev database (40 golden questions, 30 should-answer / 10 should-not-answer, 2 documents, 100 chunks, e5-small); the full record is `docs/api/phase5.md` section 12.9.

- **Vector only by default.** `RETRIEVAL_RRF_KEYWORD_WEIGHT` default `0.0` (was 1.0); vector weight 1.0, `RETRIEVAL_KEYWORD_MAX_DF` 1.0 (off) and `RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS` 1 unchanged. Vector only: hit@1 83%, MRR 0.88 against the old hybrid's 53% / 0.69 and the best pruned hybrid's 73% / 0.82 (`+9/-0`). At weight 0 the keyword side is **not executed** (`keyword_side_runs`): the search statement uses the empty `kw` CTE (no `ts_rank_cd`, no `:tsq`, no keyword cap) and no per-word frequency count runs even with `RETRIEVAL_KEYWORD_MAX_DF` below 1. The hits equal the old weight-0 results exactly (same ids, same fused scores; tested against a fusion of a hybrid run's rankings with keyword weight 0). Behaviour change: at weight 0 a hit's `keyword_rank` / `keyword_matched_terms` and the result's `keyword_ranked` are now empty (before, the keyword side ran and was reported without a say). The keyword code and settings stay for exact-code search; weights 1.0 / 1.0 reproduce the old hybrid exactly (regression test with the ids and scores pinned before 5d-4).
- **Coverage clause guard.** `RETRIEVAL_MIN_KEYWORD_COVERAGE > 0` with keyword weight 0 is refused (Settings validator and `RetrievalConfig`): with no keyword side nothing measures coverage, so the clause would silently never fire.
- **Threshold 0.84** (was 0.85, uncalibrated). Sweep: 0.84 answers 97% / refuses 70%; 0.85 90% / 80%; 0.86 80% / 90%. Valid questions at 0.840, 0.849, 0.850 sit below the hard negatives at 0.863 (Angular routing) and 0.856 (Redux), so no threshold separates them; 0.84 chosen on purpose, near-topic questions go to the Phase 6 grounding rule and Phase 7 evaluation (B108).
- **B105 closed: `threshold_calibrated` is derived.** New setting `RETRIEVAL_CALIBRATED_MODEL_KEY` (default the e5-small key). `RetrievalConfig.threshold_calibrated` is gone; `retrieval.is_threshold_calibrated(model_key, config)` is called in `_result`, the one place results are built, so the test-search route and `search_for_chat` both report true only for the calibrated model. `retrieval.CALIBRATION` holds the record (date, key, threshold, 30 / 10, 2 documents, 100 chunks); a test checks it against the defaults. Startup log line `retrieval_threshold` (threshold, calibrated key, active key from `models.lock.json`, match; a warning when false), called from the API lifespan.
- **Harness.** Prints `calibrated on 2026-11-20 for <key> using 30 should-answer / 10 should-not-answer questions` (plus the measured value when the run's threshold differs), or `WARNING: the threshold is not calibrated for this model`. The old "not calibrated yet: a default" text is gone. With the keyword side off the keyword-only numbers are `None` / `n/a` (not 0%) and the B97 cap line says the cap is unused. `--compare` unchanged; its `current settings` row now equals `vector only` with the defaults; grid rows still use keyword weight 1.
- **Golden set committed as a fixture** (`tests/fixtures/golden_set.csv`, already tracked; the 40-row version is in the working tree). `tests/test_golden_set_fixture.py` parses it with the harness parser and checks at least 20 / 8 rows, both languages on both sides, supported locations.
- **Test helpers.** The shared leak and scope helpers (`tests/security/retrieval_assertions.py`) now search with both sides on by default (`search_config()`), so the leak, scope, prove-it, SQL-safety, limits and quality tests keep covering the keyword SQL that is now off in production; the vector-only statement is a subset of it. Tests that pin the defaults (0.84, vector only, `threshold_calibrated`) changed deliberately and say why.
- **Tests first** (run against the unchanged code): the two new fast files gave **16 failed, 4 passed**: all 16 tests of `test_retrieval_calibration.py` failed, and the 4 passing were `test_golden_set_fixture.py`, which pass because the fixture already existed; shown to fail on a truncated copy, an English-only copy and a copy with `slide two`). Reasons: old defaults (`RETRIEVAL_RRF_KEYWORD_WEIGHT` 1.0, threshold 0.85), `RetrievalConfig` had no `calibrated_model_key`, no `keyword_side_runs` / `CALIBRATION` / `is_threshold_calibrated` / `log_threshold_calibration` / `calibration_note`, and `threshold_calibrated` still a config field. DB tests: **14 failed, 9 passed** (selected): `ts_rank_cd` executed at weight 0, `keyword_ranked` not empty at weight 0, `Settings` had no `RETRIEVAL_CALIBRATED_MODEL_KEY`, `threshold_calibrated` False for the e5 key.
- **Prove-it:** `is_threshold_calibrated` replaced by a constant `True` fails the false-case checks (pure and through both entry points); `keyword_side_runs` forced to `True` at weight 0 fails the "no keyword statement" check. Each passes with the feature on first.
- **New files:** `test_retrieval_calibration.py`, `test_golden_set_fixture.py` (fast), `test_retrieval_calibration_db.py` (db), all in `tests/tier_map.py`.
- **Not done, recorded:** recalibration with a larger golden set and per-tenant data before launch (B107); near-topic questions are left to Phase 6 / Phase 7 (B108); trigrams only if the keyword side comes back (B92).
- **Checks (2026-11-20, Windows):** `run_tests gate`: `=== 1787 passed, 11 skipped, 790 deselected, 4 xfailed in 544.26s (0:09:04) ===`; `ruff check .`: `All checks passed!`; `mypy app`: `Success: no issues found in 96 source files`. The db tests of the touched modules ran separately (16 files: every `test_retrieval_*` db file, the retrieval prove-it file, both test-search files and both harness db files): `182 passed in 439.87s (0:07:19)`. `run_tests full` was not run.

### 5e-1 (2026-11-20): two small backend additions for the frontend

No migration, no grant, no new dependency, nothing under `frontend/`. Not committed. Contract: `docs/api/phase5.md` sections 4.1, 4.2 and 5; frontend note: backlog B109.

- **`GET /api/v1/documents/limits`** (`document:view`): the effective limits of the caller's own tenant (override, else platform default) and `accepted_extensions`; exactly six keys. No usage, quotas, tenant id, overrides or ceilings. One single-row read (`load_tenant_limits`, the same function the upload routes use). Declared before `GET /{document_id}`, otherwise "limits" would be read as an id (a 422).
- **`retryable`** (`bool | null`) on `DocumentVersionResponse`. The rule moved into ONE function, `document_upload.is_retryable(status, error_code)`; the status route, the retry route (which had its own copy of the rule) and the version response all call it. Null unless the version is failed.
- **Tests written first** (`test_document_limits_route.py`, `test_document_retryable.py`, a new row in the route matrix, the `retryable` key in `test_document_upload.py::VERSION_KEYS`): **22 failed, 20 passed** before the code (422 because `limits` was taken for a document id; no `retryable` key; no `is_retryable`; the matrix row without a route). The student 403 and anonymous 401 cases already passed before the code, because the document-id route's permission check runs before its path validation: they were proven with the prove-it below, not by failing first.
- **Prove-it:** the new route with `get_current_user` instead of `require("document:view")` fails the student test, the matrix row and the declared-permission guard (3 failed); `retryable` computed separately in `_version_response` (only `worker_crash`) fails the parity tests, naming `processing_failed: status=True detail=False` (2 failed). Both restored.
- **Test decisions:** a token of tenant A with tenant B's slug (or `?tenant=`) gets A's own limits, never B's (the tenant comes from the token); asserted as "refused, or A's own limits".
- **Checks:** `run_tests gate --touched` (2026-11-20, Windows): `= 1 failed, 1976 passed, 11 skipped, 623 deselected, 4 xfailed in 803.01s (0:13:23) =`. The one failure is `tests/security/test_prove_it_fails_retrieval.py::test_7a_rls_alone_holds_and_bypassing_it_is_caught` (`SearchUnavailable: search_timeout` in its privileged search, guards off). It fails the same way alone (1 failed, 8 passed) and on a clean checkout of HEAD without this step's changes, so it is not caused by 5e-1; cause not diagnosed (a 5 s statement timeout on this machine's test database). `ruff check .`: `All checks passed!`; `mypy app`: `Success: no issues found in 96 source files`. `run_tests full` was not run.
