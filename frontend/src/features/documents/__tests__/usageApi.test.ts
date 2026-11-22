import test from "node:test";
import assert from "node:assert/strict";
import axios, { type InternalAxiosRequestConfig, type AxiosResponse } from "axios";
import { createDocumentsApi } from "../api/documentsApi.ts";
import { DocumentUsageSchema, validateResponse } from "../schemas/documentSchemas.ts";
import { SCHEMA_MISMATCH_NAME } from "../../../api/errorMessages.ts";

function createMockAxios(handler: (config: InternalAxiosRequestConfig) => { status?: number; data: unknown }) {
  return axios.create({
    baseURL: "/api/v1",
    adapter: async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => {
      const result = handler(config);
      return { data: result.data, status: result.status ?? 200, statusText: "OK", headers: {}, config };
    },
  });
}

const LIMITS = {
  max_file_bytes: 10485760,
  max_pdf_pages: 500,
  max_pptx_slides: 200,
  max_documents: 200,
  max_storage_bytes: 1073741824,
  max_pages: 10000,
  uploads_per_user_per_hour: 20,
  uploads_per_tenant_per_hour: 100,
};
const FULL = { documents: 120, storage_bytes: 5242880, pages: 3400, limits: LIMITS };

test("usage: GET /api/v1/admin/documents/usage (final URL), no body, no params", async () => {
  let captured: InternalAxiosRequestConfig | undefined;
  const api = createDocumentsApi(
    createMockAxios((config) => {
      captured = config;
      return { data: FULL };
    })
  );
  const result = await api.usage();
  assert.ok(captured);
  assert.equal(captured.method?.toLowerCase(), "get");
  assert.equal(axios.getUri(captured), "/api/v1/admin/documents/usage");
  assert.equal(captured.data, undefined);
  assert.equal(captured.params, undefined);
  assert.equal(result.documents, 120);
  assert.equal(result.storage_bytes, 5242880);
  assert.equal(result.pages, 3400);
  assert.equal(result.limits.max_documents, 200);
  assert.equal(result.limits.uploads_per_tenant_per_hour, 100);
});

test("usage: the guard over the exported API object lists usage as a function that makes a request", async () => {
  const seen: string[] = [];
  const api = createDocumentsApi(
    createMockAxios((config) => {
      seen.push(`${config.method?.toUpperCase()} ${axios.getUri(config)}`);
      return { data: FULL };
    })
  );
  assert.ok(Object.keys(api).includes("usage"));
  assert.equal(typeof api.usage, "function");
  await api.usage();
  assert.deepEqual(seen, ["GET /api/v1/admin/documents/usage"]);
});

test("usage schema: unknown extra fields (top level and inside limits) are accepted and dropped", () => {
  const parsed = DocumentUsageSchema.parse({
    ...FULL,
    brand_new_field: { a: 1 },
    limits: { ...LIMITS, future_limit: 5 },
  });
  assert.equal(parsed.documents, 120);
  assert.equal(parsed.limits.max_pages, 10000);
});

test("usage schema: every missing number becomes null, never undefined or NaN", () => {
  const parsed = DocumentUsageSchema.parse({});
  assert.equal(parsed.documents, null);
  assert.equal(parsed.storage_bytes, null);
  assert.equal(parsed.pages, null);
  for (const key of Object.keys(LIMITS)) {
    assert.equal((parsed.limits as Record<string, unknown>)[key], null, key);
  }
});

test("usage schema: one missing limit key does not blank the rest", () => {
  const { max_pages: _omit, ...rest } = LIMITS;
  const parsed = DocumentUsageSchema.parse({ ...FULL, limits: rest });
  assert.equal(parsed.limits.max_pages, null);
  assert.equal(parsed.limits.max_documents, 200);
});

test("usage schema: explicit nulls and a null limits object are tolerated", () => {
  const nulls = DocumentUsageSchema.parse({ documents: null, storage_bytes: null, pages: null, limits: null });
  assert.equal(nulls.documents, null);
  assert.equal(nulls.limits.max_documents, null);
  const nullLimit = DocumentUsageSchema.parse({ ...FULL, limits: { ...LIMITS, max_documents: null } });
  assert.equal(nullLimit.limits.max_documents, null);
});

test("usage schema: NaN, Infinity, negatives, strings and booleans become null; zero stays zero", () => {
  const parsed = DocumentUsageSchema.parse({
    documents: Number.NaN,
    storage_bytes: Number.POSITIVE_INFINITY,
    pages: -3,
    limits: { ...LIMITS, max_documents: "200", max_pages: true, max_file_bytes: 0 },
  });
  assert.equal(parsed.documents, null);
  assert.equal(parsed.storage_bytes, null);
  assert.equal(parsed.pages, null);
  assert.equal(parsed.limits.max_documents, null);
  assert.equal(parsed.limits.max_pages, null);
  assert.equal(parsed.limits.max_file_bytes, 0);
  assert.equal(DocumentUsageSchema.parse({ documents: 0 }).documents, 0);
});

test("usage schema: a fractional count is rounded down to a whole number", () => {
  assert.equal(DocumentUsageSchema.parse({ documents: 12.9 }).documents, 12);
});

test("usage schema: a response that is not an object is a schema mismatch", () => {
  for (const bad of [null, "text", 5, [], undefined]) {
    assert.throws(
      () => validateResponse(DocumentUsageSchema, bad, "usage"),
      (e: Error) => e.name === SCHEMA_MISMATCH_NAME,
      `payload ${JSON.stringify(bad)}`
    );
  }
});

test("usage: api.usage rejects a non-object response with the schema mismatch name", async () => {
  const api = createDocumentsApi(createMockAxios(() => ({ data: "<html>" })));
  await assert.rejects(
    () => api.usage(),
    (e: Error) => e.name === SCHEMA_MISMATCH_NAME
  );
});
