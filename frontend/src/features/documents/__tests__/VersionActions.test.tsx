import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MaterialDetailPage } from "../pages/MaterialDetailPage";
import { ToastProvider } from "@/components/ui/Toast";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentItem, DocumentVersionItem } from "../schemas/documentSchemas";
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

const THREE_HOURS_AGO = new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString();

function makeVersion(o: Partial<DocumentVersionItem> = {}): DocumentVersionItem {
  return {
    id: "v-3",
    document_id: "doc-1",
    version_no: 3,
    status: "failed",
    stage: null,
    error_code: "processing_failed",
    file_kind: "pdf",
    original_filename: "notes.pdf",
    size_bytes: 1000,
    page_count: null,
    chunk_count: null,
    flagged_chunk_count: null,
    created_at: THREE_HOURS_AGO,
    published_at: null,
    retryable: true,
    warnings: [],
    ...o,
  };
}

function makeDoc(latest: DocumentVersionItem, o: Partial<DocumentItem> = {}): DocumentItem {
  return {
    id: "doc-1",
    title: "Operating Systems",
    description: null,
    subject: { id: "s1", name: "Computer Science" },
    classes: [{ id: "c1", name: "Class 10A" }],
    created_by: "u1",
    created_at: THREE_HOURS_AGO,
    updated_at: THREE_HOURS_AGO,
    latest_version: latest,
    published_version: null,
    can_manage: true,
    blocked_reason: null,
    ...o,
  };
}

function makeApi(overrides: Partial<Record<keyof DocumentsApi, unknown>> = {}) {
  return {
    list: vi.fn(),
    get: vi.fn(),
    limits: vi.fn(),
    status: vi.fn().mockResolvedValue({
      document_id: "doc-1",
      version_id: "v-3",
      version_no: 3,
      status: "processing",
      stage: "parsing",
      error_code: null,
      retryable: false,
      updated_at: THREE_HOURS_AGO,
    }),
    upload: vi.fn(),
    retryVersion: vi.fn(),
    usage: vi.fn(),
    deleteVersion: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  } as unknown as DocumentsApi & {
    get: ReturnType<typeof vi.fn>;
    status: ReturnType<typeof vi.fn>;
    retryVersion: ReturnType<typeof vi.fn>;
    deleteVersion: ReturnType<typeof vi.fn>;
  };
}

function setUser(permissions: string[], roles: string[] = ["teacher"]) {
  vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
    user: { id: "u1", isAdmin: false, roles, permissions },
    isAuthenticated: true,
    isLoading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  });
}

function renderPage(api: DocumentsApi, layout: "scroll" | "stacked" = "scroll") {
  return render(
    <QueryClientProvider
      // mutations default to 3 retries here, so only an explicit retry: false in the hooks keeps a failed request single
      client={new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: 3, retryDelay: 1 } } })}
    >
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

function httpError(status: number, code: string, headers: Record<string, string> = {}) {
  return {
    isAxiosError: true,
    response: {
      status,
      headers,
      data: { detail: { code, message: "RAW SERVER TEXT 42" } },
    },
  };
}

const UPLOAD = ["document:view", "document:upload"];

describe("5f-4 retry and delete version", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    setUser(UPLOAD);
  });

  describe("retry", () => {
    it("failed + retryable shows Try again; failed + not retryable shows the reason and the corrected-file text, no button", async () => {
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion({ retryable: true })));
      const first = renderPage(api);
      expect(await screen.findByRole("button", { name: "Try again" })).toBeDefined();
      expect(screen.queryByText("Upload a corrected file as a new version")).toBeNull();
      first.unmount();

      const api2 = makeApi();
      api2.get.mockResolvedValue(
        makeDoc(makeVersion({ retryable: false, error_code: "parse_failed" }))
      );
      renderPage(api2);
      expect(await screen.findByText("Upload a corrected file as a new version")).toBeDefined();
      expect(
        screen.getByText(/The document could not be processed\. Try exporting it again/)
      ).toBeDefined();
      expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
      expect(screen.queryByRole("button", { name: /corrected file/i })).toBeNull();
      // The status route is not used to decide anything
      expect(api2.status).not.toHaveBeenCalled();
    });

    it("clicking Try again calls retryVersion once; 202 shows Waiting and polling resumes even for an old version", async () => {
      const user = userEvent.setup();
      const failed = makeDoc(makeVersion());
      const queued = makeDoc(makeVersion({ status: "queued", retryable: null, error_code: null }));
      const api = makeApi();
      api.get.mockResolvedValueOnce(failed).mockResolvedValue(queued);
      api.status.mockResolvedValue({
        document_id: "doc-1",
        version_id: "v-3",
        version_no: 3,
        status: "queued",
        stage: null,
        error_code: null,
        retryable: false,
        updated_at: THREE_HOURS_AGO,
      });
      api.retryVersion.mockResolvedValue(
        makeVersion({ status: "queued", retryable: null, error_code: null })
      );
      renderPage(api);

      await user.click(await screen.findByRole("button", { name: "Try again" }));

      expect(api.retryVersion).toHaveBeenCalledTimes(1);
      expect(api.retryVersion).toHaveBeenCalledWith("doc-1", "v-3");
      expect(await screen.findByText("Waiting")).toBeDefined();
      expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
      // The version was created 3 hours ago (past the 65 minute stop), yet polling restarts
      await waitFor(() => expect(api.status).toHaveBeenCalledWith("doc-1", "v-3"));
    });

    it("polling that ended in failed resumes after a retry (the old terminal state is forgotten)", async () => {
      const user = userEvent.setup();
      const now = new Date().toISOString();
      const processing = makeDoc(
        makeVersion({ status: "processing", stage: "parsing", retryable: null, error_code: null, created_at: now })
      );
      const failed = makeDoc(makeVersion({ created_at: now }));
      const queued = makeDoc(makeVersion({ status: "queued", retryable: null, error_code: null, created_at: now }));
      const api = makeApi();
      api.get.mockResolvedValueOnce(processing).mockResolvedValueOnce(failed).mockResolvedValue(queued);
      const statusOf = (status: string) => ({
        document_id: "doc-1",
        version_id: "v-3",
        version_no: 3,
        status,
        stage: null,
        error_code: status === "failed" ? "processing_failed" : null,
        retryable: status === "failed",
        updated_at: now,
      });
      api.status.mockResolvedValueOnce(statusOf("failed")).mockResolvedValue(statusOf("queued"));
      api.retryVersion.mockResolvedValue(makeVersion({ status: "queued", retryable: null }));
      renderPage(api);

      // polling saw the failure, the page refetched and now offers Try again
      await user.click(await screen.findByRole("button", { name: "Try again" }));
      expect(await screen.findByText("Waiting")).toBeDefined();
      // one status call was the failure; resumed polling makes at least one more after the retry
      await waitFor(() => expect(api.status.mock.calls.length).toBeGreaterThanOrEqual(2));
      expect(screen.queryByText("Failed")).toBeNull();
    }, 10000);

    const RETRY_ERRORS: Array<[string, number, string, Record<string, string>?]> = [
      ["version_not_retryable", 409, "This file failed for a reason a retry cannot fix. Upload a corrected file as a new version."],
      ["document_processing_in_progress", 409, "Another version of this material is being processed. Try again when it is done."],
      ["duplicate_version", 409, "The same file is already an active version."],
      ["retry_limit_reached", 409, "This file has been retried too many times. Upload it again as a new version."],
      ["retry_rate_limit_exceeded", 429, "You have retried too often. Please try again in 45 seconds.", { "retry-after": "45" }],
      ["retry_version_rate_limit_exceeded", 429, "You have retried too often. Please try again in 45 seconds.", { "retry-after": "45" }],
      ["service_unavailable", 503, "Uploads, search or storage are temporarily unavailable. Please try again in a few minutes."],
      ["permission_denied", 403, "You do not have permission to do this."],
      ["not_found", 404, "We could not find this material. It may have been removed."],
    ];

    for (const [code, status, expected, headers] of RETRY_ERRORS) {
      it(`retry error ${code} (${status}) shows its mapped text in a page alert and never the raw code or server text`, async () => {
        const user = userEvent.setup();
        const api = makeApi();
        api.get.mockResolvedValue(makeDoc(makeVersion()));
        api.retryVersion.mockRejectedValue(httpError(status, code, headers));
        renderPage(api);

        await user.click(await screen.findByRole("button", { name: "Try again" }));

        const alerts = await screen.findAllByRole("alert");
        const match = alerts.find((a) => a.textContent?.includes(expected));
        expect(match, `no alert with: ${expected}`).toBeDefined();
        expect(document.body.textContent).not.toContain("RAW SERVER TEXT 42");
        expect(document.body.textContent).not.toContain(code);
        // The row is usable again (not stuck in loading)
        expect((screen.getByRole("button", { name: "Try again" }) as HTMLButtonElement).disabled).toBe(false);
        // An error is never retried automatically
        expect(api.retryVersion).toHaveBeenCalledTimes(1);
      });
    }

    it("403 document_manage_requires_all_classes shows the mapped text, refetches the document and does not retry", async () => {
      const user = userEvent.setup();
      const api = makeApi();
      api.get
        .mockResolvedValueOnce(makeDoc(makeVersion()))
        .mockResolvedValue(makeDoc(makeVersion(), { can_manage: false, blocked_reason: "document_manage_requires_all_classes" }));
      api.retryVersion.mockRejectedValue(httpError(403, "document_manage_requires_all_classes"));
      renderPage(api);

      await user.click(await screen.findByRole("button", { name: "Try again" }));

      const alerts = await screen.findAllByRole("alert");
      expect(
        alerts.some((a) =>
          a.textContent?.includes(
            "You can view this material, but you teach this subject in only some of its classes, so you cannot change it. Ask an administrator."
          )
        )
      ).toBe(true);
      await waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThanOrEqual(2));
      await waitFor(() =>
        expect((screen.getByRole("button", { name: "Try again" }) as HTMLButtonElement).disabled).toBe(true)
      );
      expect(api.retryVersion).toHaveBeenCalledTimes(1);
    });

    it("no double submit: two quick clicks send one request and the button shows loading and is disabled", async () => {
      const user = userEvent.setup();
      let resolve!: (v: unknown) => void;
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion()));
      api.retryVersion.mockImplementation(() => new Promise((r) => (resolve = r)));
      renderPage(api);

      const btn = await screen.findByRole("button", { name: "Try again" });
      await user.click(btn);
      await user.click(btn);

      expect(api.retryVersion).toHaveBeenCalledTimes(1);
      await waitFor(() => expect((btn as HTMLButtonElement).disabled).toBe(true));
      // that row's other actions are disabled as well
      expect((screen.getByRole("button", { name: /Delete version/ }) as HTMLButtonElement).disabled).toBe(true);
      resolve(makeVersion({ status: "queued", retryable: null }));
    });
  });

  describe("same-tick double clicks (before React re-renders)", () => {
    it("two click events in one tick on Try again send one request", async () => {
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion()));
      api.retryVersion.mockImplementation(() => new Promise(() => undefined));
      renderPage(api);

      const btn = await screen.findByRole("button", { name: "Try again" });
      act(() => {
        btn.click();
        btn.click();
      });
      // the mutation starts the request asynchronously: wait for it, then make sure no second one follows
      await waitFor(() => expect(api.retryVersion).toHaveBeenCalled());
      await new Promise((r) => setTimeout(r, 30));
      expect(api.retryVersion).toHaveBeenCalledTimes(1);
    });

    it("two click events in one tick on the confirm button send one delete request", async () => {
      const user = userEvent.setup();
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion()));
      api.deleteVersion.mockImplementation(() => new Promise(() => undefined));
      renderPage(api);

      await user.click(await screen.findByRole("button", { name: /Delete version/ }));
      const dialog = await screen.findByRole("dialog");
      const confirm = within(dialog).getByRole("button", { name: "Delete version" });
      act(() => {
        confirm.click();
        confirm.click();
      });
      await waitFor(() => expect(api.deleteVersion).toHaveBeenCalled());
      await new Promise((r) => setTimeout(r, 30));
      expect(api.deleteVersion).toHaveBeenCalledTimes(1);
    });
  });

  describe("delete version", () => {
    it("opens a confirmation naming the version, focuses Cancel, and Cancel does nothing", async () => {
      const user = userEvent.setup();
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion({ status: "ready", retryable: null, flagged_chunk_count: 0 })));
      renderPage(api);

      await user.click(await screen.findByRole("button", { name: /Delete version/ }));

      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByText("Delete version 3 (notes.pdf)?")).toBeDefined();
      await waitFor(() => expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "Cancel" })));

      await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(api.deleteVersion).not.toHaveBeenCalled();
    });

    it("Escape and the backdrop close the dialog without deleting", async () => {
      const user = userEvent.setup();
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion({ status: "ready", retryable: null, flagged_chunk_count: 0 })));
      renderPage(api);

      const opener = await screen.findByRole("button", { name: /Delete version/ });
      await user.click(opener);
      await screen.findByRole("dialog");
      await user.keyboard("{Escape}");
      expect(screen.queryByRole("dialog")).toBeNull();
      // focus goes back to the button that opened it
      await waitFor(() => expect(document.activeElement).toBe(opener));

      await user.click(opener);
      const dialog = await screen.findByRole("dialog");
      const backdrop = dialog.querySelector('[aria-hidden="true"].fixed') as HTMLElement;
      expect(backdrop).not.toBeNull();
      await user.click(backdrop);
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(api.deleteVersion).not.toHaveBeenCalled();
    });

    it("the file name in the confirmation is plain text and is truncated safely", async () => {
      const user = userEvent.setup();
      const longName = `<img src=x onerror=alert(1)>${"a".repeat(120)}‮fdp.pdf`;
      const api = makeApi();
      api.get.mockResolvedValue(
        makeDoc(makeVersion({ status: "ready", retryable: null, flagged_chunk_count: 0, original_filename: longName }))
      );
      renderPage(api);

      await user.click(await screen.findByRole("button", { name: /Delete version/ }));
      const dialog = await screen.findByRole("dialog");
      const heading = within(dialog).getByRole("heading", { level: 3 });
      expect(dialog.querySelector("img")).toBeNull();
      expect(heading.textContent!.length).toBeLessThan(90);
      expect(heading.textContent).not.toContain("‮");
      expect(heading.textContent).toContain("Delete version 3 (");
    });

    it("confirm deletes once (two clicks, one request), closes the dialog and refetches", async () => {
      const user = userEvent.setup();
      let resolve!: (v: unknown) => void;
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion()));
      api.deleteVersion.mockImplementation(() => new Promise((r) => (resolve = r)));
      renderPage(api);

      await user.click(await screen.findByRole("button", { name: /Delete version/ }));
      const dialog = await screen.findByRole("dialog");
      const confirm = within(dialog).getByRole("button", { name: "Delete version" });
      await user.click(confirm);
      await user.click(confirm);

      expect(api.deleteVersion).toHaveBeenCalledTimes(1);
      expect(api.deleteVersion).toHaveBeenCalledWith("doc-1", "v-3");
      // while running: the row's own actions are disabled and the dialog cannot be cancelled by accident
      expect((screen.getAllByRole("button", { name: "Try again" })[0] as HTMLButtonElement).disabled).toBe(true);

      const callsBefore = api.get.mock.calls.length;
      resolve(undefined);
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThan(callsBefore));
    });

    it("404 on delete is treated as already removed: dialog closes, no error, list refetched", async () => {
      const user = userEvent.setup();
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion()));
      api.deleteVersion.mockRejectedValue(httpError(404, "not_found"));
      renderPage(api);

      await user.click(await screen.findByRole("button", { name: /Delete version/ }));
      const dialog = await screen.findByRole("dialog");
      const callsBefore = api.get.mock.calls.length;
      await user.click(within(dialog).getByRole("button", { name: "Delete version" }));

      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(screen.queryByText("We could not find this material. It may have been removed.")).toBeNull();
      await waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThan(callsBefore));
    });

    const DELETE_ERRORS: Array<[string, number, string]> = [
      ["version_not_deletable", 409, "Only drafts and failed versions can be deleted."],
      ["document_manage_requires_all_classes", 403, "You can view this material, but you teach this subject in only some of its classes, so you cannot change it. Ask an administrator."],
      ["permission_denied", 403, "You do not have permission to do this."],
      ["service_unavailable", 503, "Uploads, search or storage are temporarily unavailable. Please try again in a few minutes."],
    ];

    for (const [code, status, expected] of DELETE_ERRORS) {
      it(`delete error ${code} (${status}) is shown inside the dialog as an alert; the dialog stays open`, async () => {
        const user = userEvent.setup();
        const api = makeApi();
        api.get.mockResolvedValue(makeDoc(makeVersion()));
        api.deleteVersion.mockRejectedValue(httpError(status, code));
        renderPage(api);

        await user.click(await screen.findByRole("button", { name: /Delete version/ }));
        const dialog = await screen.findByRole("dialog");
        await user.click(within(dialog).getByRole("button", { name: "Delete version" }));

        const alert = await within(dialog).findByRole("alert");
        expect(alert.textContent).toContain(expected);
        expect(document.body.textContent).not.toContain("RAW SERVER TEXT 42");
        expect(screen.getByRole("dialog")).toBeDefined();
        expect(api.deleteVersion).toHaveBeenCalledTimes(1);
      });
    }

    it("after a 403 manage error the document is refetched and the dialog's confirm button is disabled", async () => {
      const user = userEvent.setup();
      const api = makeApi();
      api.get
        .mockResolvedValueOnce(makeDoc(makeVersion()))
        .mockResolvedValue(makeDoc(makeVersion(), { can_manage: false, blocked_reason: "document_manage_requires_all_classes" }));
      api.deleteVersion.mockRejectedValue(httpError(403, "document_manage_requires_all_classes"));
      renderPage(api);

      await user.click(await screen.findByRole("button", { name: /Delete version/ }));
      const dialog = await screen.findByRole("dialog");
      await user.click(within(dialog).getByRole("button", { name: "Delete version" }));

      await within(dialog).findByRole("alert");
      await waitFor(() =>
        expect((within(dialog).getByRole("button", { name: "Delete version" }) as HTMLButtonElement).disabled).toBe(true)
      );
      expect(api.deleteVersion).toHaveBeenCalledTimes(1);
    });

    it("published and archived versions have no Delete version control", async () => {
      const api = makeApi();
      const published = makeVersion({ id: "v-2", version_no: 2, status: "published", retryable: null, error_code: null });
      api.get.mockResolvedValue(makeDoc(published, { published_version: published }));
      renderPage(api);

      await screen.findByText("v2");
      expect(screen.queryByRole("button", { name: /Delete version/ })).toBeNull();
      expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
    });
  });

  describe("gating", () => {
    it("can_manage false: Try again and Delete are disabled and the lock reason is on the row", async () => {
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion(), { can_manage: false, blocked_reason: "document_manage_requires_all_classes" }));
      renderPage(api);

      const retry = (await screen.findByRole("button", { name: "Try again" })) as HTMLButtonElement;
      const del = screen.getByRole("button", { name: /Delete version/ }) as HTMLButtonElement;
      expect(retry.disabled).toBe(true);
      expect(del.disabled).toBe(true);
      const lockTexts = screen.getAllByText(
        "You teach this subject in only some of the linked classes, so you can only view it"
      );
      expect(lockTexts.length).toBeGreaterThan(0);
    });

    it("a user with only document:view (student, custom role) sees none of the controls", async () => {
      setUser(["document:view"], ["student"]);
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion({ retryable: false })));
      renderPage(api);

      await screen.findByText("v3");
      expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
      expect(screen.queryByRole("button", { name: /Delete version/ })).toBeNull();
      expect(screen.queryByText("Upload a corrected file as a new version")).toBeNull();
      expect(screen.queryByRole("columnheader", { name: "Actions" })).toBeNull();
    });
  });

  describe("mobile (360 px) and keyboard", () => {
    it("action buttons carry the 44px size class; destructive and safe actions are separate elements in the card footer", async () => {
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion()));
      const { container } = renderPage(api, "stacked");

      await screen.findAllByText("Operating Systems");
      const mobile = container.querySelector("div.md\\:hidden") as HTMLElement;
      expect(mobile).not.toBeNull();
      const retry = within(mobile).getByRole("button", { name: "Try again" });
      const del = within(mobile).getByRole("button", { name: /Delete version/ });

      for (const b of [retry, del]) {
        expect(b.className).toContain("min-h-[44px]");
        expect(b.className).toContain("w-full");
        expect(b.getAttribute("style")).toBeNull();
      }
      expect(retry).not.toBe(del);
      expect(retry.parentElement).not.toBe(del.parentElement);
      expect(retry.contains(del)).toBe(false);
      expect(del.contains(retry)).toBe(false);
      // Delete sits in its own separated block (border) below the safe action
      expect(del.parentElement!.className).toContain("border-t");
      // no hex colours and no inline styles anywhere in the row actions
      expect(mobile.innerHTML).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(mobile.innerHTML).not.toMatch(/\sstyle=/);
    });

    it("on mobile the row actions are in the stacked card, not only in a hover-only or scrolled table", async () => {
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion()));
      const { container } = renderPage(api, "stacked");
      await screen.findAllByText("Operating Systems");
      const mobile = container.querySelector("div.md\\:hidden") as HTMLElement;
      expect(within(mobile).getAllByRole("button", { name: /Try again|Delete version/ })).toHaveLength(2);
      expect(mobile.innerHTML).not.toContain("group-hover");
      expect(mobile.innerHTML).not.toContain("opacity-0");
    });

    it("the shared .btn class is 44px high on touch screens (the buttons inherit it)", () => {
      const css = readFileSync(resolve(process.cwd(), "src/styles/theme.css"), "utf8");
      const btnBlock = /\.btn\s*\{[^}]*\}/.exec(css)?.[0] ?? "";
      expect(btnBlock).toContain("min-h-[44px]");
    });

    it("keyboard: Tab moves from Try again to Delete version, Enter opens the dialog, Tab stays inside it, Escape closes it", async () => {
      const user = userEvent.setup();
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion()));
      renderPage(api);

      const retry = await screen.findByRole("button", { name: "Try again" });
      const del = screen.getByRole("button", { name: /Delete version/ });
      retry.focus();
      await user.tab();
      expect(document.activeElement).toBe(del);

      await user.keyboard("{Enter}");
      const dialog = await screen.findByRole("dialog");
      await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
      for (let i = 0; i < 6; i++) {
        await user.tab();
        expect(dialog.contains(document.activeElement)).toBe(true);
      }
      await user.keyboard("{Escape}");
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(api.deleteVersion).not.toHaveBeenCalled();
    });
  });
});
