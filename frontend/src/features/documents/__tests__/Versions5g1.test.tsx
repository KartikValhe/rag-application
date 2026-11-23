import { describe, it, expect, vi, beforeEach } from "vitest";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { render, screen, waitFor, within, configure } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MaterialDetailPage } from "../pages/MaterialDetailPage";
import { ToastProvider } from "@/components/ui/Toast";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentItem, DocumentVersionItem } from "../schemas/documentSchemas";
import { getDocumentError } from "../utils/documentErrors";
import * as AuthContextModule from "@/features/auth/AuthContext";

// Nothing here depends on a timer: every wait is a waitFor with a long limit.
configure({ asyncUtilTimeout: 20000 });
vi.setConfig({ testTimeout: 60000, hookTimeout: 60000 });

vi.mock("@/features/branding", () => ({
  useBranding: () => ({
    branding: { tenant_id: "t1", name: "Acme Academy", logo_url: null, favicon_url: null },
    isLoading: false,
    isCustomTheme: false,
    tenantSlug: "acme",
    refreshBranding: vi.fn(),
  }),
}));

vi.mock("@/theme/ThemeProvider", () => ({
  useTheme: () => ({ theme: "light", setTheme: vi.fn(), resolvedTheme: "light" }),
}));

const NOW = "2026-11-20T10:00:00Z";
const CHANGED = "2026-11-21T11:30:00Z";

function ver(o: Partial<DocumentVersionItem> = {}): DocumentVersionItem {
  return {
    id: "v-3",
    document_id: "doc-1",
    version_no: 3,
    status: "published",
    stage: null,
    error_code: null,
    file_kind: "pdf",
    original_filename: "unit3.pdf",
    size_bytes: 1000,
    page_count: 3,
    chunk_count: 3,
    flagged_chunk_count: 0,
    created_at: NOW,
    updated_at: CHANGED,
    published_at: NOW,
    retryable: null,
    warnings: [],
    ...o,
  };
}

const V3_PUBLISHED = ver();
const V2_ARCHIVED = ver({
  id: "v-2",
  version_no: 2,
  status: "archived",
  original_filename: "unit2.pdf",
  published_at: NOW,
  retryable: true,
  flagged_chunk_count: 2,
});

function makeDoc(latest: DocumentVersionItem, published: DocumentVersionItem | null = latest): DocumentItem {
  return {
    id: "doc-1",
    title: "Chapter 4 notes",
    description: null,
    subject: { id: "s1", name: "Computer Science" },
    classes: [{ id: "c1", name: "Class 10A" }],
    created_by: "u1",
    created_at: NOW,
    updated_at: NOW,
    latest_version: latest,
    published_version: published,
    can_manage: true,
    blocked_reason: null,
  };
}

const list = (items: DocumentVersionItem[], has_more = false) => ({ items, has_more });

function makeApi() {
  return {
    list: vi.fn(),
    get: vi.fn(),
    versions: vi.fn(() => new Promise<never>(() => undefined)),
    limits: vi.fn().mockResolvedValue({
      max_file_bytes: 50 * 1024 * 1024,
      max_pdf_pages: 200,
      max_pptx_slides: 100,
      uploads_per_user_per_hour: 20,
      uploads_per_tenant_per_hour: 100,
      accepted_extensions: ["pdf", "docx", "pptx", "txt", "md"],
    }),
    status: vi.fn(),
    flags: vi.fn().mockResolvedValue({
      document_id: "doc-1",
      version_id: "v-3",
      version_no: 3,
      version_status: "ready",
      flagged_chunk_count: 0,
      flags_acknowledged: false,
      title_flagged: false,
      filename_flagged: false,
      items: [],
      next_after_ordinal: null,
    }),
    upload: vi.fn(),
    uploadVersion: vi.fn(),
    retryVersion: vi.fn(),
    publishVersion: vi.fn(),
    usage: vi.fn(),
    deleteVersion: vi.fn(),
    deleteDocument: vi.fn(),
    search: vi.fn(),
  } as unknown as DocumentsApi & {
    get: ReturnType<typeof vi.fn>;
    versions: ReturnType<typeof vi.fn>;
    status: ReturnType<typeof vi.fn>;
    uploadVersion: ReturnType<typeof vi.fn>;
    retryVersion: ReturnType<typeof vi.fn>;
    publishVersion: ReturnType<typeof vi.fn>;
    deleteVersion: ReturnType<typeof vi.fn>;
  };
}

const ALL = ["document:view", "document:upload", "document:delete", "document:publish"];

function setUser(permissions: string[] = ALL) {
  vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
    user: { id: "u1", isAdmin: false, roles: ["teacher"], permissions },
    isAuthenticated: true,
    isLoading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  } as never);
}

// A brand new client each time: a render is a fresh page load, like a browser reload.
function renderPage(api: DocumentsApi, layout: "stacked" | "scroll" = "scroll") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/teacher/materials/doc-1"]}>
          <Routes>
            <Route
              path="/teacher/materials/:documentId"
              element={<MaterialDetailPage api={api} responsiveLayout={layout} />}
            />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

const rowOf = (versionNo: number) => screen.getByText(`v${versionNo}`).closest("tr") as HTMLElement;

function httpError(status: number, code?: string) {
  return {
    isAxiosError: true,
    response: { status, headers: {}, data: { detail: code ? { code, message: "RAW SERVER TEXT 42" } : "RAW SERVER TEXT 42" } },
  };
}

beforeEach(() => {
  vi.restoreAllMocks();
  setUser();
  Element.prototype.scrollIntoView = vi.fn();
});

describe("5g-1 versions list: what the page shows", () => {
  it("asks for the list of THIS document and shows every listed version, newest first", async () => {
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(V3_PUBLISHED));
    api.versions.mockResolvedValue(
      list([V3_PUBLISHED, ver({ id: "v-2", version_no: 2, status: "archived" }), ver({ id: "v-1", version_no: 1, status: "archived" })])
    );
    renderPage(api);
    await waitFor(() => expect(screen.getAllByText(/^v\d$/)).toHaveLength(3));
    expect(api.versions).toHaveBeenCalledWith("doc-1");
    expect(screen.getAllByText(/^v\d$/).map((el) => el.textContent)).toEqual(["v3", "v2", "v1"]);
  });

  it("RELOAD: a page rendered fresh against a server that returns an archived version shows it as 'Older version'", async () => {
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(V3_PUBLISHED));
    api.versions.mockResolvedValue(list([V3_PUBLISHED, V2_ARCHIVED]));
    renderPage(api);
    await waitFor(() => expect(within(rowOf(2)).getByText("Older version")).toBeTruthy());
    expect(within(rowOf(3)).getByText("Published")).toBeTruthy();
  });

  it("an archived row has no Publish, Try again, Delete version or Review flags button (even with every permission)", async () => {
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(V3_PUBLISHED));
    api.versions.mockResolvedValue(list([V3_PUBLISHED, V2_ARCHIVED]));
    renderPage(api);
    await waitFor(() => expect(within(rowOf(2)).getByText("Older version")).toBeTruthy());
    expect(within(rowOf(2)).queryAllByRole("button")).toHaveLength(0);
  });

  it("has_more true shows 'Showing the newest 100 versions.'; false does not", async () => {
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(V3_PUBLISHED));
    api.versions.mockResolvedValue(list([V3_PUBLISHED], true));
    const first = renderPage(api);
    expect(await screen.findByText("Showing the newest 100 versions.")).toBeTruthy();
    first.unmount();

    const api2 = makeApi();
    api2.get.mockResolvedValue(makeDoc(V3_PUBLISHED));
    api2.versions.mockResolvedValue(list([V3_PUBLISHED], false));
    renderPage(api2);
    await screen.findByText("v3");
    expect(screen.queryByText("Showing the newest 100 versions.")).toBeNull();
  });

  it("the client invents no row: a version the list does not contain is not shown (no 'replaced version' memory)", async () => {
    const api = makeApi();
    const user = userEvent.setup();
    const ready = ver({ id: "v-3", status: "ready", published_at: null, flagged_chunk_count: 0 });
    const v2published = ver({ id: "v-2", version_no: 2, status: "published", original_filename: "unit2.pdf" });
    let published = false;
    api.get.mockImplementation(async () => (published ? makeDoc(V3_PUBLISHED) : makeDoc(ready, v2published)));
    // After publishing the server list no longer contains v2 at all
    api.versions.mockImplementation(async () => (published ? list([V3_PUBLISHED]) : list([ready, v2published])));
    api.publishVersion.mockImplementation(async () => {
      published = true;
      return V3_PUBLISHED;
    });
    renderPage(api);
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(within(rowOf(3)).getByText("Published")).toBeTruthy());
    await waitFor(() => expect(screen.queryByText("v2")).toBeNull());
  });

  it("source: MaterialDetailPage no longer keeps a replaced-version memory", () => {
    const source = readFileSync(join(process.cwd(), "src/features/documents/pages/MaterialDetailPage.tsx"), "utf8");
    expect(source).not.toMatch(/replacedVersions|setReplacedVersions/);
  });
});

describe("5g-1 last changed", () => {
  it("shows 'Last changed <date and time>' in plain words, never raw ISO text", async () => {
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(V3_PUBLISHED));
    api.versions.mockResolvedValue(list([V3_PUBLISHED]));
    renderPage(api);
    const line = await screen.findByText(/^Last changed /);
    expect(line.textContent).toMatch(/2026/);
    expect(line.textContent).not.toMatch(/T11:30|Z\b/);
  });

  it.each([["null", null], ["missing", undefined], ["invalid", "garbage-date"], ["empty", ""]])(
    "a %s updated_at shows nothing (no 'Last changed', no Invalid Date, no NaN)",
    async (_name, value) => {
      const api = makeApi();
      const v = ver({ updated_at: value as string | null | undefined });
      api.get.mockResolvedValue(makeDoc(v));
      api.versions.mockResolvedValue(list([v]));
      renderPage(api);
      await screen.findByText("v3");
      await waitFor(() => expect(api.versions).toHaveBeenCalled());
      expect(screen.queryByText(/Last changed/)).toBeNull();
      expect(document.body.textContent ?? "").not.toMatch(/Invalid Date|NaN/);
    }
  );

  it("on a phone (stacked cards) the card carries the line too", async () => {
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(V3_PUBLISHED));
    api.versions.mockResolvedValue(list([V3_PUBLISHED]));
    renderPage(api, "stacked");
    await waitFor(() => expect(screen.getAllByText(/^Last changed /).length).toBeGreaterThan(0));
  });
});

describe("5g-1 the list is refetched", () => {
  it("after a successful publish", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    const ready = ver({ id: "v-3", status: "ready", published_at: null });
    api.get.mockResolvedValue(makeDoc(ready, null));
    api.versions.mockResolvedValue(list([ready]));
    api.publishVersion.mockResolvedValue(V3_PUBLISHED);
    renderPage(api);
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    const dialog = await screen.findByRole("dialog");
    const before = api.versions.mock.calls.length;
    await user.click(within(dialog).getByRole("button", { name: "Publish" }));
    await waitFor(() => expect(api.publishVersion).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.versions.mock.calls.length).toBeGreaterThan(before));
  });

  it("after a successful retry", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    const failed = ver({
      id: "v-3",
      status: "failed",
      error_code: "processing_failed",
      retryable: true,
      published_at: null,
    });
    api.get.mockResolvedValue(makeDoc(failed, null));
    api.versions.mockResolvedValue(list([failed]));
    // the number of list requests at the moment the server accepted the retry
    let callsAtRetry = -1;
    api.retryVersion.mockImplementation(async () => {
      callsAtRetry = api.versions.mock.calls.length;
      return ver({ id: "v-3", status: "queued", published_at: null });
    });
    api.status.mockResolvedValue({
      document_id: "doc-1",
      version_id: "v-3",
      version_no: 3,
      status: "queued",
      stage: null,
      error_code: null,
      retryable: false,
      updated_at: NOW,
    });
    renderPage(api);
    await user.click(await screen.findByRole("button", { name: "Try again" }));
    await waitFor(() => expect(api.retryVersion).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.versions.mock.calls.length).toBeGreaterThan(callsAtRetry));
  });

  it("after a successful version delete (and the deleted row disappears)", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    const draft = ver({ id: "v-3", status: "ready", published_at: null, flagged_chunk_count: 0 });
    let deleted = false;
    api.get.mockImplementation(async () => makeDoc(deleted ? V2_ARCHIVED : draft, null));
    api.versions.mockImplementation(async () => (deleted ? list([V2_ARCHIVED]) : list([draft, V2_ARCHIVED])));
    api.deleteVersion.mockImplementation(async () => {
      deleted = true;
    });
    renderPage(api);
    await waitFor(() => expect(within(rowOf(3)).getByRole("button", { name: "Delete version" })).toBeTruthy());
    await user.click(within(rowOf(3)).getByRole("button", { name: "Delete version" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Delete version" }));
    await waitFor(() => expect(api.deleteVersion).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByText("v3")).toBeNull());
  });

  it("after a successful new version upload", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(V3_PUBLISHED));
    api.versions.mockResolvedValue(list([V3_PUBLISHED]));
    api.uploadVersion.mockResolvedValue(ver({ id: "v-4", version_no: 4, status: "queued", published_at: null }));
    renderPage(api);
    await user.click(await screen.findByRole("button", { name: "New version" }));
    const dialog = await screen.findByRole("dialog");
    const input = dialog.querySelector("input[type='file']") as HTMLInputElement;
    await user.upload(input, new File(["content"], "next.pdf", { type: "application/pdf" }));
    const before = api.versions.mock.calls.length;
    await user.click(within(dialog).getByRole("button", { name: /^upload$/i }));
    await waitFor(() => expect(api.uploadVersion).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(api.versions.mock.calls.length).toBeGreaterThan(before));
  });

  it("when a polled status reaches a terminal status", async () => {
    const api = makeApi();
    const processing = ver({ id: "v-3", status: "processing", stage: "parsing", published_at: null });
    let done = false;
    api.get.mockImplementation(async () =>
      makeDoc(done ? ver({ id: "v-3", status: "ready", published_at: null }) : processing, null)
    );
    api.versions.mockImplementation(async () =>
      list([done ? ver({ id: "v-3", status: "ready", published_at: null }) : processing])
    );
    let callsAtTerminal = -1;
    api.status.mockImplementation(async () => {
      callsAtTerminal = api.versions.mock.calls.length;
      done = true;
      return {
        document_id: "doc-1",
        version_id: "v-3",
        version_no: 3,
        status: "ready",
        stage: null,
        error_code: null,
        retryable: false,
        updated_at: CHANGED,
      };
    });
    renderPage(api);
    await screen.findByText("v3");
    await waitFor(() => expect(api.status).toHaveBeenCalled());
    await waitFor(() => expect(api.versions.mock.calls.length).toBeGreaterThan(callsAtTerminal));
  });
});

describe("5g-1 list errors go through the error table", () => {
  it.each([404, 403])("a %s on the list shows the table's text, never the server's raw text", async (status) => {
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(V3_PUBLISHED));
    const err = httpError(status, status === 403 ? "forbidden" : undefined);
    api.versions.mockRejectedValue(err);
    renderPage(api);
    const expected = getDocumentError(err, "list").message;
    expect(expected.length).toBeGreaterThan(0);
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(expected);
    expect(document.body.textContent ?? "").not.toContain("RAW SERVER TEXT 42");
  });
});
