import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MaterialDetailPage } from "../pages/MaterialDetailPage";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentItem, DocumentStatusItem } from "../schemas/documentSchemas";
import * as AuthContextModule from "@/features/auth/AuthContext";
import { TeacherRoute } from "@/features/auth/guards";
import { getVersionFailure } from "../utils/documentErrors";

vi.mock("@/features/branding", () => ({
  useBranding: () => ({
    branding: {
      tenant_id: "t1",
      name: "Acme Academy",
      logo_url: null,
      favicon_url: null,
    },
    isLoading: false,
    isCustomTheme: false,
    tenantSlug: "acme",
    refreshBranding: vi.fn(),
  }),
}));

vi.mock("@/theme/ThemeProvider", () => ({
  useTheme: () => ({
    theme: "light",
    setTheme: vi.fn(),
    resolvedTheme: "light",
  }),
}));

function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
      },
    },
  });
}

function createMockDetailDoc(overrides: Partial<DocumentItem> = {}): DocumentItem {
  return {
    id: "doc-1",
    title: "Operating Systems",
    description: "Lecture notes and labs",
    subject: { id: "sub-1", name: "Computer Science" },
    classes: [
      { id: "cls-1", name: "Class 10A" },
      { id: "cls-2", name: "Class 10B" },
    ],
    created_by: "u-1",
    created_at: "2026-11-20T10:00:00Z",
    updated_at: "2026-11-20T10:00:00Z",
    latest_version: {
      id: "v-1",
      document_id: "doc-1",
      version_no: 1,
      status: "processing",
      stage: "parsing",
      error_code: null,
      file_kind: "pdf",
      original_filename: "os_notes.pdf",
      size_bytes: 2097152,
      page_count: 24,
      chunk_count: 50,
      flagged_chunk_count: null,
      created_at: "2026-11-20T10:00:00Z",
      published_at: null,
      retryable: null,
      warnings: [],
    },
    published_version: null,
    can_manage: true,
    blocked_reason: null,
    ...overrides,
  };
}

describe("MaterialDetailPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
      user: {
        id: "teacher-1",
        isAdmin: false,
        roles: ["teacher"],
        permissions: ["document:view", "document:upload"],
      },
      isAuthenticated: true,
      isLoading: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
    });
  });

  it("a processing version becomes ready without reload", async () => {
    const doc = createMockDetailDoc();
    const readyStatus: DocumentStatusItem = {
      document_id: "doc-1",
      version_id: "v-1",
      version_no: 1,
      status: "ready",
      stage: null,
      error_code: null,
      retryable: false,
      updated_at: "2026-11-20T10:02:00Z",
    };

    let statusCallCount = 0;
    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      publishVersion: vi.fn(),
      search: vi.fn(),
      deleteVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn().mockImplementation(() => {
        if (statusCallCount > 0) {
          return Promise.resolve({
            ...doc,
            latest_version: {
              ...doc.latest_version!,
              status: "ready",
              stage: null,
              flagged_chunk_count: 0,
            },
          });
        }
        return Promise.resolve(doc);
      }),
      limits: vi.fn(),
      status: vi.fn().mockImplementation(() => {
        statusCallCount++;
        return Promise.resolve(readyStatus);
      }),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials/doc-1"]}>
          <Routes>
            <Route
              path="/teacher/materials/:documentId"
              element={<MaterialDetailPage api={fakeApi} responsiveLayout="scroll" />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    // Initial state: shows "Reading file"
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Operating Systems", level: 1 })).toBeDefined();
      expect(screen.getByText("Reading file")).toBeDefined();
    });

    // After polling resolves ready: transitions to "Draft, ready to publish"
    await waitFor(() => {
      expect(screen.getByText("Draft, ready to publish")).toBeDefined();
    });
  });

  it("polling stops on terminal states", async () => {
    const readyDoc = createMockDetailDoc({
      latest_version: {
        id: "v-ready",
        document_id: "doc-1",
        version_no: 1,
        status: "ready",
        stage: null,
        error_code: null,
        file_kind: "pdf",
        original_filename: "ready.pdf",
        size_bytes: 100,
        page_count: 5,
        chunk_count: 10,
        flagged_chunk_count: 0,
        created_at: "2026-11-20T10:00:00Z",
        published_at: null,
        retryable: null,
        warnings: [],
      },
    });

    const statusMock = vi.fn();
    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      publishVersion: vi.fn(),
      search: vi.fn(),
      deleteVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn().mockResolvedValue(readyDoc),
      limits: vi.fn(),
      status: statusMock,
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials/doc-1"]}>
          <Routes>
            <Route
              path="/teacher/materials/:documentId"
              element={<MaterialDetailPage api={fakeApi} responsiveLayout="scroll" />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText("Draft, ready to publish")).toBeDefined();
    });

    // Terminal state should not trigger polling
    expect(statusMock).not.toHaveBeenCalled();
  });

  it("a lost connection keeps the last state and says so", async () => {
    const doc = createMockDetailDoc();
    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      publishVersion: vi.fn(),
      search: vi.fn(),
      deleteVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn().mockResolvedValue(doc),
      limits: vi.fn(),
      status: vi.fn().mockRejectedValue(new Error("Network Error")),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials/doc-1"]}>
          <Routes>
            <Route
              path="/teacher/materials/:documentId"
              element={<MaterialDetailPage api={fakeApi} responsiveLayout="scroll" />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText("Reading file")).toBeDefined();
      expect(screen.getByText(/connection lost, retrying/i)).toBeDefined();
    });
  });

  it("every failed-version reason shows its plain text", async () => {
    const errorCodes = [
      "processing_failed",
      "worker_crash",
      "job_time_limit_exceeded",
      "scanned_pdf",
      "corrupt_pdf",
      "password_protected_pdf",
      "pdf_too_complex",
      "parse_timeout",
    ];

    for (const code of errorCodes) {
      const expectedMsg = getVersionFailure(code).message;
      const failedDoc = createMockDetailDoc({
        latest_version: {
          id: `v-fail-${code}`,
          document_id: "doc-1",
          version_no: 1,
          status: "failed",
          stage: null,
          error_code: code,
          file_kind: "pdf",
          original_filename: "failed.pdf",
          size_bytes: 100,
          created_at: "2026-11-20T10:00:00Z",
          warnings: [],
        },
      });

      const fakeApi: DocumentsApi = {
        retryVersion: vi.fn(),
        uploadVersion: vi.fn(),
        deleteDocument: vi.fn(),
        publishVersion: vi.fn(),
        search: vi.fn(),
        deleteVersion: vi.fn(),
      flags: vi.fn(),
        list: vi.fn(),
        get: vi.fn().mockResolvedValue(failedDoc),
        limits: vi.fn(),
        status: vi.fn(),
        upload: vi.fn(),
      };

      const { unmount } = render(
        <QueryClientProvider client={createTestQueryClient()}>
          <MemoryRouter initialEntries={["/teacher/materials/doc-1"]}>
            <Routes>
              <Route
                path="/teacher/materials/:documentId"
                element={<MaterialDetailPage api={fakeApi} responsiveLayout="scroll" />}
              />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      );

      await waitFor(() => {
        expect(screen.getByText(expectedMsg)).toBeDefined();
      });

      unmount();
    }
  });

  it("404 shows NotFoundState", async () => {
    const notFoundError = {
      isAxiosError: true,
      response: {
        status: 404,
        data: { detail: { code: "not_found", message: "Resource not found." } },
      },
    };

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      publishVersion: vi.fn(),
      search: vi.fn(),
      deleteVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn().mockRejectedValue(notFoundError),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials/doc-999"]}>
          <Routes>
            <Route
              path="/teacher/materials/:documentId"
              element={<MaterialDetailPage api={fakeApi} responsiveLayout="scroll" />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText(/404/i)).toBeDefined();
    });
  });

  it("a student cannot reach the route", async () => {
    vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
      user: {
        id: "student-1",
        isAdmin: false,
        roles: ["student"],
        permissions: [],
      },
      isAuthenticated: true,
      isLoading: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
    });

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      publishVersion: vi.fn(),
      search: vi.fn(),
      deleteVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials/doc-1"]}>
          <Routes>
            <Route
              path="/teacher/materials/:documentId"
              element={
                <TeacherRoute permission="document:view">
                  <MaterialDetailPage api={fakeApi} responsiveLayout="scroll" />
                </TeacherRoute>
              }
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText(/403/i)).toBeDefined();
    });
  });

  it("an HTML-looking title and file name render as literal text", async () => {
    const maliciousDoc = createMockDetailDoc({
      title: "<img src=x onerror=alert(1)>",
      latest_version: {
        id: "v-xss",
        document_id: "doc-1",
        version_no: 1,
        status: "ready",
        stage: null,
        error_code: null,
        file_kind: "pdf",
        original_filename: "<script>alert('xss')<b>.pdf",
        size_bytes: 100,
        created_at: "2026-11-20T10:00:00Z",
        warnings: [],
      },
    });

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      publishVersion: vi.fn(),
      search: vi.fn(),
      deleteVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn().mockResolvedValue(maliciousDoc),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials/doc-1"]}>
          <Routes>
            <Route
              path="/teacher/materials/:documentId"
              element={<MaterialDetailPage api={fakeApi} responsiveLayout="scroll" />}
            />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "<img src=x onerror=alert(1)>", level: 1 })).toBeDefined();
      expect(screen.getByText("<script>alert('xss')<b>.pdf")).toBeDefined();
      expect(document.querySelector("img[src='x']")).toBeNull();
      expect(document.querySelector("script")).toBeNull();
    });
  });
});
