import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within, configure } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MaterialsUsagePage } from "../pages/MaterialsUsagePage";
import type { DocumentsApi } from "../api/documentsApi";
import { DocumentUsageSchema } from "../schemas/documentSchemas";
import * as AuthContextModule from "@/features/auth/AuthContext";

// No test relies on a timer or on the default 1 s wait.
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
    versions_by_status: { queued: 1, processing: 2, ready: 3, published: 40, archived: 5, failed: 0 },
    uploads_last_hour: { tenant: 17, caller: 4, window_seconds: 3600 },
    ...overrides,
  };
}

function renderUsage(usage: ReturnType<typeof vi.fn>) {
  vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
    user: { id: "a1", isAdmin: true, roles: ["tenant_admin"], permissions: ["*"] },
    isAuthenticated: true,
    isLoading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  } as never);
  const api = {
    usage: () => Promise.resolve(usage()).then((data) => DocumentUsageSchema.parse(data)),
  } as unknown as DocumentsApi;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/admin/materials/usage"]}>
        <Routes>
          <Route path="/admin/materials/usage" element={<MaterialsUsagePage api={api} />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

async function loaded(usage: ReturnType<typeof vi.fn>) {
  const view = renderUsage(usage);
  await screen.findByRole("heading", { level: 1, name: "Usage and limits" });
  await waitFor(() => expect(screen.queryByText(/Loading usage/)).toBeNull());
  return view;
}

describe("5g-1 usage page: versions by status", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows the six statuses in plain words with their counts", async () => {
    await loaded(vi.fn().mockResolvedValue(usageData()));
    const region = screen.getByRole("region", { name: "Versions by status" });
    const expected: Array<[string, string]> = [
      ["Waiting", "1"],
      ["Processing", "2"],
      ["Draft", "3"],
      ["Published", "40"],
      ["Older", "5"],
      ["Failed", "0"],
    ];
    const rows = Array.from(region.querySelectorAll("[data-status-row]"));
    expect(rows).toHaveLength(6);
    expected.forEach(([label, count], index) => {
      expect(within(rows[index] as HTMLElement).getByText(label)).toBeTruthy();
      expect(within(rows[index] as HTMLElement).getByText(count)).toBeTruthy();
    });
  });

  it("a missing count says 'Not available right now' and is never shown as 0", async () => {
    await loaded(
      vi.fn().mockResolvedValue(usageData({ versions_by_status: { published: 4 } }))
    );
    const region = screen.getByRole("region", { name: "Versions by status" });
    expect(within(region).getAllByText("Not available right now")).toHaveLength(5);
    expect(within(region).queryByText("0")).toBeNull();
  });
});

describe("5g-1 usage page: upload attempts", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("shows both counts against their limits and says a rejected upload still counts", async () => {
    await loaded(vi.fn().mockResolvedValue(usageData()));
    const region = screen.getByRole("region", { name: "Upload attempts in the last 60 minutes" });
    expect(within(region).getByText("Your attempts: 4 of 20")).toBeTruthy();
    expect(within(region).getByText("Everyone in your organisation: 17 of 100")).toBeTruthy();
    expect(within(region).getByText(/A rejected upload still counts as an attempt\./)).toBeTruthy();
  });

  it("null counts say 'Not available right now' and NEVER 0", async () => {
    await loaded(
      vi.fn().mockResolvedValue(
        usageData({ uploads_last_hour: { tenant: null, caller: null, window_seconds: 3600 } })
      )
    );
    const region = screen.getByRole("region", { name: "Upload attempts in the last 60 minutes" });
    expect(within(region).getByText("Your attempts: Not available right now")).toBeTruthy();
    expect(within(region).getByText("Everyone in your organisation: Not available right now")).toBeTruthy();
    expect(region.textContent ?? "").not.toMatch(/: 0\b/);
  });

  it("only one null: the other count is still shown", async () => {
    await loaded(
      vi.fn().mockResolvedValue(usageData({ uploads_last_hour: { tenant: 9, caller: null } }))
    );
    const region = screen.getByRole("region", { name: "Upload attempts in the last 60 minutes" });
    expect(within(region).getByText("Your attempts: Not available right now")).toBeTruthy();
    expect(within(region).getByText("Everyone in your organisation: 9 of 100")).toBeTruthy();
  });

  it("a real zero is shown as 0", async () => {
    await loaded(
      vi.fn().mockResolvedValue(usageData({ uploads_last_hour: { tenant: 0, caller: 0 } }))
    );
    const region = screen.getByRole("region", { name: "Upload attempts in the last 60 minutes" });
    expect(within(region).getByText("Your attempts: 0 of 20")).toBeTruthy();
  });

  it("a zero or missing maximum: no division by zero, no NaN, no Infinity", async () => {
    await loaded(
      vi.fn().mockResolvedValue(
        usageData({
          limits: { ...LIMITS, uploads_per_user_per_hour: 0, uploads_per_tenant_per_hour: null },
          uploads_last_hour: { tenant: 9, caller: 3 },
        })
      )
    );
    const region = screen.getByRole("region", { name: "Upload attempts in the last 60 minutes" });
    expect(within(region).getByText("Your attempts: 3")).toBeTruthy();
    expect(within(region).getByText("Everyone in your organisation: 9")).toBeTruthy();
    expect(document.body.textContent ?? "").not.toMatch(/NaN|Infinity|undefined/);
  });

  it("the old 'not shown here' sentence is gone", async () => {
    await loaded(vi.fn().mockResolvedValue(usageData()));
    expect(screen.queryByText(/not shown here/i)).toBeNull();
  });
});

describe("5g-1 usage page: no polling, mobile layout", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("loads once and never again by itself", async () => {
    const usage = vi.fn().mockResolvedValue(usageData());
    await loaded(usage);
    expect(usage).toHaveBeenCalledTimes(1);
    // A visibility change or focus must not refetch either.
    window.dispatchEvent(new Event("focus"));
    document.dispatchEvent(new Event("visibilitychange"));
    expect(screen.getByRole("region", { name: "Upload attempts in the last 60 minutes" })).toBeTruthy();
    expect(usage).toHaveBeenCalledTimes(1);
  });

  it("the new sections use stacked, wrapping rows and no horizontal scroll classes", async () => {
    await loaded(vi.fn().mockResolvedValue(usageData()));
    for (const name of ["Versions by status", "Upload attempts in the last 60 minutes"]) {
      const region = screen.getByRole("region", { name });
      expect(region.innerHTML).not.toMatch(/overflow-x-(auto|scroll)/);
      expect(region.innerHTML).not.toMatch(/<table/);
      expect(region.innerHTML).toMatch(/break-words/);
    }
  });
});
