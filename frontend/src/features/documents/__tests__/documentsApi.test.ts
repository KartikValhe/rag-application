import test from "node:test";
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import axios, { type InternalAxiosRequestConfig, type AxiosResponse } from "axios";
import { createDocumentsApi } from "../api/documentsApi.ts";
import { SCHEMA_MISMATCH_NAME } from "../../../api/errorMessages.ts";

// The real client cannot be imported under node (it uses import.meta.env), so read
// its default baseURL straight from the source file. Stays in sync automatically.
const clientSource = readFileSync(new URL("../../../api/client.ts", import.meta.url), "utf8");
const BASE_URL = /baseURL:\s*import\.meta\.env\.VITE_API_URL\s*\|\|\s*"([^"]+)"/.exec(clientSource)?.[1] ?? "";

function createMockAxios(handler: (config: InternalAxiosRequestConfig) => { status?: number; data: unknown }) {
  return axios.create({
    baseURL: BASE_URL,
    adapter: async (config: InternalAxiosRequestConfig): Promise<AxiosResponse> => {
      const result = handler(config);
      return {
        data: result.data,
        status: result.status ?? 200,
        statusText: "OK",
        headers: {},
        config,
      };
    },
  });
}

test("createDocumentsApi: list calls GET /documents with query params", async () => {
  let capturedConfig: InternalAxiosRequestConfig | undefined;
  const mockClient = createMockAxios((config) => {
    capturedConfig = config;
    return {
      data: {
        items: [
          {
            id: "doc-1",
            title: "Physics Notes",
            description: null,
            subject: { id: "s1", name: "Physics" },
            classes: [{ id: "c1", name: "Class 10" }],
            created_by: "u1",
            created_at: "2026-11-20T10:00:00Z",
            updated_at: "2026-11-20T10:00:00Z",
            latest_version: null,
            published_version: null,
            can_manage: true,
            blocked_reason: null,
          },
        ],
        next_cursor: "next-abc",
      },
    };
  });

  const api = createDocumentsApi(mockClient);
  const result = await api.list({ class_id: "c1", status: "published", limit: 20 });

  assert.ok(capturedConfig);
  assert.equal(capturedConfig.url, "/documents");
  assert.equal(axios.getUri(capturedConfig), "/api/v1/documents?class_id=c1&status=published&limit=20");
  assert.equal(capturedConfig.params?.class_id, "c1");
  assert.equal(capturedConfig.params?.status, "published");
  assert.equal(result.items.length, 1);
  assert.equal(result.items[0].title, "Physics Notes");
  assert.equal(result.next_cursor, "next-abc");
});

test("createDocumentsApi: get calls GET /documents/:id", async () => {
  let capturedUrl = "";
  let finalUrl = "";
  const mockClient = createMockAxios((config) => {
    capturedUrl = config.url ?? "";
    finalUrl = axios.getUri(config);
    return {
      data: {
        id: "doc-42",
        title: "Chemistry Notes",
        description: "Organic chemistry chapter",
        subject: { id: "s2", name: "Chemistry" },
        classes: [{ id: "c2", name: "Class 12" }],
        created_by: "u2",
        created_at: "2026-11-20T10:00:00Z",
        updated_at: "2026-11-20T10:00:00Z",
        latest_version: null,
        published_version: null,
        can_manage: false,
        blocked_reason: "document_manage_requires_all_classes",
      },
    };
  });

  const api = createDocumentsApi(mockClient);
  const result = await api.get("doc-42");

  assert.equal(capturedUrl, "/documents/doc-42");
  assert.equal(finalUrl, "/api/v1/documents/doc-42");
  assert.equal(result.id, "doc-42");
  assert.equal(result.can_manage, false);
  assert.equal(result.blocked_reason, "document_manage_requires_all_classes");
});

test("createDocumentsApi: limits calls GET /documents/limits", async () => {
  let capturedUrl = "";
  let finalUrl = "";
  const mockClient = createMockAxios((config) => {
    capturedUrl = config.url ?? "";
    finalUrl = axios.getUri(config);
    return {
      data: {
        max_file_bytes: 52428800,
        max_pdf_pages: 500,
        max_pptx_slides: 200,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: ["docx", "md", "pdf", "pptx", "txt"],
      },
    };
  });

  const api = createDocumentsApi(mockClient);
  const result = await api.limits();

  assert.equal(capturedUrl, "/documents/limits");
  assert.equal(finalUrl, "/api/v1/documents/limits");
  assert.equal(result.max_file_bytes, 52428800);
  assert.deepEqual(result.accepted_extensions, ["docx", "md", "pdf", "pptx", "txt"]);
});

test("createDocumentsApi: status calls GET /documents/:docId/versions/:vid/status", async () => {
  let capturedUrl = "";
  let finalUrl = "";
  const mockClient = createMockAxios((config) => {
    capturedUrl = config.url ?? "";
    finalUrl = axios.getUri(config);
    return {
      data: {
        document_id: "doc-123",
        version_id: "v-456",
        version_no: 2,
        status: "processing",
        stage: "chunking",
        error_code: null,
        retryable: false,
        updated_at: "2026-11-20T10:00:00Z",
      },
    };
  });

  const api = createDocumentsApi(mockClient);
  const result = await api.status("doc-123", "v-456");

  assert.equal(capturedUrl, "/documents/doc-123/versions/v-456/status");
  assert.equal(finalUrl, "/api/v1/documents/doc-123/versions/v-456/status");
  assert.equal(result.document_id, "doc-123");
  assert.equal(result.version_id, "v-456");
  assert.equal(result.status, "processing");
  assert.equal(result.stage, "chunking");
  assert.equal(result.retryable, false);
});

test("createDocumentsApi: throws SchemaMismatchError if response shape is invalid", async () => {
  const mockClient = createMockAxios(() => {
    return {
      data: {
        // missing required fields for limits
        invalid_key: true,
      },
    };
  });

  const api = createDocumentsApi(mockClient);
  await assert.rejects(
    async () => {
      await api.limits();
    },
    (err: unknown) => {
      const error = err as Error;
      return error.name === SCHEMA_MISMATCH_NAME;
    }
  );
});

test("documentsApi: baseURL is read from the real client", () => {
  assert.equal(BASE_URL, "/api/v1");
});

test("documentsApi: every API function requests a path relative to baseURL", async () => {
  const seen: string[] = [];
  const mockClient = createMockAxios((config) => {
    seen.push(config.url ?? "");
    const final = axios.getUri(config);
    assert.ok(!final.includes("/api/v1/api/v1"), `doubled prefix: ${final}`);
    assert.ok(final.startsWith("/api/v1/"), `wrong final url: ${final}`);
    return { data: {} };
  });
  const api = createDocumentsApi(mockClient);
  for (const [name, fn] of Object.entries(api)) {
    const args = Array.from({ length: fn.length }, () => "id-1");
    // Responses are not valid payloads; only the requested path matters here.
    await (fn as (...a: unknown[]) => Promise<unknown>)(...args).catch(() => undefined);
    assert.ok(seen.length > 0, `${name} made no request`);
  }
  for (const path of seen) {
    assert.ok(!path.startsWith("/api"), `path starts with /api: ${path}`);
    assert.ok(!path.includes("//"), `path contains //: ${path}`);
    assert.ok(path.startsWith("/"), `path must start with /: ${path}`);
  }
});
