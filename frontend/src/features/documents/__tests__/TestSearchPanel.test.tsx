import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within, fireEvent, act, renderHook } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { TestSearchPanel } from "../components/TestSearchPanel";
import { useTestSearch } from "../hooks/useTestSearch";
import { MaterialDetailPage } from "../pages/MaterialDetailPage";
import { MaterialsListPage } from "../pages/MaterialsListPage";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentItem, TestSearchResponseItem } from "../schemas/documentSchemas";
import { academicApi } from "@/features/academic/api/academicApi";
import * as AuthContextModule from "@/features/auth/AuthContext";

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

function createTestQueryClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
}

function hit(over: Record<string, unknown> = {}) {
  return {
    rank: 1,
    chunk_id: "c1",
    document_id: "d1",
    document_title: "Physics Notes",
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

function searchResponse(over: Partial<TestSearchResponseItem> = {}): TestSearchResponseItem {
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

function axiosError(status: number, detail: unknown, extra: Record<string, unknown> = {}) {
  return Object.assign(new Error("request failed"), {
    isAxiosError: true,
    response: { status, data: { detail }, headers: {} },
    ...extra,
  });
}

function mockApi(search = vi.fn().mockResolvedValue(searchResponse())): DocumentsApi {
  return {
    list: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
    get: vi.fn(),
    limits: vi.fn(),
    status: vi.fn(),
    flags: vi.fn(),
    upload: vi.fn(),
    uploadVersion: vi.fn(),
    retryVersion: vi.fn(),
    usage: vi.fn(),
    publishVersion: vi.fn(),
    deleteVersion: vi.fn(),
    deleteDocument: vi.fn(),
    search,
  } as unknown as DocumentsApi;
}

function setUser(permissions: string[], roles: string[] = ["teacher"], isAdmin = false) {
  vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
    user: { id: "u-1", isAdmin, roles, permissions },
    isAuthenticated: true,
    isLoading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  } as never);
}

function LocationProbe() {
  const loc = useLocation();
  return <div data-testid="loc">{`${loc.pathname}${loc.search}${loc.hash}`}</div>;
}

const CLASSES = [
  { id: "cls-1", name: "Class 10A" },
  { id: "cls-2", name: "Class 10B" },
];
const SUBJECTS = [{ id: "sub-1", name: "Computer Science" }];

function renderPanel(api: DocumentsApi, props: Record<string, unknown> = {}) {
  return render(
    <QueryClientProvider client={createTestQueryClient()}>
      <MemoryRouter initialEntries={["/teacher/materials"]}>
        <LocationProbe />
        <TestSearchPanel api={api} classOptions={CLASSES} subjectOptions={SUBJECTS} {...props} />
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function liveRegion(container: HTMLElement): HTMLElement {
  const el = container.querySelector<HTMLElement>('[aria-live="polite"]');
  if (!el) throw new Error("polite live region not found");
  return el;
}

async function ask(text: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/question/i), text);
  await user.click(screen.getByRole("button", { name: "Search" }));
  return user;
}

describe("Phase 5f-8: test-search panel", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setUser(["document:view"]);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  // ---- who sees it --------------------------------------------------------------------------
  it("a student (no document:view) sees no panel at all", () => {
    setUser([], ["student"]);
    const { container } = renderPanel(mockApi());
    expect(screen.queryByRole("button", { name: /search/i })).toBeNull();
    expect(screen.queryByLabelText(/question/i)).toBeNull();
    expect(container.textContent ?? "").not.toMatch(/test search/i);
  });

  it("a user with only upload permission (no view) sees no panel", () => {
    setUser(["document:upload"]);
    renderPanel(mockApi());
    expect(screen.queryByLabelText(/question/i)).toBeNull();
  });

  it("a teacher with document:view and an admin both see the panel", () => {
    renderPanel(mockApi());
    expect(screen.getByLabelText(/question/i)).toBeDefined();
    vi.restoreAllMocks();
    setUser([], ["tenant_admin"], true);
    renderPanel(mockApi());
    expect(screen.getAllByLabelText(/question/i).length).toBe(2);
  });

  // ---- form ---------------------------------------------------------------------------------
  it("Search is disabled while the question is empty, with a live character counter", async () => {
    renderPanel(mockApi());
    const button = screen.getByRole("button", { name: "Search" }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(screen.getByText("0 / 500")).toBeDefined();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/question/i), "light");
    expect(button.disabled).toBe(false);
    expect(screen.getByText("5 / 500")).toBeDefined();
    await user.clear(screen.getByLabelText(/question/i));
    expect(button.disabled).toBe(true);
  });

  it("a blank (spaces only) question stays disabled", async () => {
    renderPanel(mockApi());
    const user = userEvent.setup();
    await user.type(screen.getByLabelText(/question/i), "   ");
    expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("sends the question, scope, drafts flag and result count to the API (drafts off, 6 results by default)", async () => {
    const search = vi.fn().mockResolvedValue(searchResponse());
    renderPanel(mockApi(search), { initialSubjectId: "sub-1" });
    await ask("What is light?");
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1));
    const arg = search.mock.calls[0][0];
    expect(arg.query).toBe("What is light?");
    expect(arg.subjectId).toBe("sub-1");
    expect(arg.includeDrafts).toBe(false);
    expect(arg.k).toBe(6);
    expect(arg.classId || undefined).toBeUndefined();
  });

  it("Include drafts is off by default, explains itself, and is sent as true only when ticked; k comes from the select", async () => {
    const search = vi.fn().mockResolvedValue(searchResponse());
    renderPanel(mockApi(search));
    const box = screen.getByLabelText(/include drafts/i) as HTMLInputElement;
    expect(box.checked).toBe(false);
    expect(screen.getByText(/Also search versions that are ready but not published/)).toBeDefined();
    expect(screen.getByText(/Only published versions are searched unless you tick/i)).toBeDefined();
    const user = userEvent.setup();
    await user.click(box);
    await user.selectOptions(screen.getByLabelText(/number of results/i), "3");
    await user.type(screen.getByLabelText(/question/i), "light");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(search).toHaveBeenCalled());
    expect(search.mock.calls[0][0].includeDrafts).toBe(true);
    expect(search.mock.calls[0][0].k).toBe(3);
  });

  it("the result count select offers 1 to 10 with 6 selected", () => {
    renderPanel(mockApi());
    const select = screen.getByLabelText(/number of results/i) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual(["1", "2", "3", "4", "5", "6", "7", "8", "9", "10"]);
    expect(select.value).toBe("6");
  });

  it("a question over 500 characters shows the plain message beside the box and sends nothing", async () => {
    const search = vi.fn().mockResolvedValue(searchResponse());
    renderPanel(mockApi(search));
    fireEvent.change(screen.getByLabelText(/question/i), { target: { value: "a".repeat(501) } });
    expect(screen.getByText("501 / 500")).toBeDefined();
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText(/too long.*500 characters/i)).toBeDefined();
    expect(search).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/question/i).getAttribute("aria-invalid")).toBe("true");
  });

  // ---- loading, double submit -----------------------------------------------------------------
  it("shows a loading state and never sends a second request while one is in flight", async () => {
    let resolve!: (v: TestSearchResponseItem) => void;
    const search = vi.fn().mockImplementation(
      () => new Promise<TestSearchResponseItem>((r) => {
        resolve = r;
      })
    );
    renderPanel(mockApi(search));
    fireEvent.change(screen.getByLabelText(/question/i), { target: { value: "light" } });
    const button = screen.getByRole("button", { name: "Search" });
    // two clicks in the same tick: only one request may leave
    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.submit(button.closest("form")!);
    await waitFor(() => expect(search).toHaveBeenCalledTimes(1));
    const busy = await screen.findByRole("button", { name: /searching/i });
    expect((busy as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(busy);
    expect(search).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolve(searchResponse());
    });
    await waitFor(() => expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(false));
    expect(search).toHaveBeenCalledTimes(1);
  });

  it("after the first search slows past 3 seconds it says the search model is loading, and the hint goes away when done", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let resolve!: (v: TestSearchResponseItem) => void;
    const search = vi.fn().mockImplementation(
      () => new Promise<TestSearchResponseItem>((r) => {
        resolve = r;
      })
    );
    renderPanel(mockApi(search));
    fireEvent.change(screen.getByLabelText(/question/i), { target: { value: "light" } });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    expect(screen.queryByText(/Loading the search model/)).toBeNull();
    await act(async () => {
      vi.advanceTimersByTime(2900);
    });
    expect(screen.queryByText(/Loading the search model/)).toBeNull();
    await act(async () => {
      vi.advanceTimersByTime(200);
    });
    expect(screen.getByText(/Loading the search model, this can take a few seconds/)).toBeDefined();
    await act(async () => {
      resolve(searchResponse());
    });
    // the hint is gone as soon as the search is done (no timers involved: state is set by the finished call)
    expect(screen.queryByText(/Loading the search model/)).toBeNull();
  });

  // ---- results ------------------------------------------------------------------------------
  it("renders result cards in rank order with title link, version, chip, page, heading and a 3-decimal similarity", async () => {
    const search = vi.fn().mockResolvedValue(
      searchResponse({
        hits: [
          hit({ rank: 2, chunk_id: "c2", document_title: "Second", vector_similarity: 0.86 }),
          hit({ rank: 1, chunk_id: "c1", document_title: "First", vector_similarity: 0.9123, version_status: "ready" }),
        ],
      })
    );
    renderPanel(mockApi(search));
    await ask("light");
    const list = await screen.findByRole("list", { name: /search results/i });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("First");
    expect(items[1].textContent).toContain("Second");
    const first = within(items[0]);
    expect(first.getByText("#1")).toBeDefined();
    expect(first.getByText(/Version 2/)).toBeDefined();
    expect(first.getByText("Draft")).toBeDefined();
    expect(first.getByText(/Pages 3-4/)).toBeDefined();
    expect(first.getByText(/Unit 1 > Light/)).toBeDefined();
    expect(first.getByText(/0\.912/)).toBeDefined();
    expect(first.getByText(/above the threshold/)).toBeDefined();
    expect(within(items[1]).getByText("Published")).toBeDefined();
    const link = first.getByRole("link", { name: /First/ });
    expect(link.getAttribute("href")).toBe("/teacher/materials/d1");
  });

  it("shows threshold, candidate count and the confirmation when calibrated; no warning", async () => {
    renderPanel(mockApi());
    await ask("light");
    expect(await screen.findByText("Threshold 0.84")).toBeDefined();
    expect(screen.getByText("Searched 40 passages.")).toBeDefined();
    expect(screen.getByText(/Calibrated for this model/)).toBeDefined();
    expect(screen.queryByText(/Not calibrated/)).toBeNull();
    expect(screen.getByText(/Enough matching material was found/)).toBeDefined();
  });

  it("shows the not-calibrated notice ONLY when threshold_calibrated is false", async () => {
    const search = vi.fn().mockResolvedValue(searchResponse({ threshold_calibrated: false, model: "model-b" }));
    renderPanel(mockApi(search));
    await ask("light");
    const notice = await screen.findByText(/Not calibrated for the current search model \(model-b\)/);
    expect(notice.textContent).toMatch(/may mislead/);
    expect(notice.textContent).toMatch(/recalibrated/);
    expect(screen.queryByText(/Calibrated for this model/)).toBeNull();
  });

  it("insufficient with hits: banner, 'assistant would not use them' note, and the hits are still listed", async () => {
    const search = vi.fn().mockResolvedValue(
      searchResponse({ sufficient: false, reason: "below_threshold", hits: [hit({ vector_similarity: 0.5 })] })
    );
    renderPanel(mockApi(search));
    await ask("light");
    expect(await screen.findByText(/No passage was found that is similar enough to answer this question\./)).toBeDefined();
    expect(screen.getByText(/would not use these passages/i)).toBeDefined();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText(/below the threshold/)).toBeDefined();
  });

  it("keyword line is hidden when keyword_rank is null and shown when present; technical details are in a disclosure", async () => {
    const search = vi
      .fn()
      .mockResolvedValueOnce(searchResponse())
      .mockResolvedValueOnce(searchResponse({ hits: [hit({ keyword_rank: 2, keyword_matched_terms: 3 })] }));
    renderPanel(mockApi(search));
    const user = await ask("light");
    await screen.findByText("Threshold 0.84");
    expect(screen.queryByText(/Keyword/)).toBeNull();
    expect(screen.getByText("Technical details")).toBeDefined();
    expect(screen.getByText("Fused score")).toBeDefined();
    await user.click(screen.getByRole("button", { name: "Search" }));
    expect(await screen.findByText(/Keyword rank 2/)).toBeDefined();
  });

  it("a null similarity shows a dash, never 0", async () => {
    const search = vi.fn().mockResolvedValue(searchResponse({ hits: [hit({ vector_similarity: null })] }));
    renderPanel(mockApi(search));
    await ask("light");
    const item = (await screen.findAllByRole("listitem"))[0];
    expect(within(item).getByText(/Similarity -/)).toBeDefined();
    expect(item.textContent).not.toMatch(/0\.000/);
  });

  it("flag chips and title/file-name warnings appear on a flagged hit", async () => {
    const search = vi.fn().mockResolvedValue(
      searchResponse({
        hits: [hit({ flagged: true, injection_flags: ["role_markup"], title_flagged: true, filename_flagged: true })],
      })
    );
    renderPanel(mockApi(search));
    await ask("light");
    expect(await screen.findByText("Looks like chat-system markup")).toBeDefined();
    expect(screen.getByText(/title of this material was flagged/i)).toBeDefined();
    expect(screen.getByText(/file name .*flagged/i)).toBeDefined();
  });

  it("a long passage is collapsed with a Show more button and expands and collapses", async () => {
    const long = Array.from({ length: 30 }, (_, i) => `Line number ${i}`).join("\n");
    const search = vi.fn().mockResolvedValue(searchResponse({ hits: [hit({ content: long })] }));
    renderPanel(mockApi(search));
    await ask("light");
    const more = await screen.findByRole("button", { name: /show more/i });
    expect(more.getAttribute("aria-expanded")).toBe("false");
    expect(more.className).toContain("min-h-[44px]");
    expect(screen.queryByText(/Line number 29/)).toBeNull();
    const user = userEvent.setup();
    await user.click(more);
    expect(screen.getByText(/Line number 29/)).toBeDefined();
    const less = screen.getByRole("button", { name: /show less/i });
    expect(less.getAttribute("aria-expanded")).toBe("true");
    await user.click(less);
    expect(screen.queryByText(/Line number 29/)).toBeNull();
  });

  // ---- untrusted text -----------------------------------------------------------------------
  it("renders a hostile passage, title, heading and model name as literal text: no script, image, link or markup", async () => {
    const evil =
      '<script>window.__pwned = 1</script><img src=x onerror="window.__pwned = 2"> ' +
      '[click me](https://evil.example/x) **bold** <a href="https://evil.example">anchor</a> ' +
      "<b>tag</b> ![pic](https://evil.example/p.png)";
    const search = vi.fn().mockResolvedValue(
      searchResponse({
        model: "<img src=x onerror=1>model",
        threshold_calibrated: false,
        hits: [hit({ content: evil, document_title: "<script>t</script>Title", heading_path: "<img src=h onerror=1>Heading" })],
      })
    );
    const { container } = renderPanel(mockApi(search));
    await ask("light");
    await screen.findByRole("list", { name: /search results/i });

    // the exact characters are on the page as text
    expect(screen.getByText(evil)).toBeDefined();
    expect(screen.getByText(/<script>t<\/script>Title/)).toBeDefined();
    expect(screen.getByText(/<img src=h onerror=1>Heading/)).toBeDefined();
    expect(container.textContent).toContain("<img src=x onerror=1>model");

    // and none of it became an element
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("strong, b, em")).toBeNull();
    expect(screen.queryByRole("link", { name: /click me|anchor|pic/i })).toBeNull();
    const hrefs = Array.from(container.querySelectorAll("a")).map((a) => a.getAttribute("href") ?? "");
    for (const href of hrefs) expect(href.startsWith("/")).toBe(true);
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
  });

  // ---- empty states ---------------------------------------------------------------------------
  const EMPTY_CASES: Array<[string, Partial<TestSearchResponseItem>, RegExp]> = [
    ["below_threshold", { sufficient: false, reason: "below_threshold", hits: [] }, /No passage was found that is similar enough to answer this question\./],
    ["no_candidates", { sufficient: false, reason: "no_candidates", hits: [], candidate_count: 0 }, /No passage matched/],
    ["empty_scope", { sufficient: false, reason: "empty_scope", hits: [], candidate_count: 0 }, /no (published )?material/i],
    ["unknown reason with message", { sufficient: false, reason: "brand_new", message: "Custom words", hits: [] }, /Custom words/],
    ["unknown reason without message", { sufficient: false, reason: "brand_new", message: null, hits: [] }, /\S/],
  ];
  for (const [name, over, re] of EMPTY_CASES) {
    it(`empty state: ${name}`, async () => {
      const search = vi.fn().mockResolvedValue(searchResponse(over));
      const { container } = renderPanel(mockApi(search));
      await ask("light");
      await screen.findByText("Threshold 0.84");
      // the banner and the hidden live region both carry the text
      expect(screen.getAllByText(re).length).toBeGreaterThanOrEqual(2);
      expect(screen.queryByRole("list", { name: /search results/i })).toBeNull();
      expect(liveRegion(container).textContent ?? "").toMatch(re);
    });
  }

  it("the empty-scope text points at drafts when drafts are off", async () => {
    const search = vi.fn().mockResolvedValue(searchResponse({ sufficient: false, reason: "empty_scope", hits: [], candidate_count: 0 }));
    renderPanel(mockApi(search));
    await ask("light");
    expect((await screen.findAllByText(/no published material to search in this scope/i)).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Tick Include drafts/i).length).toBeGreaterThan(0);
  });

  // ---- errors -------------------------------------------------------------------------------
  const ERROR_CASES: Array<[string, unknown, RegExp]> = [
    ["429 with wait", axiosError(429, { code: "test_search_rate_limit_exceeded", retry_after_seconds: 45 }), /searched too often.*45 seconds/],
    ["503 search_unavailable", axiosError(503, { code: "search_unavailable" }), /Search is temporarily unavailable/],
    ["503 search_timeout", axiosError(503, { code: "search_timeout" }), /took too long/],
    ["503 search_busy", axiosError(503, { code: "search_busy" }), /busy right now/],
    ["503 service_unavailable", axiosError(503, { code: "service_unavailable" }), /Search is temporarily unavailable/],
    ["404", axiosError(404, { code: "not_found" }), /not one of yours/],
    ["401", axiosError(401, { code: "missing_token" }), /sign in again/],
    ["403", axiosError(403, { code: "permission_denied" }), /permission/],
    ["500", axiosError(500, "boom"), /went wrong on our side/],
    ["network", Object.assign(new Error("Network Error"), { isAxiosError: true, request: {} }), /Network error/],
  ];
  for (const [name, err, re] of ERROR_CASES) {
    it(`error state: ${name} shows plain text in an alert, no raw code`, async () => {
      const search = vi.fn().mockRejectedValue(err);
      renderPanel(mockApi(search));
      await ask("light");
      const alert = await screen.findByRole("alert");
      expect(alert.textContent ?? "").toMatch(re);
      expect(alert.textContent ?? "").not.toMatch(/search_unavailable|search_timeout|search_busy|service_unavailable|not_found|permission_denied|missing_token|boom/);
      // the form stays usable and keeps the typed question
      expect((screen.getByLabelText(/question/i) as HTMLTextAreaElement).value).toBe("light");
      expect((screen.getByRole("button", { name: "Search" }) as HTMLButtonElement).disabled).toBe(false);
    });
  }

  it("422 invalid_query and query_too_long show beside the question box and move focus to it", async () => {
    for (const code of ["invalid_query", "query_too_long"]) {
      vi.restoreAllMocks();
      setUser(["document:view"]);
      const search = vi.fn().mockRejectedValue(axiosError(422, { code }));
      const { unmount } = renderPanel(mockApi(search));
      await ask("???");
      const box = screen.getByLabelText(/question/i);
      await waitFor(() => expect(box.getAttribute("aria-invalid")).toBe("true"));
      const describedBy = box.getAttribute("aria-describedby") ?? "";
      const text = describedBy
        .split(" ")
        .map((id) => document.getElementById(id)?.textContent ?? "")
        .join(" ");
      expect(text).toMatch(code === "invalid_query" ? /at least one word/ : /too long/);
      await waitFor(() => expect(document.activeElement).toBe(box));
      unmount();
    }
  });

  it("a failed search focuses the error and the previous results are gone", async () => {
    const search = vi.fn().mockResolvedValueOnce(searchResponse()).mockRejectedValueOnce(axiosError(503, { code: "search_busy" }));
    renderPanel(mockApi(search));
    const user = await ask("light");
    await screen.findByRole("list", { name: /search results/i });
    await user.click(screen.getByRole("button", { name: "Search" }));
    const alert = await screen.findByRole("alert");
    expect(screen.queryByRole("list", { name: /search results/i })).toBeNull();
    await waitFor(() => expect(alert.contains(document.activeElement) || alert === document.activeElement || alert.parentElement === document.activeElement).toBe(true));
  });

  // ---- focus and announcements --------------------------------------------------------------
  it("after a search focus moves to the results heading, and the polite live region says how many passages were found", async () => {
    const search = vi.fn().mockResolvedValue(searchResponse({ hits: [hit({ rank: 1 }), hit({ rank: 2, chunk_id: "c2" })] }));
    const { container } = renderPanel(mockApi(search));
    await ask("light");
    const heading = await screen.findByRole("heading", { name: /search results/i });
    await waitFor(() => expect(document.activeElement).toBe(heading));
    expect(heading.getAttribute("tabindex")).toBe("-1");
    expect(liveRegion(container).textContent).toBe("2 passages found");
  });

  it("the live region says '1 passage found' for a single hit", async () => {
    const { container } = renderPanel(mockApi());
    await ask("light");
    await screen.findByRole("list", { name: /search results/i });
    expect(liveRegion(container).textContent).toBe("1 passage found");
  });

  // ---- privacy ------------------------------------------------------------------------------
  it("the question never reaches the URL or browser storage", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    renderPanel(mockApi());
    await ask("super secret question");
    await screen.findByRole("list", { name: /search results/i });
    expect(screen.getByTestId("loc").textContent).toBe("/teacher/materials");
    expect(setItem).not.toHaveBeenCalled();
    expect(JSON.stringify(Object.entries(localStorage))).not.toContain("secret");
    expect(JSON.stringify(Object.entries(sessionStorage))).not.toContain("secret");
  });

  // ---- mobile (360 px) ----------------------------------------------------------------------
  it("mobile: the form stacks, the button is full width with a 44px target, cards wrap long text", async () => {
    const long = "w".repeat(300);
    const search = vi.fn().mockResolvedValue(searchResponse({ hits: [hit({ content: long, document_title: long, heading_path: long })] }));
    const { container } = renderPanel(mockApi(search));
    const form = container.querySelector("form")!;
    expect(form.className).toMatch(/\bflex-col\b|\bgrid-cols-1\b|\bspace-y-/);
    const button = screen.getByRole("button", { name: "Search" });
    expect(button.className).toContain("min-h-[44px]");
    expect(button.className).toContain("w-full");
    const textarea = screen.getByLabelText(/question/i);
    expect(textarea.className).toContain("w-full");
    await ask("light");
    const items = await screen.findAllByRole("listitem");
    expect(items[0].className).toContain("min-w-0");
    for (const el of Array.from(items[0].querySelectorAll("p, a, h4"))) {
      expect(el.className, `${el.tagName} ${el.textContent?.slice(0, 20)}`).toMatch(/break-words/);
    }
    expect(container.querySelector("table")).toBeNull();
    // everything focusable in the result is a native control reachable by Tab
    for (const el of Array.from(items[0].querySelectorAll("[tabindex]"))) {
      expect(el.getAttribute("tabindex")).not.toBe("-1");
    }
  });

  it("mobile: the options (select, checkbox label) are real labelled controls", () => {
    renderPanel(mockApi());
    for (const name of [/class/i, /subject/i, /number of results/i]) {
      expect(screen.getByLabelText(name).tagName).toBe("SELECT");
    }
    expect(screen.getByLabelText(/include drafts/i).getAttribute("type")).toBe("checkbox");
  });

  // ---- scope options ------------------------------------------------------------------------
  it("class and subject choices come from the options and default to everything", async () => {
    const search = vi.fn().mockResolvedValue(searchResponse());
    renderPanel(mockApi(search));
    const classSelect = screen.getByLabelText(/class/i) as HTMLSelectElement;
    expect(Array.from(classSelect.options).map((o) => o.textContent)).toEqual(["All classes", "Class 10A", "Class 10B"]);
    const user = userEvent.setup();
    await user.selectOptions(classSelect, "cls-2");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-1");
    await user.type(screen.getByLabelText(/question/i), "light");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(search).toHaveBeenCalled());
    expect(search.mock.calls[0][0].classId).toBe("cls-2");
    expect(search.mock.calls[0][0].subjectId).toBe("sub-1");
  });
});

describe("Phase 5f-8: useTestSearch hook", () => {
  it("two calls in the same tick send ONE request; after it finishes a new call is allowed", async () => {
    const search = vi.fn().mockResolvedValue(searchResponse());
    const api = mockApi(search);
    const { result } = renderHook(() => useTestSearch({ api }));
    await act(async () => {
      void result.current.run({ query: "a" });
      void result.current.run({ query: "a" });
    });
    expect(search).toHaveBeenCalledTimes(1);
    expect(result.current.state.status).toBe("success");
    await act(async () => {
      await result.current.run({ query: "b" });
    });
    expect(search).toHaveBeenCalledTimes(2);
  });

  it("a failed call is reported as an error and the next call is allowed", async () => {
    const search = vi.fn().mockRejectedValueOnce(axiosError(503, { code: "search_busy" })).mockResolvedValue(searchResponse());
    const api = mockApi(search);
    const { result } = renderHook(() => useTestSearch({ api }));
    await act(async () => {
      await result.current.run({ query: "a" });
    });
    expect(result.current.state.status).toBe("error");
    await act(async () => {
      await result.current.run({ query: "a" });
    });
    expect(result.current.state.status).toBe("success");
  });

  it("the state never holds the question text", async () => {
    const api = mockApi();
    const { result } = renderHook(() => useTestSearch({ api }));
    await act(async () => {
      await result.current.run({ query: "very private words" });
    });
    expect(JSON.stringify(result.current.state)).not.toContain("very private words");
  });
});

// -------------------------------------------------------------------------------------------------
// Where the panel is mounted
function docItem(): DocumentItem {
  return {
    id: "doc-1",
    title: "Intro to AI",
    description: null,
    subject: { id: "sub-1", name: "Computer Science" },
    classes: [{ id: "cls-1", name: "Class 10A" }],
    created_by: "u-1",
    created_at: "2026-11-20T10:00:00Z",
    updated_at: "2026-11-20T10:00:00Z",
    latest_version: {
      id: "v-1",
      document_id: "doc-1",
      version_no: 1,
      status: "published",
      stage: null,
      error_code: null,
      file_kind: "pdf",
      original_filename: "ai.pdf",
      size_bytes: 2048,
      page_count: 5,
      chunk_count: 10,
      flagged_chunk_count: 0,
      created_at: "2026-11-20T10:00:00Z",
      published_at: "2026-11-20T10:00:00Z",
      retryable: null,
      warnings: [],
    },
    published_version: null,
    can_manage: true,
    blocked_reason: null,
  } as DocumentItem;
}

describe("Phase 5f-8: where the panel is mounted", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(academicApi, "getMyClasses").mockResolvedValue([
      { id: "cls-1", name: "Class 10A", kind: "standard", subjects: [{ id: "sub-1", name: "Computer Science" }] },
    ] as never);
  });

  function renderDetail(api: DocumentsApi) {
    return render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={["/teacher/materials/doc-1"]}>
          <Routes>
            <Route path="/teacher/materials/:documentId" element={<MaterialDetailPage api={api} />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  it("detail page: the Test search tab exists with document:view and is pre-scoped to the document's subject", async () => {
    setUser(["document:view"]);
    const search = vi.fn().mockResolvedValue(searchResponse());
    const api = { ...mockApi(search), get: vi.fn().mockResolvedValue(docItem()) } as DocumentsApi;
    renderDetail(api);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Test search" }));
    expect((screen.getByLabelText(/subject/i) as HTMLSelectElement).value).toBe("sub-1");
    // class choices are this document's classes only
    const classSelect = screen.getByLabelText(/class/i) as HTMLSelectElement;
    expect(Array.from(classSelect.options).map((o) => o.textContent)).toEqual(["All classes", "Class 10A"]);
    await user.type(screen.getByLabelText(/question/i), "what is AI");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(search).toHaveBeenCalled());
    expect(search.mock.calls[0][0].subjectId).toBe("sub-1");
  });

  it("detail page: no Test search tab without document:view", async () => {
    setUser(["document:upload"]);
    const api = { ...mockApi(), get: vi.fn().mockResolvedValue(docItem()) } as DocumentsApi;
    renderDetail(api);
    await screen.findByRole("heading", { name: "Intro to AI", level: 1 });
    expect(screen.queryByRole("tab", { name: "Test search" })).toBeNull();
  });

  function renderList(api: DocumentsApi, path = "/teacher/materials?class=cls-1&subject=sub-1") {
    return render(
      <QueryClientProvider client={createTestQueryClient()}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="/teacher/materials" element={<MaterialsListPage api={api} />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  it("list page: a Test search tab is scoped by the same filters", async () => {
    setUser(["document:view"]);
    const search = vi.fn().mockResolvedValue(searchResponse());
    renderList(mockApi(search));
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "Test search" }));
    await user.type(screen.getByLabelText(/question/i), "light");
    await user.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => expect(search).toHaveBeenCalled());
    expect(search.mock.calls[0][0].classId).toBe("cls-1");
    expect(search.mock.calls[0][0].subjectId).toBe("sub-1");
  });

  it("list page: no Test search tab without document:view", async () => {
    setUser([], ["student"]);
    renderList(mockApi());
    await waitFor(() => expect(screen.getByRole("heading", { name: /course materials/i, level: 1 })).toBeDefined());
    expect(screen.queryByRole("tab", { name: "Test search" })).toBeNull();
  });
});
