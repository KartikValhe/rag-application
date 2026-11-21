import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MaterialsListPage } from "../pages/MaterialsListPage";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentItem } from "../schemas/documentSchemas";
import { academicApi, type MyClassItem } from "@/features/academic/api/academicApi";
import * as AuthContextModule from "@/features/auth/AuthContext";
import { TeacherRoute } from "@/features/auth/guards";

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

// Helper to create test query client
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

function createMockDoc(overrides: Partial<DocumentItem> = {}): DocumentItem {
  return {
    id: "doc-1",
    title: "Intro to Algorithms",
    description: "Syllabus and lecture notes",
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
      status: "ready",
      stage: null,
      error_code: null,
      file_kind: "pdf",
      original_filename: "algorithms.pdf",
      size_bytes: 1048576,
      page_count: 10,
      chunk_count: 20,
      flagged_chunk_count: 0,
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

const mockClasses: MyClassItem[] = [
  {
    id: "cls-1",
    name: "Class 10A",
    kind: "standard",
    subjects: [
      { id: "sub-1", name: "Computer Science" },
      { id: "sub-2", name: "Mathematics" },
    ],
  },
  {
    id: "cls-2",
    name: "Class 10B",
    kind: "standard",
    subjects: [{ id: "sub-1", name: "Computer Science" }],
  },
];


// The default layout is stacked cards (phones) plus a hidden desktop table: both are in the DOM,
// so content assertions look at the card list only.
function cardList(): HTMLElement {
  const el = document.querySelector<HTMLElement>("div.block.md\\:hidden");
  if (!el) throw new Error("stacked card list not found");
  return el;
}
function cards() {
  return within(cardList());
}

describe("MaterialsListPage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(academicApi, "getMyClasses").mockResolvedValue(mockClasses);
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

  it("renders page header and list with filters for teacher", async () => {
    const doc = createMockDoc();
    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      list: vi.fn().mockResolvedValue({
        items: [doc],
        next_cursor: null,
      }),
      get: vi.fn(),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials"]}>
          <MaterialsListPage api={fakeApi} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    expect(screen.getByRole("heading", { name: /course materials/i })).toBeDefined();
    await waitFor(() => {
      expect(cards().getByText("Intro to Algorithms")).toBeDefined();
      expect(cards().getByText("Computer Science")).toBeDefined();
      expect(cards().getByText("Draft, ready to publish")).toBeDefined();
    });
  });

  it("renders status chips for different version statuses, stages, and dual published version", async () => {
    const docs: DocumentItem[] = [
      createMockDoc({
        id: "doc-queued",
        title: "Queued Doc",
        latest_version: {
          id: "v-q",
          document_id: "doc-queued",
          version_no: 1,
          status: "queued",
          file_kind: "pdf",
          original_filename: "q.pdf",
          size_bytes: 100,
          created_at: "2026-11-20T10:00:00Z",
          warnings: [],
        },
      }),
      createMockDoc({
        id: "doc-parsing",
        title: "Parsing Doc",
        latest_version: {
          id: "v-p",
          document_id: "doc-parsing",
          version_no: 1,
          status: "processing",
          stage: "parsing",
          file_kind: "pdf",
          original_filename: "p.pdf",
          size_bytes: 100,
          created_at: "2026-11-20T10:00:00Z",
          warnings: [],
        },
      }),
      createMockDoc({
        id: "doc-flagged",
        title: "Flagged Doc",
        latest_version: {
          id: "v-f",
          document_id: "doc-flagged",
          version_no: 1,
          status: "ready",
          flagged_chunk_count: 3,
          file_kind: "pdf",
          original_filename: "f.pdf",
          size_bytes: 100,
          created_at: "2026-11-20T10:00:00Z",
          warnings: [],
        },
      }),
      createMockDoc({
        id: "doc-unscanned",
        title: "Unscanned Doc",
        latest_version: {
          id: "v-u",
          document_id: "doc-unscanned",
          version_no: 1,
          status: "ready",
          flagged_chunk_count: null,
          file_kind: "pdf",
          original_filename: "u.pdf",
          size_bytes: 100,
          created_at: "2026-11-20T10:00:00Z",
          warnings: [],
        },
      }),
      createMockDoc({
        id: "doc-dual",
        title: "Dual Version Doc",
        latest_version: {
          id: "v-2",
          document_id: "doc-dual",
          version_no: 2,
          status: "ready",
          flagged_chunk_count: 0,
          file_kind: "pdf",
          original_filename: "v2.pdf",
          size_bytes: 100,
          created_at: "2026-11-20T10:00:00Z",
          warnings: [],
        },
        published_version: {
          id: "v-1",
          document_id: "doc-dual",
          version_no: 1,
          status: "published",
          file_kind: "pdf",
          original_filename: "v1.pdf",
          size_bytes: 100,
          created_at: "2026-11-19T10:00:00Z",
          warnings: [],
        },
      }),
    ];

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      list: vi.fn().mockResolvedValue({ items: docs, next_cursor: null }),
      get: vi.fn(),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials"]}>
          <MaterialsListPage api={fakeApi} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(cards().getByText("Waiting")).toBeDefined();
      expect(cards().getByText("Reading file")).toBeDefined();
      expect(cards().getByText("Draft, needs review (3 passages)")).toBeDefined();
      expect(cards().getByText("Draft, not checked yet")).toBeDefined();
      expect(cards().getByText("Published: v1")).toBeDefined();
    });
  });

  it("handles Load more pagination following next_cursor", async () => {
    const user = userEvent.setup();
    const page1Doc = createMockDoc({ id: "doc-1", title: "Page 1 Doc" });
    const page2Doc = createMockDoc({ id: "doc-2", title: "Page 2 Doc" });

    const listMock = vi.fn().mockImplementation(({ cursor }) => {
      if (!cursor) {
        return Promise.resolve({
          items: [page1Doc],
          next_cursor: "cursor-page-2",
        });
      }
      return Promise.resolve({
        items: [page2Doc],
        next_cursor: null,
      });
    });

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      list: listMock,
      get: vi.fn(),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials"]}>
          <MaterialsListPage api={fakeApi} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(cards().getByText("Page 1 Doc")).toBeDefined();
    });

    const loadMoreButton = screen.getByRole("button", { name: /load more/i });
    expect(loadMoreButton).toBeDefined();

    await user.click(loadMoreButton);

    await waitFor(() => {
      expect(listMock).toHaveBeenCalledWith(expect.objectContaining({ cursor: "cursor-page-2" }));
      expect(cards().getByText("Page 2 Doc")).toBeDefined();
    });
  });

  it("shows empty state when no documents exist", async () => {
    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      list: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      get: vi.fn(),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials"]}>
          <MaterialsListPage api={fakeApi} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText("No materials yet")).toBeDefined();
    });
  });

  it("shows filtered empty state with clear filters button", async () => {
    const user = userEvent.setup();
    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      list: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      get: vi.fn(),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials?status=published"]}>
          <MaterialsListPage api={fakeApi} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText("No materials match")).toBeDefined();
    });

    const clearBtn = screen.getByRole("button", { name: /clear filters/i });
    await user.click(clearBtn);
  });

  it("shows empty state when teacher has no assigned classes", async () => {
    vi.spyOn(academicApi, "getMyClasses").mockResolvedValue([]);
    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      list: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      get: vi.fn(),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials"]}>
          <MaterialsListPage api={fakeApi} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(screen.getByText(/no assigned classes/i)).toBeDefined();
    });
  });

  it("shows can_manage=false lock note with plain text", async () => {
    const lockedDoc = createMockDoc({
      can_manage: false,
      blocked_reason: "document_manage_requires_all_classes",
    });

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      list: vi.fn().mockResolvedValue({ items: [lockedDoc], next_cursor: null }),
      get: vi.fn(),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials"]}>
          <MaterialsListPage api={fakeApi} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(
        cards().getByText(/You teach this subject in only some of the linked classes, so you can only view it/i)
      ).toBeDefined();
    });
  });

  it("renders untrusted text like <img src=x onerror=alert(1)> as literal text, never HTML", async () => {
    const maliciousDoc = createMockDoc({
      title: "<img src=x onerror=alert(1)>",
      latest_version: {
        id: "v-malicious",
        document_id: "doc-1",
        version_no: 1,
        status: "ready",
        file_kind: "pdf",
        original_filename: "<script>alert('xss')</script>.pdf",
        size_bytes: 100,
        created_at: "2026-11-20T10:00:00Z",
        warnings: [],
      },
    });

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      list: vi.fn().mockResolvedValue({ items: [maliciousDoc], next_cursor: null }),
      get: vi.fn(),
      limits: vi.fn(),
      status: vi.fn(),
      upload: vi.fn(),
    };

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials"]}>
          <MaterialsListPage api={fakeApi} />
        </MemoryRouter>
      </QueryClientProvider>
    );

    await waitFor(() => {
      expect(cards().getByText("<img src=x onerror=alert(1)>")).toBeDefined();
      // Ensure no img tag exists in the document
      expect(document.querySelector("img[src='x']")).toBeNull();
    });
  });

  it("student user is blocked and receives 403 ForbiddenState", async () => {
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

    render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials"]}>
          <Routes>
            <Route
              path="/teacher/materials"
              element={
                <TeacherRoute permission="document:view">
                  <MaterialsListPage />
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

  describe("5f-5 leftover A: stacked card layout on phones", () => {
    async function renderList(
      docs: DocumentItem[],
      perms = ["document:view", "document:upload"],
      waitFor_ = "Intro to Algorithms"
    ) {
      vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
        user: { id: "teacher-1", isAdmin: false, roles: ["teacher"], permissions: perms },
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
        deleteVersion: vi.fn(),
        list: vi.fn().mockResolvedValue({ items: docs, next_cursor: null }),
        get: vi.fn(),
        limits: vi.fn(),
        status: vi.fn(),
        upload: vi.fn(),
      };
      const utils = render(
        <QueryClientProvider client={createTestQueryClient()}>
          <MemoryRouter initialEntries={["/teacher/materials"]}>
            <Routes>
              <Route path="/teacher/materials" element={<MaterialsListPage api={fakeApi} />} />
              <Route path="/teacher/materials/:documentId" element={<div>DETAIL ROUTE</div>} />
            </Routes>
          </MemoryRouter>
        </QueryClientProvider>
      );
      await waitFor(() => expect(cards().getByText(waitFor_)).toBeDefined());
      return utils;
    }

    it("renders the stacked card list on phones and keeps the wide table hidden below md (no horizontal scroll at 360 px)", async () => {
      await renderList([createMockDoc()]);
      // phone cards are visible below md only
      expect(cardList().classList.contains("block")).toBe(true);
      expect(cardList().classList.contains("md:hidden")).toBe(true);
      // the wide table sits in a wrapper that is display:none below md
      const table = document.querySelector("table") as HTMLElement;
      const wrapper = table.parentElement as HTMLElement;
      expect(wrapper.classList.contains("hidden")).toBe(true);
      expect(wrapper.classList.contains("md:block")).toBe(true);
      // nothing on the phone side scrolls sideways
      expect(cardList().className).not.toMatch(/overflow-x-(auto|scroll)/);
      expect(cardList().querySelector(".overflow-x-auto")).toBeNull();
    });

    it("each card has a footer link at least 44 px high that opens the material; destructive actions are not offered here", async () => {
      await renderList([createMockDoc()], ["document:view", "document:upload", "document:delete"]);
      const link = cards().getByRole("link", { name: /open/i });
      expect(link.className).toContain("min-h-[44px]");
      expect(link.getAttribute("href")).toBe("/teacher/materials/doc-1");
      expect(cards().queryByRole("button", { name: /delete/i })).toBeNull();
    });

    it("the Open link is reachable by keyboard and Enter opens the material once", async () => {
      const user = userEvent.setup();
      await renderList([createMockDoc()]);
      const link = cards().getByRole("link", { name: /open/i });
      link.focus();
      expect(document.activeElement).toBe(link);
      await user.keyboard("{Enter}");
      expect(await screen.findByText("DETAIL ROUTE")).toBeDefined();
    });

    it("long titles and subjects wrap instead of widening the card", async () => {
      const long = "A".repeat(150);
      await renderList([createMockDoc({ title: long, subject: { id: "s", name: "B".repeat(120) } })], undefined, long);
      expect(cards().getByText(long).className).toContain("break-words");
      expect(cards().getByText("B".repeat(120)).className).toContain("break-words");
    });

    it("the Open link comes from the policy: without document:view it is not rendered (the title link is not the only way in)", async () => {
      await renderList([createMockDoc()], []);
      expect(cards().queryByRole("link", { name: /open/i })).toBeNull();
    });

    it("shows both chips when a draft is waiting and an older version stays published", async () => {
      const waiting = createMockDoc({
        latest_version: {
          ...createMockDoc().latest_version!,
          id: "v-3",
          version_no: 3,
          status: "queued",
        },
        published_version: {
          ...createMockDoc().latest_version!,
          id: "v-2",
          version_no: 2,
          status: "published",
        },
      });
      await renderList([waiting]);
      expect(cards().getByText("Waiting")).toBeDefined();
      expect(cards().getByText("Published: v2")).toBeDefined();
    });
  });
});
