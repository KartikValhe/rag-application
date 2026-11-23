import test from "node:test";
import assert from "node:assert/strict";
import axios, { type InternalAxiosRequestConfig, type AxiosResponse } from "axios";
import { createDocumentsApi } from "../api/documentsApi.ts";
import {
  DocumentUsageSchema,
  DocumentVersionListSchema,
  DocumentVersionSchema,
} from "../schemas/documentSchemas.ts";
import { formatChangedAt } from "../utils/versionView.ts";
import { getDocumentActions } from "../utils/documentPolicy.ts";
import { formatAttempts, getVersionStatusRows, NOT_AVAILABLE_NOW } from "../utils/usageView.ts";

function mockClient(handler: (config: InternalAxiosRequestConfig) => unknown) {
  return axios.create({
    baseURL: "/api/v1",
    adapter: async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => ({
      data: handler(config),
      status: 200,
      statusText: "OK",
      headers: {},
      config,
    }),
  });
}

const VERSION = {
  id: "v-1",
  document_id: "d-1",
  version_no: 1,
  status: "archived",
  file_kind: "pdf",
  original_filename: "a.pdf",
  size_bytes: 10,
  created_at: "2026-11-20T10:00:00Z",
  updated_at: "2026-11-21T11:30:00Z",
};

// ---------- API ----------

test("versions: GET /api/v1/documents/{id}/versions (final URL), no body, no params", async () => {
  let captured: InternalAxiosRequestConfig | undefined;
  const api = createDocumentsApi(
    mockClient((config) => {
      captured = config;
      return { items: [VERSION], has_more: false };
    })
  );
  const result = await api.versions("d-1");
  assert.ok(captured);
  assert.equal(captured.method?.toLowerCase(), "get");
  assert.equal(axios.getUri(captured), "/api/v1/documents/d-1/versions");
  assert.equal(captured.data, undefined);
  assert.equal(captured.params, undefined);
  assert.equal(result.items.length, 1);
  assert.equal(result.has_more, false);
});

// ---------- schemas ----------

test("version schema: updated_at present, missing and null all parse", () => {
  assert.equal(DocumentVersionSchema.parse(VERSION).updated_at, "2026-11-21T11:30:00Z");
  const { updated_at: _unused, ...without } = VERSION;
  assert.equal(DocumentVersionSchema.parse(without).updated_at, undefined);
  assert.equal(DocumentVersionSchema.parse({ ...VERSION, updated_at: null }).updated_at, null);
});

test("version list schema: unknown fields are accepted; a missing or non-boolean has_more means false", () => {
  const parsed = DocumentVersionListSchema.parse({ items: [{ ...VERSION, future: 1 }], extra: true });
  assert.equal(parsed.items.length, 1);
  assert.equal(parsed.has_more, false);
  assert.equal(DocumentVersionListSchema.parse({ items: [], has_more: true }).has_more, true);
  assert.equal(DocumentVersionListSchema.parse({ items: [], has_more: "yes" }).has_more, false);
});

test("usage schema: new fields parse; missing or null values become null, never 0", () => {
  const full = DocumentUsageSchema.parse({
    documents: 1,
    storage_bytes: 2,
    pages: 3,
    limits: {},
    versions_by_status: { queued: 1, processing: 2, ready: 3, published: 4, archived: 5, failed: 0 },
    uploads_last_hour: { tenant: 7, caller: 2, window_seconds: 3600, future: 1 },
  });
  assert.equal(full.versions_by_status.failed, 0);
  assert.equal(full.versions_by_status.archived, 5);
  assert.equal(full.uploads_last_hour.caller, 2);
  assert.equal(full.uploads_last_hour.tenant, 7);

  const nulls = DocumentUsageSchema.parse({ uploads_last_hour: { tenant: null, caller: null } });
  assert.equal(nulls.uploads_last_hour.tenant, null);
  assert.equal(nulls.uploads_last_hour.caller, null);

  const empty = DocumentUsageSchema.parse({});
  assert.equal(empty.uploads_last_hour.tenant, null);
  assert.equal(empty.uploads_last_hour.caller, null);
  for (const key of ["queued", "processing", "ready", "published", "archived", "failed"] as const) {
    assert.equal(empty.versions_by_status[key], null);
  }
  const junk = DocumentUsageSchema.parse({ uploads_last_hour: "x", versions_by_status: [1] });
  assert.equal(junk.uploads_last_hour.caller, null);
});

// ---------- date formatting ----------

test("formatChangedAt: a valid value gives plain words with year and time", () => {
  const text = formatChangedAt("2026-11-21T11:30:00Z");
  assert.ok(text);
  assert.match(text, /2026/);
  assert.match(text, /Nov/);
  assert.doesNotMatch(text, /T11:30|Z$/);
});

test("formatChangedAt: null, undefined, empty and invalid values give null (never Invalid Date or NaN)", () => {
  for (const bad of [null, undefined, "", "not a date", "2026-13-45T99:99:99Z", 5 as unknown as string]) {
    assert.equal(formatChangedAt(bad), null);
  }
});

// ---------- policy ----------

test("policy: an archived version has no publish, retry, delete or review action, even for an admin", () => {
  const archived = { ...VERSION, status: "archived", retryable: true, flagged_chunk_count: 3, warnings: [] };
  const document = {
    id: "d-1",
    title: "T",
    subject: { id: "s", name: "S" },
    classes: [],
    created_by: "u",
    created_at: "x",
    updated_at: "x",
    latest_version: { ...archived, id: "v-2", version_no: 2, status: "ready" },
    published_version: null,
    can_manage: true,
  };
  const actions = getDocumentActions({
    can: () => true,
    document: document as never,
    version: archived as never,
  });
  for (const id of ["publish", "retry", "delete_version", "review_flags"]) {
    const action = actions.find((a) => a.id === id);
    assert.ok(action, id);
    assert.equal(action.visible, false, `${id} must be hidden for an archived version`);
    assert.equal(action.enabled, false, `${id} must be disabled for an archived version`);
  }
});

// ---------- usage view ----------

test("formatAttempts: null is 'Not available right now', never 0", () => {
  assert.equal(NOT_AVAILABLE_NOW, "Not available right now");
  assert.equal(formatAttempts(null, 20).text, "Not available right now");
  assert.equal(formatAttempts(undefined, 20).text, "Not available right now");
  assert.equal(formatAttempts(Number.NaN, 20).text, "Not available right now");
  assert.notEqual(formatAttempts(null, 20).text, "0");
});

test("formatAttempts: a real zero is shown as 0", () => {
  assert.equal(formatAttempts(0, 20).text, "0 of 20");
});

test("formatAttempts: N of LIMIT, and a zero or missing limit never divides or shows NaN", () => {
  assert.equal(formatAttempts(5, 20).text, "5 of 20");
  assert.equal(formatAttempts(1200, 5000).text, "1,200 of 5,000");
  for (const max of [0, null, undefined, Number.NaN]) {
    const result = formatAttempts(5, max);
    assert.equal(result.text, "5");
    assert.equal(result.levelText, "");
    assert.doesNotMatch(result.text, /NaN|Infinity|undefined/);
  }
});

test("formatAttempts: at or over the limit says so in words", () => {
  assert.equal(formatAttempts(20, 20).levelText, "Limit reached");
  assert.equal(formatAttempts(3, 20).levelText, "Within limit");
});

test("getVersionStatusRows: six rows in plain words; null shows 'Not available right now', a zero shows 0", () => {
  const rows = getVersionStatusRows({
    queued: 1,
    processing: 0,
    ready: 3,
    published: 4,
    archived: null,
    failed: 6,
  });
  assert.deepEqual(
    rows.map((r) => r.label),
    ["Waiting", "Processing", "Draft", "Published", "Older", "Failed"]
  );
  assert.deepEqual(
    rows.map((r) => r.text),
    ["1", "0", "3", "4", "Not available right now", "6"]
  );
});
