import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import axios, { type InternalAxiosRequestConfig, type AxiosResponse } from "axios";
import {
  createDocumentsApi,
  buildTestSearchBody,
  SearchInputError,
} from "../api/documentsApi.ts";
import {
  buildSearchView,
  collapsePassage,
  formatSimilarity,
  SEARCH_MAX_QUERY_CHARS,
  DEFAULT_SEARCH_K,
} from "../utils/searchView.ts";
import { getDocumentError, getSearchInputMessage } from "../utils/documentErrors.ts";
import { getDocumentActions } from "../utils/documentPolicy.ts";
import type { TestSearchResponseItem } from "../schemas/documentSchemas.ts";

const clientSource = readFileSync(new URL("../../../api/client.ts", import.meta.url), "utf8");
const BASE_URL = /baseURL:\s*import\.meta\.env\.VITE_API_URL\s*\|\|\s*"([^"]+)"/.exec(clientSource)?.[1] ?? "";

function createMockAxios(handler: (config: InternalAxiosRequestConfig) => { status?: number; data: unknown }) {
  return axios.create({
    baseURL: BASE_URL,
    adapter: async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => {
      const result = handler(config);
      return { data: result.data, status: result.status ?? 200, statusText: "OK", headers: {}, config };
    },
  });
}

function hit(over: Record<string, unknown> = {}) {
  return {
    rank: 1,
    chunk_id: "c1",
    document_id: "d1",
    document_title: "Physics",
    version_id: "v1",
    version_no: 2,
    version_status: "published",
    page_start: 3,
    page_end: 4,
    heading_path: "Unit 1 > Light",
    content: "Light travels fast.",
    vector_rank: 1,
    vector_similarity: 0.9123,
    keyword_rank: null,
    keyword_matched_terms: null,
    fused_score: 0.0164,
    flagged: false,
    injection_flags: [],
    title_flagged: false,
    filename_flagged: false,
    ...over,
  };
}

function response(over: Partial<TestSearchResponseItem> = {}): TestSearchResponseItem {
  return {
    sufficient: true,
    reason: "similarity",
    message: null,
    threshold: 0.84,
    threshold_calibrated: true,
    keyword_coverage_threshold: null,
    model: "model-a",
    candidate_count: 40,
    hits: [hit()],
    ...over,
  } as TestSearchResponseItem;
}

// ---------------------------------------------------------------------------------------------
// Body builder (the ONE place that builds the search request body)
test("buildTestSearchBody: trims the question and sends only documented fields", () => {
  const body = buildTestSearchBody({
    query: "  What is light?  \n",
    classId: "c-1",
    subjectId: "s-1",
    includeDrafts: true,
    k: 3,
    // a mistaken extra field must never reach the body
    tenant_id: "t-evil",
    foo: "bar",
  } as never);
  assert.deepEqual(body, {
    query: "What is light?",
    class_id: "c-1",
    subject_id: "s-1",
    include_drafts: true,
    k: 3,
  });
  assert.ok(!("tenant_id" in body));
});

test("buildTestSearchBody: optional fields are left out when not given or empty", () => {
  assert.deepEqual(buildTestSearchBody({ query: "light" }), { query: "light" });
  assert.deepEqual(
    buildTestSearchBody({ query: "light", classId: "", subjectId: null, includeDrafts: false }),
    { query: "light" }
  );
});

test("buildTestSearchBody: include_drafts is sent only for the boolean true", () => {
  for (const value of ["true", 1, "yes", {}, [], null, undefined, false, new Boolean(true)]) {
    const body = buildTestSearchBody({ query: "q", includeDrafts: value as never }) as unknown as Record<string, unknown>;
    assert.ok(!("include_drafts" in body), `must not send include_drafts for ${String(value)}`);
  }
  assert.equal(buildTestSearchBody({ query: "q", includeDrafts: true }).include_drafts, true);
});

test("buildTestSearchBody: k is sent only as an integer from 1 to 10", () => {
  for (const k of [1, 6, 10]) assert.equal(buildTestSearchBody({ query: "q", k }).k, k);
  for (const k of [0, 11, -1, 2.5, NaN, "6", null, undefined, Infinity]) {
    const body = buildTestSearchBody({ query: "q", k: k as never }) as unknown as Record<string, unknown>;
    assert.ok(!("k" in body), `must not send k=${String(k)}`);
  }
  assert.equal(DEFAULT_SEARCH_K, 6);
});

test("buildTestSearchBody: empty or blank question throws invalid_query", () => {
  for (const q of ["", "   ", "\n\t"]) {
    assert.throws(
      () => buildTestSearchBody({ query: q }),
      (e: unknown) => e instanceof SearchInputError && e.code === "invalid_query"
    );
  }
  assert.throws(() => buildTestSearchBody({ query: 42 as never }), SearchInputError);
});

test("buildTestSearchBody: 500 characters after trimming is accepted, 501 throws query_too_long", () => {
  assert.equal(SEARCH_MAX_QUERY_CHARS, 500);
  const ok = buildTestSearchBody({ query: `  ${"a".repeat(500)}  ` });
  assert.equal(ok.query.length, 500);
  assert.throws(
    () => buildTestSearchBody({ query: "a".repeat(501) }),
    (e: unknown) => e instanceof SearchInputError && e.code === "query_too_long"
  );
  // the raw schema cap (4,000) is far above the soft limit; anything longer is refused too
  assert.throws(() => buildTestSearchBody({ query: "a".repeat(4001) }), SearchInputError);
});

// ---------------------------------------------------------------------------------------------
// API function: final URL, method and body through the real builder
test("createDocumentsApi: search is POST /documents/test-search (final URL) with the built body", async () => {
  let captured: InternalAxiosRequestConfig | undefined;
  const client = createMockAxios((config) => {
    captured = config;
    return { data: response() };
  });
  const api = createDocumentsApi(client);

  const result = await api.search({ query: "  light  ", subjectId: "s-1", includeDrafts: true, k: 4 });

  assert.ok(captured);
  assert.equal(captured.method?.toLowerCase(), "post");
  assert.equal(axios.getUri(captured), `${BASE_URL}/documents/test-search`);
  assert.deepEqual(JSON.parse(String(captured.data)), {
    query: "light",
    subject_id: "s-1",
    include_drafts: true,
    k: 4,
  });
  assert.equal(result.hits.length, 1);
  assert.equal(result.threshold_calibrated, true);
});

test("createDocumentsApi: search never puts the question in the URL or params", async () => {
  let captured: InternalAxiosRequestConfig | undefined;
  const client = createMockAxios((config) => {
    captured = config;
    return { data: response() };
  });
  await createDocumentsApi(client).search({ query: "secret question" });
  assert.ok(captured);
  assert.ok(!axios.getUri(captured).includes("secret"));
  assert.equal(captured.params, undefined);
});

test("createDocumentsApi: search refuses an unusable question before any request", async () => {
  let calls = 0;
  const client = createMockAxios(() => {
    calls += 1;
    return { data: response() };
  });
  const api = createDocumentsApi(client);
  await assert.rejects(() => api.search({ query: "   " }), SearchInputError);
  await assert.rejects(() => api.search({ query: "x".repeat(501) }), SearchInputError);
  assert.equal(calls, 0);
});

test("createDocumentsApi: search rejects an invalid response shape with SchemaMismatchError", async () => {
  const client = createMockAxios(() => ({ data: { nope: true } }));
  await assert.rejects(
    () => createDocumentsApi(client).search({ query: "q" }),
    (e: Error) => e.name === "SchemaMismatchError"
  );
});

test("createDocumentsApi: the API object has search, and search is not a mutation", () => {
  const api = createDocumentsApi({} as never);
  assert.ok(Object.keys(api).includes("search"));
});

// ---------------------------------------------------------------------------------------------
// Error mapping (existing table, search context)
test("search errors: every search code maps to its plain text, never the raw code or server text", () => {
  const mk = (status: number, detail: unknown, headers: Record<string, string> = {}) => ({
    isAxiosError: true,
    response: { status, data: { detail }, headers },
  });
  const cases: Array<[number, string, RegExp, string | undefined]> = [
    [422, "invalid_query", /at least one word/, "query"],
    [422, "query_too_long", /too long.*500/, "query"],
    [503, "search_unavailable", /Search is temporarily unavailable/, undefined],
    [503, "search_timeout", /took too long/, undefined],
    [503, "search_busy", /busy/, undefined],
    [503, "service_unavailable", /Search is temporarily unavailable/, undefined],
  ];
  for (const [status, code, re, field] of cases) {
    const info = getDocumentError(mk(status, { code, message: "SERVER-TEXT sql" }), "search");
    assert.match(info.message, re, code);
    assert.equal(info.field, field, code);
    assert.ok(!info.message.includes(code), `${code}: raw code leaked`);
    assert.ok(!info.message.includes("SERVER-TEXT"), `${code}: server text leaked`);
  }
});

test("search errors: 429 shows the wait, 404 names the scope, 401/403/5xx/network are plain", () => {
  const mk = (status: number, data: unknown, headers: Record<string, string> = {}) => ({
    isAxiosError: true,
    response: { status, data, headers },
  });
  const rate = getDocumentError(
    mk(429, { detail: { code: "test_search_rate_limit_exceeded", retry_after_seconds: 45 } }),
    "search"
  );
  assert.match(rate.message, /searched too often/);
  assert.match(rate.message, /45 seconds/);

  assert.match(getDocumentError(mk(404, { detail: { code: "not_found" } }), "search").message, /not one of yours/);
  assert.match(getDocumentError(mk(401, { detail: { code: "missing_token" } }), "search").message, /sign in again/);
  assert.equal(getDocumentError(mk(401, { detail: { code: "missing_token" } }), "search").action, "login");
  assert.match(getDocumentError(mk(403, { detail: { code: "permission_denied" } }), "search").message, /permission/);
  assert.match(getDocumentError(mk(500, { detail: "boom" }), "search").message, /went wrong on our side/);
  const network = getDocumentError({ isAxiosError: true, request: {}, message: "Network Error" }, "search");
  assert.match(network.message, /Network error/);
});

test("getSearchInputMessage: client-side input errors use the same table texts", () => {
  assert.match(getSearchInputMessage("invalid_query"), /at least one word/);
  assert.match(getSearchInputMessage("query_too_long"), /500 characters/);
});

// ---------------------------------------------------------------------------------------------
// Policy: who sees the panel
test("policy: test_search is visible only with document:view", () => {
  const find = (perms: string[]) =>
    getDocumentActions({ can: (p) => perms.includes(p) }).find((a) => a.id === "test_search");
  assert.equal(find(["document:view"])?.visible, true);
  assert.equal(find([])?.visible, false);
  assert.equal(find(["document:upload"])?.visible, false);
});

// ---------------------------------------------------------------------------------------------
// View model
test("formatSimilarity: three decimals, null/undefined/NaN show a dash, never 0", () => {
  assert.equal(formatSimilarity(0.9123), "0.912");
  assert.equal(formatSimilarity(0.5), "0.500");
  assert.equal(formatSimilarity(0), "0.000");
  for (const v of [null, undefined, NaN]) assert.equal(formatSimilarity(v as never), "-");
});

test("view: hits are ordered by rank even when the server order is shuffled", () => {
  const view = buildSearchView(
    response({ hits: [hit({ rank: 3, chunk_id: "c3" }), hit({ rank: 1, chunk_id: "c1" }), hit({ rank: 2, chunk_id: "c2" })] })
  );
  assert.deepEqual(view.hits.map((h) => h.rank), [1, 2, 3]);
});

test("view: banner text for every reason", () => {
  const banner = (over: Partial<TestSearchResponseItem>) => buildSearchView(response(over)).banner;
  assert.match(banner({ sufficient: true, reason: "similarity" }).text, /Enough matching material was found/);
  assert.match(banner({ sufficient: true, reason: "keyword_coverage" }).text, /Enough matching material was found/);
  const below = banner({ sufficient: false, reason: "below_threshold" });
  assert.match(below.text, /No passage was found that is similar enough to answer this question\./);
  assert.match(below.text, /not enough information/);
  assert.match(banner({ sufficient: false, reason: "no_candidates", hits: [] }).text, /No passage/);
  assert.match(banner({ sufficient: false, reason: "empty_scope", hits: [] }).text, /no published material/i);
  assert.match(banner({ sufficient: false, reason: "something_new", message: "Server says hi" }).text, /Server says hi/);
  assert.match(banner({ sufficient: false, reason: "something_new", message: null }).text, /\S/);
});

test("view: sufficient false still lists hits and says the assistant would not use them", () => {
  const view = buildSearchView(response({ sufficient: false, reason: "below_threshold", hits: [hit({ vector_similarity: 0.5 })] }));
  assert.equal(view.hits.length, 1);
  assert.equal(view.assistantWouldNotUse, true);
  assert.match(view.hits[0].thresholdText ?? "", /below the threshold/);
  assert.equal(buildSearchView(response()).assistantWouldNotUse, false);
});

test("view: similarity bands compare with the threshold; null similarity has no band text", () => {
  const view = buildSearchView(
    response({ hits: [hit({ rank: 1, vector_similarity: 0.84 }), hit({ rank: 2, vector_similarity: 0.839 }), hit({ rank: 3, vector_similarity: null })] })
  );
  assert.match(view.hits[0].thresholdText ?? "", /above the threshold/);
  assert.match(view.hits[1].thresholdText ?? "", /below the threshold/);
  assert.equal(view.hits[2].similarityText, "-");
  assert.equal(view.hits[2].thresholdText, null);
});

test("view: threshold line, candidate count, and the calibration notice only when the field says so", () => {
  const ok = buildSearchView(response({ threshold_calibrated: true }));
  assert.equal(ok.thresholdText, "Threshold 0.84");
  assert.equal(ok.candidateText, "Searched 40 passages.");
  assert.equal(ok.calibration.calibrated, true);
  assert.match(ok.calibration.text, /Calibrated for this model/);

  const bad = buildSearchView(response({ threshold_calibrated: false, model: "model-b" }));
  assert.equal(bad.calibration.calibrated, false);
  assert.match(bad.calibration.text, /Not calibrated for the current search model \(model-b\)/);
  assert.match(bad.calibration.text, /may mislead/);
  assert.match(bad.calibration.text, /recalibrated/);
  assert.ok(!/Calibrated for this model/.test(bad.calibration.text));
});

test("view: keyword line only when keyword_rank is not null; technical details always carry fused and vector rank", () => {
  const hidden = buildSearchView(response()).hits[0];
  assert.equal(hidden.keywordText, null);
  assert.ok(hidden.technical.some((t) => t.label === "Fused score"));
  assert.ok(hidden.technical.some((t) => t.label === "Vector rank"));

  const shown = buildSearchView(response({ hits: [hit({ keyword_rank: 2, keyword_matched_terms: 3 })] })).hits[0];
  assert.match(shown.keywordText ?? "", /2/);
  assert.match(shown.keywordText ?? "", /3/);
});

test("view: drafts are labelled Draft, published Published, unknown status gets no wrong chip", () => {
  const view = buildSearchView(
    response({
      hits: [
        hit({ rank: 1, version_status: "ready" }),
        hit({ rank: 2, version_status: "published" }),
        hit({ rank: 3, version_status: "weird" }),
      ],
    })
  );
  assert.equal(view.hits[0].statusLabel, "Draft");
  assert.equal(view.hits[0].isDraft, true);
  assert.equal(view.hits[1].statusLabel, "Published");
  assert.equal(view.hits[1].isDraft, false);
  assert.equal(view.hits[2].statusLabel, null);
});

test("view: page range, version label, heading and flag notes", () => {
  const h = buildSearchView(
    response({
      hits: [
        hit({ page_start: 3, page_end: 4, version_no: 2, heading_path: "A > B", flagged: true, injection_flags: ["role_markup"], title_flagged: true, filename_flagged: true }),
      ],
    })
  ).hits[0];
  assert.equal(h.pageText, "Pages 3-4");
  assert.equal(h.versionLabel, "Version 2");
  assert.equal(h.heading, "A > B");
  assert.deepEqual(h.flagLabels, ["Looks like chat-system markup"]);
  assert.equal(h.titleFlagged, true);
  assert.equal(h.filenameFlagged, true);
});

test("view: announcement counts passages or repeats the empty state", () => {
  assert.equal(buildSearchView(response({ hits: [hit()] })).announcement, "1 passage found");
  assert.equal(
    buildSearchView(response({ hits: [hit({ rank: 1 }), hit({ rank: 2 })] })).announcement,
    "2 passages found"
  );
  const empty = buildSearchView(response({ sufficient: false, reason: "below_threshold", hits: [] }));
  assert.match(empty.announcement, /No passage was found/);
});

test("collapsePassage: long or many-line text is cut to a preview, short text is not", () => {
  const short = collapsePassage("one\ntwo");
  assert.equal(short.isTruncated, false);
  assert.equal(short.preview, "one\ntwo");

  const lines = Array.from({ length: 20 }, (_, i) => `line ${i}`).join("\n");
  const many = collapsePassage(lines);
  assert.equal(many.isTruncated, true);
  assert.ok(many.preview.split("\n").length <= 6);

  const long = collapsePassage("a".repeat(5000));
  assert.equal(long.isTruncated, true);
  assert.ok(long.preview.length < 5000);
});
