import test from "node:test";
import assert from "node:assert/strict";
import axios, { type InternalAxiosRequestConfig, type AxiosResponse } from "axios";
import { createDocumentsApi } from "../api/documentsApi.ts";
import { SCHEMA_MISMATCH_NAME } from "../../../api/errorMessages.ts";

function createMockAxios(handler: (config: InternalAxiosRequestConfig) => { status?: number; data: unknown }) {
  return axios.create({
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

test("createDocumentsApi: list calls GET /api/v1/documents with query params", async () => {
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
  assert.equal(capturedConfig.url, "/api/v1/documents");
  assert.equal(capturedConfig.params?.class_id, "c1");
  assert.equal(capturedConfig.params?.status, "published");
  assert.equal(result.items.length, 1);
  assert.equal(result.items[0].title, "Physics Notes");
  assert.equal(result.next_cursor, "next-abc");
});

test("createDocumentsApi: get calls GET /api/v1/documents/:id", async () => {
  let capturedUrl = "";
  const mockClient = createMockAxios((config) => {
    capturedUrl = config.url ?? "";
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

  assert.equal(capturedUrl, "/api/v1/documents/doc-42");
  assert.equal(result.id, "doc-42");
  assert.equal(result.can_manage, false);
  assert.equal(result.blocked_reason, "document_manage_requires_all_classes");
});

test("createDocumentsApi: limits calls GET /api/v1/documents/limits", async () => {
  let capturedUrl = "";
  const mockClient = createMockAxios((config) => {
    capturedUrl = config.url ?? "";
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

  assert.equal(capturedUrl, "/api/v1/documents/limits");
  assert.equal(result.max_file_bytes, 52428800);
  assert.deepEqual(result.accepted_extensions, ["docx", "md", "pdf", "pptx", "txt"]);
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
