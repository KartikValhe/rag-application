import test from "node:test";
import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import axios, { type InternalAxiosRequestConfig, type AxiosResponse } from "axios";
import { createDocumentsApi, buildUploadFormData } from "../api/documentsApi.ts";
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

test("buildUploadFormData: builds real FormData with file, title, subject_id, repeated class_ids and no tenant", () => {
  const fakeFile = new File(["content"], "notes.pdf", { type: "application/pdf" });
  const payload = {
    file: fakeFile,
    title: "Chapter 1 Notes",
    subject_id: "sub-1",
    class_ids: ["cls-a", "cls-b", "cls-c"],
  };

  const formData = buildUploadFormData(payload);

  assert.ok(formData instanceof FormData);
  assert.ok(formData.get("file") instanceof File);
  assert.equal((formData.get("file") as File).name, "notes.pdf");
  assert.equal(formData.get("title"), "Chapter 1 Notes");
  assert.equal(formData.get("subject_id"), "sub-1");

  // class_ids repeated, one entry each, no comma-joined or JSON value
  const classIds = formData.getAll("class_ids");
  assert.deepEqual(classIds, ["cls-a", "cls-b", "cls-c"]);
  assert.equal(classIds.length, 3);
  assert.notEqual(formData.get("class_ids"), '["cls-a","cls-b","cls-c"]');
  assert.notEqual(formData.get("class_ids"), "cls-a,cls-b,cls-c");

  // No tenant field
  assert.equal(formData.get("tenant"), null);
});

test("createDocumentsApi: upload sends exact multipart body to POST /documents with 10-minute timeout", async () => {
  let capturedConfig: InternalAxiosRequestConfig | undefined;
  const mockClient = createMockAxios((config) => {
    capturedConfig = config;
    return {
      status: 202,
      data: {
        id: "v-new",
        document_id: "doc-new",
        version_no: 1,
        status: "queued",
        stage: null,
        error_code: null,
        file_kind: "pdf",
        original_filename: "test.pdf",
        size_bytes: 12345,
        page_count: null,
        chunk_count: null,
        flagged_chunk_count: null,
        created_at: "2026-11-21T10:00:00Z",
        published_at: null,
        retryable: null,
        warnings: ["duplicate_content"],
      },
    };
  });

  const api = createDocumentsApi(mockClient);

  const fakeFile = new File(["content"], "test.pdf", { type: "application/pdf" });
  const payload = {
    file: fakeFile,
    title: "Lecture 1 Notes",
    subject_id: "sub-101",
    class_ids: ["cls-1", "cls-2"],
  };

  const abortController = new AbortController();
  let progressReported: number | null = null;

  const result = await api.upload(payload, {
    onProgress: (p: number) => {
      progressReported = p;
    },
    signal: abortController.signal,
  });

  assert.ok(capturedConfig);
  assert.equal(capturedConfig.method?.toLowerCase(), "post");
  assert.equal(capturedConfig.url, "/documents");
  assert.equal(axios.getUri(capturedConfig), "/api/v1/documents");
  assert.equal(capturedConfig.timeout, 600000, "Must have 10-minute timeout (600,000 ms)");
  assert.equal(capturedConfig.signal, abortController.signal);

  // Verify onProgress callback triggers correctly
  capturedConfig.onUploadProgress?.({ loaded: 50, total: 100 } as any);
  assert.equal(progressReported, 50);

  // Validate exact multipart body
  const sentForm = capturedConfig.data as FormData;
  assert.ok(sentForm instanceof FormData);
  assert.ok(sentForm.get("file"));
  assert.equal(sentForm.get("title"), "Lecture 1 Notes");
  assert.equal(sentForm.get("subject_id"), "sub-101");
  assert.deepEqual(sentForm.getAll("class_ids"), ["cls-1", "cls-2"]);
  assert.equal(sentForm.get("tenant"), null, "No tenant field must be sent");

  // Validate response
  assert.equal(result.id, "v-new");
  assert.equal(result.status, "queued");
  assert.deepEqual(result.warnings, ["duplicate_content"]);
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
