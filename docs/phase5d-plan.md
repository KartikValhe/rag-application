# Phase 5d plan: retrieval, test search, injection scanner, retrieval-only evaluation

Status: APPROVED 2026-11-19 · 5d-1 built, review fixes 5d-1b built (section 14) · 5d-2 and 5d-3 not started.
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
- **Publish:** unchanged. A flagged version needs `acknowledge_flags=true` (existing behaviour and tests).
- **Retrieval behaviour (design A6, B5):** flagged chunks of a published version **are returned**, with `flagged: true` and the codes, so Phase 6 can wrap them as untrusted and Phase 7 can apply strict mode. `excluded` chunks are never returned. Students can only reach a flagged chunk after a teacher acknowledged it at publish.
- **Limits (written in `docs/api/phase5.md`):** this is a tripwire, not a guarantee. Paraphrases, other languages, text in images, split-across-chunk phrases and anything we did not think of pass unflagged. Educational text **about** prompts (computer-science syllabi) will be flagged; that is the price of "flag, don't drop". The 5d-3 report gives the flagged count on your real files.

## 8. Settings

| Setting | Default | Note |
|---|---|---|
| `RETRIEVAL_VECTOR_K` / `RETRIEVAL_KEYWORD_K` | 20 / 20 | design A5 |
| `RETRIEVAL_FINAL_K` | 6 | design A5 |
| `RETRIEVAL_RRF_K` | 60 | design A5 |
| `RETRIEVAL_MAX_QUERY_CHARS` | 500 | design A5 (test search) |
| `RETRIEVAL_MIN_SIMILARITY` | 0.85 | **UNCALIBRATED**; above the design's unverified 0.82, so it refuses more; replaced from the 5d-3 sweep |
| `RETRIEVAL_MIN_KEYWORD_COVERAGE` | off (`0` = disabled) | C2 |
| `RETRIEVAL_EXACT_MAX_CANDIDATES` | 50,000 | design A5 |
| `RETRIEVAL_HNSW_EF_SEARCH` | 100 | design A5 |
| `DOC_TEST_SEARCH_RATE_USER` / `..._WINDOW_SECONDS` | 60 / 600 | design A5 |

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
