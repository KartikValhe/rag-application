import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within, fireEvent, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MaterialsUsagePage } from "../pages/MaterialsUsagePage";
import { MaterialsListPage } from "../pages/MaterialsListPage";
import type { DocumentsApi } from "../api/documentsApi";
import { getDocumentError } from "../utils/documentErrors";
import { DocumentUsageSchema } from "../schemas/documentSchemas";
import { academicApi } from "@/features/academic/api/academicApi";
import * as AuthContextModule from "@/features/auth/AuthContext";
import { AdminRoute, TeacherRoute } from "@/features/auth/guards";

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

const LIMITS = {
  max_file_bytes: 10 * 1024 * 1024,
  max_pdf_pages: 500,
  max_pptx_slides: 200,
  max_documents: 200,
  max_storage_bytes: 1024 * 1024 * 1024,
  max_pages: 10000,
  uploads_per_user_per_hour: 20,
  uploads_per_tenant_per_hour: 100,
};

function usageData(overrides: Record<string, unknown> = {}) {
  return {
    documents: 120,
    storage_bytes: 5 * 1024 * 1024,
    pages: 3400,
    limits: { ...LIMITS },
    ...overrides,
  };
}

type Who = "admin" | "teacher" | "teacher-with-settings" | "student" | "admin-no-settings" | "settings-no-view";

function setUser(who: Who) {
  const users = {
    admin: { id: "a1", isAdmin: true, roles: ["tenant_admin"], permissions: ["*"] },
    teacher: {
      id: "t1",
      isAdmin: false,
      roles: ["teacher"],
      permissions: ["document:view", "document:upload", "document:delete", "document:publish"],
    },
    "teacher-with-settings": {
      id: "t2",
      isAdmin: false,
      roles: ["teacher"],
      permissions: ["document:view", "settings:manage"],
    },
    student: { id: "s1", isAdmin: false, roles: ["student"], permissions: [] as string[] },
    "admin-no-settings": { id: "a2", isAdmin: false, roles: ["tenant_admin"], permissions: ["document:view"] },
    "settings-no-view": { id: "a3", isAdmin: false, roles: ["tenant_admin"], permissions: ["settings:manage"] },
  };
  vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
    user: users[who],
    isAuthenticated: true,
    isLoading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  } as never);
}

function axiosError(status: number, detail?: unknown, headers: Record<string, string> = {}) {
  return Object.assign(new Error(`HTTP ${status}`), {
    isAxiosError: true,
    response: { status, data: { detail }, headers },
  });
}

// Like the real API function: the response goes through the same schema before the page sees it
function fakeApi(usage: ReturnType<typeof vi.fn>): DocumentsApi {
  const wrapped = () => Promise.resolve(usage()).then((data) => DocumentUsageSchema.parse(data));
  return { usage: wrapped } as unknown as DocumentsApi;
}

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function renderAt(path: string, api: DocumentsApi, who: Who = "admin") {
  setUser(who);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/admin/materials/usage"
            element={
              <AdminRoute permission="document:view">
                <AdminRoute permission="settings:manage">
                  <MaterialsUsagePage api={api} />
                </AdminRoute>
              </AdminRoute>
            }
          />
          <Route
            path="/admin/materials"
            element={
              <AdminRoute permission="document:view">
                <MaterialsListPage api={api} />
              </AdminRoute>
            }
          />
          <Route
            path="/teacher/materials"
            element={
              <TeacherRoute permission="document:view">
                <MaterialsListPage api={api} />
              </TeacherRoute>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function loaded(api: DocumentsApi, who: Who = "admin") {
  const view = renderAt("/admin/materials/usage", api, who);
  await screen.findByRole("heading", { level: 1, name: "Usage and limits" });
  await waitFor(() => expect(screen.queryByText(/Loading usage/)).toBeNull());
  return view;
}

function meterCard(name: string): HTMLElement {
  const bar = screen.getByRole("progressbar", { name });
  const card = bar.closest("[data-usage-card]");
  if (!card) throw new Error(`no card for ${name}`);
  return card as HTMLElement;
}

describe("MaterialsUsagePage: admin view", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the heading, three native meters with accessible names and 'used of max (percent%)' text", async () => {
    const usage = vi.fn().mockResolvedValue(usageData());
    await loaded(fakeApi(usage));

    const docs = screen.getByRole("progressbar", { name: "Documents" });
    expect(docs.tagName).toBe("PROGRESS");
    expect(docs.getAttribute("value")).toBe("120");
    expect(docs.getAttribute("max")).toBe("200");
    expect(within(meterCard("Documents")).getByText("120 of 200 (60%)")).toBeTruthy();

    const storage = screen.getByRole("progressbar", { name: "Storage" });
    expect(storage.tagName).toBe("PROGRESS");
    expect(within(meterCard("Storage")).getByText("5.0 MB of 1.0 GB (0%)")).toBeTruthy();

    const pages = screen.getByRole("progressbar", { name: "Pages" });
    expect(pages.tagName).toBe("PROGRESS");
    expect(within(meterCard("Pages")).getByText("3,400 of 10,000 (34%)")).toBeTruthy();

    expect(usage).toHaveBeenCalledTimes(1);
  });

  it("lists the limits with human sizes, including uploads per hour, and says deleted materials are not counted", async () => {
    await loaded(fakeApi(vi.fn().mockResolvedValue(usageData())));
    const limits = screen.getByRole("region", { name: "Upload limits" });
    expect(within(limits).getByText("Largest file")).toBeTruthy();
    expect(within(limits).getByText("10.0 MB")).toBeTruthy();
    expect(within(limits).getByText("Most pages in one PDF")).toBeTruthy();
    expect(within(limits).getByText("500")).toBeTruthy();
    expect(within(limits).getByText("Most slides in one presentation")).toBeTruthy();
    expect(within(limits).getByText("200")).toBeTruthy();
    expect(within(limits).getByText("Uploads per person per hour")).toBeTruthy();
    expect(within(limits).getByText("20")).toBeTruthy();
    expect(within(limits).getByText("Uploads per organisation per hour")).toBeTruthy();
    expect(within(limits).getByText("100")).toBeTruthy();
    expect(screen.getByText(/Deleted materials are not counted/)).toBeTruthy();
    // 5g-1: the old "not shown here" sentence is replaced by the upload attempts section
    expect(screen.queryByText(/uploads made so far this hour is not shown/i)).toBeNull();
  });

  it("shows when the data was fetched as a real time element", async () => {
    await loaded(fakeApi(vi.fn().mockResolvedValue(usageData())));
    const line = screen.getByText(/Fetched at/);
    const time = line.querySelector("time");
    expect(time).not.toBeNull();
    expect(Number.isNaN(Date.parse(time?.getAttribute("datetime") ?? ""))).toBe(false);
    expect((time?.textContent ?? "").length).toBeGreaterThan(0);
  });

  it("warning levels are in words: 85% says Nearly full, 100% says Limit reached, 10% says Within limit", async () => {
    const data = usageData({ documents: 170, storage_bytes: LIMITS.max_storage_bytes, pages: 1000 });
    await loaded(fakeApi(vi.fn().mockResolvedValue(data)));
    expect(within(meterCard("Documents")).getByText("170 of 200 (85%)")).toBeTruthy();
    expect(within(meterCard("Documents")).getByText("Nearly full")).toBeTruthy();
    expect(within(meterCard("Storage")).getByText("Limit reached")).toBeTruthy();
    expect(within(meterCard("Pages")).getByText("Within limit")).toBeTruthy();
  });

  it("a meter whose maximum is missing, null or zero shows the plain number: no percentage, no bar, no NaN", async () => {
    for (const bad of [0, null, undefined]) {
      vi.restoreAllMocks();
      const limits: Record<string, unknown> = { ...LIMITS };
      if (bad === undefined) delete limits.max_documents;
      else limits.max_documents = bad;
      const { unmount } = await loaded(fakeApi(vi.fn().mockResolvedValue(usageData({ limits }))));
      expect(screen.queryByRole("progressbar", { name: "Documents" })).toBeNull();
      const card = screen.getByText("Documents", { selector: "[data-usage-label]" }).closest("[data-usage-card]") as HTMLElement;
      expect(within(card).getByText("120")).toBeTruthy();
      expect(card.textContent).not.toMatch(/%|NaN|Infinity/);
      expect(document.body.textContent).not.toMatch(/NaN|Infinity|undefined/);
      unmount();
    }
  });

  it("a missing figure shows 'Not available' and never NaN", async () => {
    const data = usageData({ documents: null, pages: undefined });
    await loaded(fakeApi(vi.fn().mockResolvedValue(data)));
    expect(within(meterCard("Storage")).getByText(/of 1\.0 GB/)).toBeTruthy();
    const docsLabel = screen.getByText("Documents", { selector: "[data-usage-label]" });
    expect(within(docsLabel.closest("[data-usage-card]") as HTMLElement).getByText("Not available")).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/NaN|Infinity/);
  });

  it("zero documents shows the meters at zero and a plain 'no materials yet' note", async () => {
    await loaded(fakeApi(vi.fn().mockResolvedValue(usageData({ documents: 0, storage_bytes: 0, pages: 0 }))));
    expect(within(meterCard("Documents")).getByText("0 of 200 (0%)")).toBeTruthy();
    expect(screen.getByText("No materials have been uploaded yet.")).toBeTruthy();
  });

  it("the empty state (no figures at all) says there is no data yet", async () => {
    await loaded(fakeApi(vi.fn().mockResolvedValue({})));
    expect(screen.getByText("No usage data yet.")).toBeTruthy();
    expect(screen.queryAllByRole("progressbar")).toHaveLength(0);
  });
});

describe("MaterialsUsagePage: loading, errors and refresh", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("loading state: plain text in a status region, no figures yet", async () => {
    const d = deferred<unknown>();
    renderAt("/admin/materials/usage", fakeApi(vi.fn().mockReturnValue(d.promise)));
    expect(await screen.findByRole("status")).toBeTruthy();
    expect(screen.getByText("Loading usage figures...")).toBeTruthy();
    expect(screen.queryAllByRole("progressbar")).toHaveLength(0);
    await act(async () => d.resolve(usageData()));
    await screen.findByRole("progressbar", { name: "Documents" });
    expect(screen.queryByText("Loading usage figures...")).toBeNull();
  });

  const ERRORS: Array<[string, unknown]> = [
    ["401", axiosError(401, { code: "invalid_token", message: "RAW SERVER TEXT" })],
    ["403", axiosError(403, { code: "permission_denied", message: "RAW SERVER TEXT" })],
    ["404", axiosError(404, { code: "not_found", message: "RAW SERVER TEXT" })],
    ["429", axiosError(429, { code: "rate_limited", message: "RAW SERVER TEXT" }, { "retry-after": "30" })],
    ["500", axiosError(500, { code: "internal_error", message: "RAW SERVER TEXT" })],
    ["503", axiosError(503, { code: "service_unavailable", message: "RAW SERVER TEXT" })],
    ["network", Object.assign(new Error("Network Error"), { isAxiosError: true, request: {} })],
    ["schema mismatch", Object.assign(new Error("RAW SERVER TEXT"), { name: "SchemaMismatchError" })],
  ];

  for (const [name, err] of ERRORS) {
    it(`error state ${name}: the text comes from the error table, never from the server or the raw code`, async () => {
      renderAt("/admin/materials/usage", fakeApi(vi.fn().mockRejectedValue(err)));
      const alert = await screen.findByRole("alert");
      const expected = getDocumentError(err, "usage").message;
      expect(expected.length).toBeGreaterThan(0);
      expect(within(alert).getByText(expected)).toBeTruthy();
      expect(alert.textContent).not.toMatch(/RAW SERVER TEXT|permission_denied|invalid_token|internal_error|not_found|rate_limited/);
      expect(screen.queryAllByRole("progressbar")).toHaveLength(0);
      expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    });
  }

  it("each status has its own plain wording", () => {
    const text = (e: unknown) => getDocumentError(e, "usage").message;
    expect(text(axiosError(401))).toMatch(/sign in again/);
    expect(text(axiosError(403))).toMatch(/permission/);
    expect(text(axiosError(404))).toMatch(/usage/i);
    expect(text(axiosError(404))).not.toMatch(/material/);
    expect(text(axiosError(429, undefined, { "retry-after": "30" }))).toMatch(/30 seconds/);
    expect(text(axiosError(500))).toMatch(/our side/);
    expect(text(Object.assign(new Error("x"), { isAxiosError: true, request: {} }))).toMatch(/Network error/);
  });

  it("Try again after an error loads the figures", async () => {
    const usage = vi.fn().mockRejectedValueOnce(axiosError(500)).mockResolvedValue(usageData());
    renderAt("/admin/materials/usage", fakeApi(usage));
    await screen.findByRole("alert");
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    await screen.findByRole("progressbar", { name: "Documents" });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(usage).toHaveBeenCalledTimes(2);
  });

  it("Refresh fetches once more and shows the new figures", async () => {
    const usage = vi
      .fn()
      .mockResolvedValueOnce(usageData({ documents: 10 }))
      .mockResolvedValueOnce(usageData({ documents: 11 }));
    await loaded(fakeApi(usage));
    expect(within(meterCard("Documents")).getByText("10 of 200 (5%)")).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(within(meterCard("Documents")).getByText("11 of 200 (5%)")).toBeTruthy());
    expect(usage).toHaveBeenCalledTimes(2);
  });

  it("Refresh does not double submit: two clicks in the same tick make one request, and the button is disabled while it runs", async () => {
    const second = deferred<unknown>();
    const usage = vi.fn().mockResolvedValueOnce(usageData()).mockReturnValueOnce(second.promise);
    await loaded(fakeApi(usage));
    const button = screen.getByRole("button", { name: "Refresh" });
    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(button);
    expect(usage).toHaveBeenCalledTimes(2); // 1 load + exactly 1 refresh
    await waitFor(() => expect((screen.getByRole("button", { name: /Refresh/ }) as HTMLButtonElement).disabled).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: /Refresh/ }));
    expect(usage).toHaveBeenCalledTimes(2);
    await act(async () => second.resolve(usageData({ documents: 7 })));
    await waitFor(() => expect((screen.getByRole("button", { name: "Refresh" }) as HTMLButtonElement).disabled).toBe(false));
    expect(usage).toHaveBeenCalledTimes(2);
  });

  it("a failed refresh keeps the old figures visible, shows the error, and Refresh works again", async () => {
    const usage = vi
      .fn()
      .mockResolvedValueOnce(usageData())
      .mockRejectedValueOnce(axiosError(503, { code: "service_unavailable" }))
      .mockResolvedValueOnce(usageData({ documents: 99 }));
    await loaded(fakeApi(usage));
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain(getDocumentError(axiosError(503, { code: "service_unavailable" }), "usage").message);
    expect(screen.getByRole("progressbar", { name: "Documents" })).toBeTruthy();
    await userEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(within(meterCard("Documents")).getByText("99 of 200 (49%)")).toBeTruthy());
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("no auto-polling: focus and visibility events do not trigger another request", async () => {
    const usage = vi.fn().mockResolvedValue(usageData());
    await loaded(fakeApi(usage));
    await act(async () => {
      window.dispatchEvent(new Event("focus"));
      document.dispatchEvent(new Event("visibilitychange"));
      window.dispatchEvent(new Event("online"));
    });
    expect(usage).toHaveBeenCalledTimes(1);
  });

  it("read-only: the page only ever touches api.usage, even when every button is pressed", async () => {
    const usage = vi.fn().mockResolvedValue(usageData());
    const touched: string[] = [];
    // like the real API function, the response goes through the schema (5g-1 added fields the page relies on)
    const parsed = () => Promise.resolve(usage()).then((d: unknown) => DocumentUsageSchema.parse(d));
    const api = new Proxy({ usage: parsed } as Record<string, unknown>, {
      get(target, prop) {
        touched.push(String(prop));
        return target[prop as string];
      },
    }) as unknown as DocumentsApi;
    await loaded(api);
    for (const button of screen.getAllByRole("button")) {
      if (button.closest("nav, aside, header")) continue;
      fireEvent.click(button);
    }
    expect(touched.length).toBeGreaterThan(0);
    expect(new Set(touched)).toEqual(new Set(["usage"]));
  });
});

describe("MaterialsUsagePage: mobile and accessibility", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("focus lands on the page heading after navigation", async () => {
    await loaded(fakeApi(vi.fn().mockResolvedValue(usageData())));
    const h1 = screen.getByRole("heading", { level: 1, name: "Usage and limits" });
    await waitFor(() => expect(document.activeElement).toBe(h1));
    expect(h1.getAttribute("tabindex")).toBe("-1");
  });

  it("there is one h1 and heading levels never skip", async () => {
    await loaded(fakeApi(vi.fn().mockResolvedValue(usageData())));
    const main = document.querySelector("main") as HTMLElement;
    const levels = within(main)
      .getAllByRole("heading")
      .map((h) => Number(h.tagName.substring(1)));
    expect(levels.filter((l) => l === 1)).toHaveLength(1);
    expect(levels[0]).toBe(1);
    for (let i = 1; i < levels.length; i += 1) {
      expect(levels[i] - levels[i - 1]).toBeLessThanOrEqual(1);
    }
  });

  it("phone layout markers: cards in one column first, limits as stacked rows, no horizontal scroll container, long text wraps", async () => {
    await loaded(fakeApi(vi.fn().mockResolvedValue(usageData())));
    const grid = screen.getByTestId("usage-cards");
    expect(grid.className).toMatch(/\bgrid-cols-1\b/);
    expect(grid.className).not.toMatch(/(^|\s)grid-cols-[2-9]\b/);
    const limits = screen.getByRole("region", { name: "Upload limits" });
    const rows = limits.querySelectorAll("[data-limit-row]");
    expect(rows.length).toBe(5);
    for (const row of Array.from(rows)) {
      expect(row.className).toMatch(/\bflex-col\b/);
    }
    const main = document.querySelector("main") as HTMLElement;
    expect(main.querySelector(".overflow-x-auto, .overflow-x-scroll, table")).toBeNull();
  });

  it("the Refresh button is a real button with a visible text name", async () => {
    await loaded(fakeApi(vi.fn().mockResolvedValue(usageData())));
    const button = screen.getByRole("button", { name: "Refresh" });
    expect(button.tagName).toBe("BUTTON");
    expect(button.getAttribute("type")).toBe("button");
  });

  it("there is no extra sidebar entry for usage, and a link back to the materials list", async () => {
    await loaded(fakeApi(vi.fn().mockResolvedValue(usageData())));
    const links = Array.from(document.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(links.filter((h) => h === "/admin/materials/usage")).toHaveLength(0);
    expect(links).toContain("/admin/materials");
  });
});

describe("usage access: menu entry and direct URL", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(academicApi, "getMyClasses").mockResolvedValue([]);
  });

  function listApi() {
    const usage = vi.fn().mockResolvedValue(usageData());
    const list = vi.fn().mockResolvedValue({ items: [], next_cursor: null });
    return { usage, list, api: { usage: () => usage().then((d: unknown) => DocumentUsageSchema.parse(d)), list } as unknown as DocumentsApi };
  }

  it("an admin sees the Usage link on the admin materials page", async () => {
    const { api } = listApi();
    renderAt("/admin/materials", api, "admin");
    const link = await screen.findByRole("link", { name: "Usage and limits" });
    expect(link.getAttribute("href")).toBe("/admin/materials/usage");
  });

  it("a teacher does not see the Usage link on the materials page", async () => {
    const { api } = listApi();
    renderAt("/teacher/materials", api, "teacher");
    await screen.findByRole("heading", { level: 1, name: "Course materials" });
    expect(screen.queryByRole("link", { name: "Usage and limits" })).toBeNull();
    expect(document.querySelector('a[href*="usage"]')).toBeNull();
  });

  it("even a teacher who somehow holds settings:manage gets no Usage link on the teacher materials page", async () => {
    const { api } = listApi();
    renderAt("/teacher/materials", api, "teacher-with-settings");
    await screen.findByRole("heading", { level: 1, name: "Course materials" });
    expect(screen.queryByRole("link", { name: "Usage and limits" })).toBeNull();
  });

  it("an admin role without settings:manage does not see the link", async () => {
    const { api } = listApi();
    renderAt("/admin/materials", api, "admin-no-settings");
    await screen.findByRole("heading", { level: 1, name: "Course materials" });
    expect(screen.queryByRole("link", { name: "Usage and limits" })).toBeNull();
  });

  for (const who of ["teacher", "teacher-with-settings", "student", "admin-no-settings", "settings-no-view"] as const) {
    it(`${who} who types /admin/materials/usage gets the 403 page and no request is made`, async () => {
      const { api, usage } = listApi();
      renderAt("/admin/materials/usage", api, who);
      expect(await screen.findByText(/\(403\)/)).toBeTruthy();
      expect(screen.queryByRole("heading", { level: 1, name: "Usage and limits" })).toBeNull();
      expect(screen.queryAllByRole("progressbar")).toHaveLength(0);
      expect(usage).not.toHaveBeenCalled();
    });
  }

  it("an admin reaches the page by URL", async () => {
    const { api, usage } = listApi();
    renderAt("/admin/materials/usage", api, "admin");
    expect(await screen.findByRole("heading", { level: 1, name: "Usage and limits" })).toBeTruthy();
    await waitFor(() => expect(usage).toHaveBeenCalledTimes(1));
  });
});
