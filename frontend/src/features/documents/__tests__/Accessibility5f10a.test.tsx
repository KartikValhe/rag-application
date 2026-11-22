import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within, configure } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MaterialDetailPage } from "../pages/MaterialDetailPage";
import { MaterialsListPage } from "../pages/MaterialsListPage";
import { ToastProvider } from "@/components/ui/Toast";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentItem, DocumentVersionItem } from "../schemas/documentSchemas";
import { academicApi } from "@/features/academic/api/academicApi";
import * as AuthContextModule from "@/features/auth/AuthContext";

// Slow machines: no assertion depends on a timer; async queries wait up to 20 s.
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

function makeVersion(o: Partial<DocumentVersionItem> = {}): DocumentVersionItem {
  return {
    id: "v-3",
    document_id: "doc-1",
    version_no: 3,
    status: "ready",
    stage: null,
    error_code: null,
    file_kind: "pdf",
    original_filename: "unit3.pdf",
    size_bytes: 1000,
    page_count: 3,
    chunk_count: 3,
    flagged_chunk_count: 0,
    created_at: NOW,
    published_at: null,
    retryable: null,
    warnings: [],
    ...o,
  };
}

const V2 = makeVersion({ id: "v-2", version_no: 2, status: "published", published_at: NOW, original_filename: "unit2.pdf" });

function makeDoc(o: Partial<DocumentItem> = {}): DocumentItem {
  return {
    id: "doc-1",
    title: "Chapter 4 notes",
    description: "Lecture notes",
    subject: { id: "sub-1", name: "Computer Science" },
    classes: [
      { id: "cls-1", name: "Class 10A" },
      { id: "cls-2", name: "Class 10B" },
    ],
    created_by: "u1",
    created_at: NOW,
    updated_at: NOW,
    latest_version: makeVersion(),
    published_version: V2,
    can_manage: true,
    blocked_reason: null,
    ...o,
  };
}

const LIMITS = {
  max_file_bytes: 50 * 1024 * 1024,
  max_pdf_pages: 200,
  max_pptx_slides: 100,
  uploads_per_user_per_hour: 20,
  uploads_per_tenant_per_hour: 100,
  accepted_extensions: ["pdf", "docx", "pptx", "txt", "md"],
};

function makeApi(doc: DocumentItem = makeDoc(), over: Record<string, unknown> = {}) {
  return {
    list: vi.fn().mockResolvedValue({ items: [doc], next_cursor: null }),
    get: vi.fn().mockResolvedValue(doc),
    limits: vi.fn().mockResolvedValue(LIMITS),
    status: vi.fn().mockResolvedValue({
      document_id: "doc-1",
      version_id: "v-3",
      version_no: 3,
      status: "ready",
      stage: null,
      error_code: null,
      retryable: null,
      updated_at: NOW,
    }),
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
    usage: vi.fn(),
    publishVersion: vi.fn(),
    deleteVersion: vi.fn(),
    deleteDocument: vi.fn(),
    search: vi.fn(),
    ...over,
  } as unknown as DocumentsApi;
}

const ALL = ["document:view", "document:upload", "document:delete", "document:publish"];

function setUser(permissions: string[], roles: string[] = ["teacher"]) {
  vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
    user: { id: "u1", isAdmin: false, roles, permissions },
    isAuthenticated: true,
    isLoading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  } as never);
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } } });
}

function renderDetail(api: DocumentsApi, layout?: "stacked" | "scroll") {
  return render(
    <QueryClientProvider client={client()}>
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

function renderList(api: DocumentsApi, path = "/teacher/materials") {
  return render(
    <QueryClientProvider client={client()}>
      <ToastProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/teacher/materials" element={<MaterialsListPage api={api} />} />
            <Route path="/teacher/materials/:documentId" element={<div>DETAIL ROUTE</div>} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

const MY_CLASSES = [
  {
    id: "cls-1",
    name: "Class 10A",
    kind: "standard",
    subjects: [
      { id: "sub-1", name: "Computer Science" },
      { id: "sub-2", name: "Mathematics" },
    ],
  },
  { id: "cls-2", name: "Class 10B", kind: "standard", subjects: [{ id: "sub-1", name: "Computer Science" }] },
];

beforeEach(() => {
  vi.restoreAllMocks();
  vi.spyOn(academicApi, "getMyClasses").mockResolvedValue(MY_CLASSES as never);
  setUser(ALL);
  Element.prototype.scrollIntoView = vi.fn();
});

// ---- change 1: every document dialog is named by its visible title ------------------------------
describe("5f-10a change 1: document dialogs have an accessible name", () => {
  it("Upload material", async () => {
    const user = userEvent.setup();
    renderList(makeApi());
    await user.click(await screen.findByRole("button", { name: /upload material/i }));
    expect(await screen.findByRole("dialog", { name: "Upload material" })).toBeDefined();
  });

  it("Upload new version", async () => {
    const user = userEvent.setup();
    // New version is offered when nothing is waiting: the latest version is the published one
    renderDetail(makeApi(makeDoc({ latest_version: V2 })), "scroll");
    await user.click(await screen.findByRole("button", { name: /new version/i }));
    expect(await screen.findByRole("dialog", { name: "Upload new version" })).toBeDefined();
  });

  it("Publish version", async () => {
    const user = userEvent.setup();
    renderDetail(makeApi(), "scroll");
    await user.click(await screen.findByRole("button", { name: "Publish" }));
    expect(await screen.findByRole("dialog", { name: "Publish version 3?" })).toBeDefined();
  });

  it("Delete document (confirm dialog)", async () => {
    const user = userEvent.setup();
    renderDetail(makeApi(), "scroll");
    await user.click(await screen.findByRole("button", { name: "Delete document" }));
    expect(await screen.findByRole("dialog", { name: "Delete 'Chapter 4 notes'?" })).toBeDefined();
  });

  it("Delete version (confirm dialog)", async () => {
    const user = userEvent.setup();
    renderDetail(makeApi(), "scroll");
    await user.click(await screen.findByRole("button", { name: "Delete version" }));
    expect(await screen.findByRole("dialog", { name: "Delete version 3 (unit3.pdf)?" })).toBeDefined();
  });
});

// ---- change 2: no interactive element inside another interactive element ----------------------
const INTERACTIVE =
  'a[href], button, input:not([type="hidden"]), select, textarea, summary, ' +
  '[role="button"], [role="link"], [role="checkbox"], [role="tab"], [role="menuitem"], [role="switch"], [role="radio"]';

function nestedInteractive(root: ParentNode): string[] {
  const bad: string[] = [];
  root.querySelectorAll<HTMLElement>(INTERACTIVE).forEach((el) => {
    const outer = el.parentElement?.closest<HTMLElement>(INTERACTIVE);
    if (outer) {
      bad.push(
        `<${el.tagName.toLowerCase()} "${(el.textContent ?? "").trim().slice(0, 30)}"> inside <${outer.tagName.toLowerCase()} role=${outer.getAttribute("role")}>`
      );
    }
  });
  return bad;
}

function phoneCards(): HTMLElement {
  return document.querySelector<HTMLElement>("div.block.md\\:hidden") as HTMLElement;
}

describe("5f-10a change 2: no nested interactive controls", () => {
  it("the checker itself catches a link inside role=button (so a green result means something)", () => {
    const host = document.createElement("div");
    host.innerHTML = '<div role="button" tabindex="0"><a href="/x">x</a></div>';
    expect(nestedInteractive(host)).toHaveLength(1);
  });

  it("list page (phone cards and desktop table): nothing interactive inside anything interactive", async () => {
    renderList(makeApi());
    await screen.findAllByText("Chapter 4 notes");
    expect(nestedInteractive(document.body)).toEqual([]);
  });

  it("list page: the phone card is a plain container (no role, no tab stop)", async () => {
    renderList(makeApi());
    await screen.findAllByText("Chapter 4 notes");
    const link = within(phoneCards()).getByRole("link", { name: "Chapter 4 notes" });
    const card = link.closest<HTMLElement>("div.p-4") as HTMLElement;
    expect(card).not.toBeNull();
    expect(card.getAttribute("role")).toBeNull();
    expect(card.hasAttribute("tabindex")).toBe(false);
    expect(phoneCards().querySelectorAll('[role="button"]')).toHaveLength(0);
  });

  it("list page: the title is a real link with a 44 px touch target, and the keyboard order is title, then Open", async () => {
    const user = userEvent.setup();
    renderList(makeApi());
    await screen.findAllByText("Chapter 4 notes");
    const cards = phoneCards();
    const title = within(cards).getByRole("link", { name: "Chapter 4 notes" });
    const open = within(cards).getByRole("link", { name: /^open/i });
    expect(title.getAttribute("href")).toBe("/teacher/materials/doc-1");
    expect(title.className).toContain("min-h-[44px]");
    expect(open.className).toContain("min-h-[44px]");
    // the only tab stops inside the card list are those two links, in this order
    const stops = Array.from(cards.querySelectorAll<HTMLElement>("a[href], button, [tabindex]:not([tabindex='-1'])"));
    expect(stops).toEqual([title, open]);
    title.focus();
    await user.tab();
    expect(document.activeElement).toBe(open);
    await user.keyboard("{Enter}");
    expect(await screen.findByText("DETAIL ROUTE")).toBeDefined();
  });

  it("list page: Enter on the title link opens the material", async () => {
    const user = userEvent.setup();
    renderList(makeApi());
    await screen.findAllByText("Chapter 4 notes");
    within(phoneCards()).getByRole("link", { name: "Chapter 4 notes" }).focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByText("DETAIL ROUTE")).toBeDefined();
  });

  it("detail page (phone cards, versions table, Test search tab): nothing interactive inside anything interactive", async () => {
    const user = userEvent.setup();
    renderDetail(makeApi());
    await screen.findByRole("heading", { name: "Chapter 4 notes", level: 1 });
    expect(nestedInteractive(document.body)).toEqual([]);
    await user.click(screen.getByRole("tab", { name: "Test search" }));
    await screen.findByLabelText(/question/i);
    expect(nestedInteractive(document.body)).toEqual([]);
  });

  it("detail page: every action button is reachable by keyboard (enabled, no negative tabindex)", async () => {
    renderDetail(makeApi(), "scroll");
    await screen.findByRole("heading", { name: "Chapter 4 notes", level: 1 });
    for (const name of ["Delete document", "Publish", "Delete version"]) {
      const btn = screen.getByRole("button", { name });
      expect(btn.getAttribute("tabindex")).not.toBe("-1");
      expect((btn as HTMLButtonElement).disabled).toBe(false);
    }
  });
});

describe("5f-10a change 2: keyboard reach of New version", () => {
  it("New version is a normal tab stop when nothing is waiting", async () => {
    renderDetail(makeApi(makeDoc({ latest_version: V2 })), "scroll");
    const btn = await screen.findByRole("button", { name: "New version" });
    expect(btn.getAttribute("tabindex")).not.toBe("-1");
    expect((btn as HTMLButtonElement).disabled).toBe(false);
  });
});

// ---- change 3: scope sentence on the single-document search ------------------------------------
const SCOPE_SENTENCE =
  "This searches all published material in the chosen class and subject, not only this document.";

describe("5f-10a change 3: Test search scope line", () => {
  it("detail page: one plain sentence above the form says it is not limited to this document", async () => {
    const user = userEvent.setup();
    renderDetail(makeApi());
    await user.click(await screen.findByRole("tab", { name: "Test search" }));
    const sentence = await screen.findByText(SCOPE_SENTENCE);
    const form = document.querySelector("form") as HTMLFormElement;
    expect(sentence.tagName).toBe("P");
    expect(sentence.compareDocumentPosition(form) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByText(SCOPE_SENTENCE)).toHaveLength(1);
  });

  it("list page: the sentence is not shown (the list search is already about all materials in the filters)", async () => {
    const user = userEvent.setup();
    renderList(makeApi());
    await user.click(await screen.findByRole("tab", { name: "Test search" }));
    await screen.findByLabelText(/question/i);
    expect(screen.queryByText(SCOPE_SENTENCE)).toBeNull();
  });
});

// ---- change 4: the list-page search follows the page filters -----------------------------------
describe("5f-10a change 4: Test search follows the list filters", () => {
  it("after the tab is open, changing the page class and subject filters changes what the panel searches", async () => {
    const user = userEvent.setup();
    const search = vi.fn().mockResolvedValue({
      sufficient: true,
      reason: "similarity",
      message: null,
      threshold: 0.84,
      threshold_calibrated: true,
      keyword_coverage_threshold: null,
      model: "m",
      candidate_count: 0,
      hits: [],
    });
    renderList(makeApi(makeDoc(), { search }));
    await user.click(await screen.findByRole("tab", { name: "Test search" }));
    const panel = screen.getByRole("tabpanel");

    // the page filter bar sits above the tabs: the page "Class" select comes first, the panel's second
    await user.selectOptions(screen.getAllByLabelText("Class")[0], "cls-1");
    await waitFor(() => expect((within(panel).getByLabelText("Class") as HTMLSelectElement).value).toBe("cls-1"));
    await user.selectOptions(screen.getAllByLabelText("Subject")[0], "sub-2");
    await waitFor(() => expect((within(panel).getByLabelText("Subject") as HTMLSelectElement).value).toBe("sub-2"));

    await user.type(within(panel).getByLabelText(/question/i), "light");
    await user.click(within(panel).getByRole("button", { name: "Search" }));
    await waitFor(() => expect(search).toHaveBeenCalled());
    expect(search.mock.calls[0][0].classId).toBe("cls-1");
    expect(search.mock.calls[0][0].subjectId).toBe("sub-2");
  });

  it("clearing a page filter resets the panel to All", async () => {
    const user = userEvent.setup();
    renderList(makeApi(), "/teacher/materials?class=cls-1&subject=sub-1");
    await user.click(await screen.findByRole("tab", { name: "Test search" }));
    const panel = screen.getByRole("tabpanel");
    expect((within(panel).getByLabelText("Class") as HTMLSelectElement).value).toBe("cls-1");
    await user.selectOptions(screen.getAllByLabelText("Class")[0], "");
    await waitFor(() => expect((within(panel).getByLabelText("Class") as HTMLSelectElement).value).toBe(""));
  });

  it("a choice made inside the panel is kept when the page filters do not change", async () => {
    const user = userEvent.setup();
    renderList(makeApi(), "/teacher/materials?class=cls-1");
    await user.click(await screen.findByRole("tab", { name: "Test search" }));
    const panel = screen.getByRole("tabpanel");
    await user.selectOptions(within(panel).getByLabelText("Subject"), "sub-2");
    await user.type(within(panel).getByLabelText(/question/i), "hello");
    expect((within(panel).getByLabelText("Subject") as HTMLSelectElement).value).toBe("sub-2");
    expect((within(panel).getByLabelText("Class") as HTMLSelectElement).value).toBe("cls-1");
  });
});
