import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axios, { type InternalAxiosRequestConfig } from "axios";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MaterialDetailPage } from "../pages/MaterialDetailPage";
import { UploadDialog } from "../components/UploadDialog";
import { ToastProvider } from "@/components/ui/Toast";
import { createDocumentsApi, type DocumentsApi } from "../api/documentsApi";
import type { DocumentItem, DocumentVersionItem } from "../schemas/documentSchemas";
import { authApi } from "@/api/auth";
import { setAccessToken } from "@/api/client";
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

const NOW = new Date().toISOString();

function makeVersion(o: Partial<DocumentVersionItem> = {}): DocumentVersionItem {
  return {
    id: "v-2",
    document_id: "doc-1",
    version_no: 2,
    status: "published",
    stage: null,
    error_code: null,
    file_kind: "pdf",
    original_filename: "notes.pdf",
    size_bytes: 1000,
    page_count: 3,
    chunk_count: 3,
    flagged_chunk_count: 0,
    created_at: NOW,
    published_at: NOW,
    retryable: null,
    warnings: [],
    ...o,
  };
}

function makeDoc(latest: DocumentVersionItem | null, o: Partial<DocumentItem> = {}): DocumentItem {
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
    published_version: latest && latest.status === "published" ? latest : null,
    can_manage: true,
    blocked_reason: null,
    ...o,
  };
}

function makeApi(overrides: Partial<Record<keyof DocumentsApi, unknown>> = {}) {
  return {
    list: vi.fn(),
    get: vi.fn(),
    limits: vi.fn().mockResolvedValue({
      max_file_bytes: 50 * 1024 * 1024,
      max_pdf_pages: 200,
      max_pptx_slides: 100,
      uploads_per_user_per_hour: 20,
      uploads_per_tenant_per_hour: 100,
      accepted_extensions: ["pdf", "docx", "pptx", "txt", "md"],
    }),
    status: vi.fn().mockResolvedValue({
      document_id: "doc-1",
      version_id: "v-3",
      version_no: 3,
      status: "processing",
      stage: "parsing",
      error_code: null,
      retryable: false,
      updated_at: NOW,
    }),
    upload: vi.fn(),
    uploadVersion: vi.fn(),
    retryVersion: vi.fn(),
    deleteVersion: vi.fn(),
    deleteDocument: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  } as unknown as DocumentsApi & {
    get: ReturnType<typeof vi.fn>;
    limits: ReturnType<typeof vi.fn>;
    status: ReturnType<typeof vi.fn>;
    uploadVersion: ReturnType<typeof vi.fn>;
    deleteDocument: ReturnType<typeof vi.fn>;
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

function newClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: 3, retryDelay: 1 } },
  });
}

function renderPage(api: DocumentsApi) {
  return render(
    <QueryClientProvider client={newClient()}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/teacher/materials/doc-1"]}>
          <Routes>
            <Route path="/teacher/materials/:documentId" element={<MaterialDetailPage api={api} />} />
            <Route path="/teacher/materials" element={<div>MATERIALS LIST ROUTE</div>} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>
  );
}

function renderDialog(
  api: DocumentsApi,
  props: Partial<React.ComponentProps<typeof UploadDialog>> = {}
) {
  const onClose = vi.fn();
  const onSuccess = vi.fn();
  const utils = render(
    <QueryClientProvider client={newClient()}>
      <MemoryRouter>
        <ToastProvider>
          <UploadDialog
            isOpen
            onClose={onClose}
            onSuccess={onSuccess}
            api={api}
            mode="version"
            documentId="doc-1"
            {...props}
          />
        </ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
  return { ...utils, onClose, onSuccess };
}

function httpError(status: number, code: string, extra: Record<string, unknown> = {}, headers = {}) {
  return {
    isAxiosError: true,
    response: { status, headers, data: { detail: { code, message: "RAW SERVER TEXT 42", ...extra } } },
  };
}

const PDF = () => new File(["content"], "second.pdf", { type: "application/pdf" });
const fileInput = () => document.querySelector("input[type='file']") as HTMLInputElement;
const UPLOAD = ["document:view", "document:upload"];
const ALL = ["document:view", "document:upload", "document:delete", "document:publish"];

const LIMITS_OK = {
  max_file_bytes: 50 * 1024 * 1024,
  max_pdf_pages: 200,
  max_pptx_slides: 100,
  uploads_per_user_per_hour: 20,
  uploads_per_tenant_per_hour: 100,
  accepted_extensions: ["pdf", "docx", "pptx", "txt", "md"],
};
const VERSION_OK = makeVersion({ id: "v-3", version_no: 3, status: "queued", published_at: null });

beforeEach(() => {
  vi.restoreAllMocks();
  setUser(ALL);
  Element.prototype.scrollIntoView = vi.fn();
});

// ---------------------------------------------------------------------------------------------
describe("UploadDialog in new-version mode", () => {
  it("shows only the file field: no title, subject or classes, and says it is a new version", async () => {
    renderDialog(makeApi());
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Upload new version" })).toBeDefined();
    expect(screen.queryByLabelText(/title/i)).toBeNull();
    expect(screen.queryByLabelText(/subject/i)).toBeNull();
    expect(screen.queryByText(/^Classes/)).toBeNull();
    expect(screen.queryAllByRole("checkbox")).toHaveLength(0);
    expect(fileInput()).toBeDefined();
    expect(screen.getByRole("button", { name: /^upload$/i })).toBeDefined();
  });

  it("does not load the class list in this mode", async () => {
    const { academicApi } = await import("@/features/academic/api/academicApi");
    const spy = vi.spyOn(academicApi, "getMyClasses").mockResolvedValue([]);
    renderDialog(makeApi());
    await screen.findByRole("dialog");
    expect(spy).not.toHaveBeenCalled();
  });

  it("sends only `file` to uploadVersion(documentId, ...), then closes with a toast", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.uploadVersion.mockResolvedValue(VERSION_OK);
    const { onClose, onSuccess } = renderDialog(api);

    await user.upload(fileInput(), PDF());
    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    await waitFor(() => expect(api.uploadVersion).toHaveBeenCalledTimes(1));
    const [docId, payload] = api.uploadVersion.mock.calls[0];
    expect(docId).toBe("doc-1");
    expect(Object.keys(payload as object)).toEqual(["file"]);
    expect((payload as { file: File }).file.name).toBe("second.pdf");
    expect(api.upload).not.toHaveBeenCalled();
    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(onSuccess).toHaveBeenCalledWith(VERSION_OK);
    expect(await screen.findByText("Uploaded. We are reading the file now.")).toBeDefined();
  });

  it("through the REAL api and request builder: POST .../documents/doc-1/versions with a body that has only `file`", async () => {
    const user = userEvent.setup();
    let captured: InternalAxiosRequestConfig | undefined;
    const client = axios.create({
      baseURL: "/api/v1",
      adapter: async (config) => {
        // The dialog also asks for the upload limits; answer each URL with its own shape.
        if (config.url?.endsWith("/documents/limits")) {
          return {
            data: LIMITS_OK,
            status: 200,
            statusText: "OK",
            headers: {},
            config,
          };
        }
        captured = config;
        return { data: VERSION_OK, status: 202, statusText: "Accepted", headers: {}, config };
      },
    });
    renderDialog(createDocumentsApi(client));

    await user.upload(fileInput(), PDF());
    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    await waitFor(() => expect(captured).toBeDefined());
    expect(captured!.method?.toLowerCase()).toBe("post");
    expect(axios.getUri(captured!)).toBe("/api/v1/documents/doc-1/versions");
    const body = captured!.data as FormData;
    expect(Array.from(new Set(body.keys()))).toEqual(["file"]);
  });

  it("client checks: a spreadsheet is refused beside the file zone and nothing is sent", async () => {
    // applyAccept: false, otherwise the test browser would silently drop the file before our check runs
    const user = userEvent.setup({ applyAccept: false });
    const api = makeApi();
    renderDialog(api);
    await user.upload(fileInput(), new File(["a,b"], "grades.xlsx"));
    expect(await screen.findByText(/Spreadsheets are not accepted here/)).toBeDefined();
    await user.click(screen.getByRole("button", { name: /^upload$/i }));
    expect(api.uploadVersion).not.toHaveBeenCalled();
  });

  it("submitting without a file shows the file message and sends nothing", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    renderDialog(api);
    await user.click(screen.getByRole("button", { name: /^upload$/i }));
    expect(await screen.findByText("Please select a file to upload.")).toBeDefined();
    expect(api.uploadVersion).not.toHaveBeenCalled();
  });

  it("shows a progress bar while sending, Cancel aborts the request signal, and a second click does not send again", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    let signal: AbortSignal | undefined;
    let onProgress: ((p: number) => void) | undefined;
    api.uploadVersion.mockImplementation((_id: string, _form: unknown, opts: { signal: AbortSignal; onProgress: (p: number) => void }) => {
      signal = opts.signal;
      onProgress = opts.onProgress;
      return new Promise(() => undefined);
    });
    const { onClose } = renderDialog(api);
    await user.upload(fileInput(), PDF());
    const send = screen.getByRole("button", { name: /^upload$/i });
    await user.click(send);
    await user.click(send);

    await waitFor(() => expect(api.uploadVersion).toHaveBeenCalledTimes(1));
    onProgress?.(40);
    const bar = await screen.findByRole("progressbar");
    await waitFor(() => expect(bar.getAttribute("value")).toBe("40"));
    expect(screen.getByText("40%")).toBeDefined();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(signal?.aborted).toBe(true);
    expect(onClose).toHaveBeenCalled();
  });

  it("token freshness: an access token that expires in 20 s is refreshed before the request is sent", async () => {
    const user = userEvent.setup();
    const exp = (s: number) =>
      `${btoa('{"alg":"HS256"}')}.${btoa(JSON.stringify({ exp: Math.floor(Date.now() / 1000) + s }))}.sig`;
    setAccessToken(exp(20));
    const order: string[] = [];
    vi.spyOn(authApi, "refresh").mockImplementation(async () => {
      order.push("refresh");
      return { access_token: exp(3600) };
    });
    const api = makeApi();
    api.uploadVersion.mockImplementation(async () => {
      order.push("upload");
      return VERSION_OK;
    });
    renderDialog(api);
    await user.upload(fileInput(), PDF());
    await user.click(screen.getByRole("button", { name: /^upload$/i }));
    await waitFor(() => expect(order).toEqual(["refresh", "upload"]));
    setAccessToken(null as never);
  });

  it("duplicate_version: plain message, a 'Show it' control, no server text; Show it closes the dialog and hands over the version id", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.uploadVersion.mockRejectedValue(httpError(409, "duplicate_version", { version_id: "v-2" }));
    const onShowVersion = vi.fn();
    const { onClose } = renderDialog(api, { onShowVersion });

    await user.upload(fileInput(), PDF());
    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("This exact file is already an active version of this material.")).toBeDefined();
    expect(screen.queryByText(/RAW SERVER TEXT/)).toBeNull();
    expect(onClose).not.toHaveBeenCalled();

    await user.click(within(alert).getByRole("button", { name: /show it/i }));
    expect(onShowVersion).toHaveBeenCalledWith("v-2");
    expect(onClose).toHaveBeenCalled();
  });

  it("duplicate_version without a version id offers no 'Show it' control", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.uploadVersion.mockRejectedValue(httpError(409, "duplicate_version"));
    renderDialog(api, { onShowVersion: vi.fn() });
    await user.upload(fileInput(), PDF());
    await user.click(screen.getByRole("button", { name: /^upload$/i }));
    await screen.findByText("This exact file is already an active version of this material.");
    expect(screen.queryByRole("button", { name: /show it/i })).toBeNull();
  });

  it("document_processing_in_progress: guidance text in the dialog alert, dialog stays open, file stays selected", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.uploadVersion.mockRejectedValue(httpError(409, "document_processing_in_progress"));
    const { onClose } = renderDialog(api);
    await user.upload(fileInput(), PDF());
    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/still being processed or waiting to be published\. Publish or delete it first\./)).toBeDefined();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByText("second.pdf")).toBeDefined();
  });

  const ERROR_ROWS: Array<[number, string, string, "alert" | "file"]> = [
    [422, "empty_file", "The file is empty.", "file"],
    [422, "encrypted_file", "Password-protected files are not supported. Remove the password and upload again.", "file"],
    [413, "file_too_large", "This file is larger than your organisation allows. Make it smaller or split it.", "file"],
    [422, "quota_storage_exceeded", "Your organisation has reached its storage limit. Ask an administrator.", "alert"],
    [403, "document_manage_requires_all_classes", "You can view this material, but you teach this subject in only some of its classes, so you cannot change it. Ask an administrator.", "alert"],
    [404, "not_found", "We could not find this material. It may have been removed.", "alert"],
    [503, "service_unavailable", "Uploads, search or storage are temporarily unavailable. Please try again in a few minutes.", "alert"],
  ];
  for (const [status, code, text, place] of ERROR_ROWS) {
    it(`error ${code} (${status}) shows the fixed text ${place === "file" ? "beside the file zone" : "in the dialog alert"}, never the server text`, async () => {
      const user = userEvent.setup();
      const api = makeApi();
      api.uploadVersion.mockRejectedValue(httpError(status, code));
      const { onClose } = renderDialog(api);
      await user.upload(fileInput(), PDF());
      await user.click(screen.getByRole("button", { name: /^upload$/i }));

      expect(await screen.findByText(text)).toBeDefined();
      expect(screen.queryByText(/RAW SERVER TEXT/)).toBeNull();
      expect(onClose).not.toHaveBeenCalled();
      const inAlert = screen.queryAllByRole("alert").some((a) => a.textContent?.includes(text));
      if (place === "alert") expect(inAlert).toBe(true);
      else expect(screen.queryByText("Upload failed")).toBeNull();
    });
  }

  it("429 shows the wait time from Retry-After", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.uploadVersion.mockRejectedValue(httpError(429, "rate_limit_exceeded", {}, { "retry-after": "45" }));
    renderDialog(api);
    await user.upload(fileInput(), PDF());
    await user.click(screen.getByRole("button", { name: /^upload$/i }));
    expect(await screen.findByText(/You have uploaded too many files this hour\. Please try again in 45 seconds\./)).toBeDefined();
  });

  it("a network failure says the upload did not finish and never claims it was saved", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.uploadVersion.mockRejectedValue({ isAxiosError: true, request: {}, message: "Network Error" });
    renderDialog(api);
    await user.upload(fileInput(), PDF());
    await user.click(screen.getByRole("button", { name: /^upload$/i }));
    expect(await screen.findByText("The upload did not finish. Check your connection and try again")).toBeDefined();
  });

  it("keyboard: Tab stays inside the dialog and Escape closes it", async () => {
    const user = userEvent.setup();
    const { onClose } = renderDialog(makeApi());
    const dialog = await screen.findByRole("dialog");
    for (let i = 0; i < 6; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("the new-document mode is unchanged: it still shows title, subject and classes", async () => {
    renderDialog(makeApi(), { mode: "document", documentId: undefined });
    expect(await screen.findByLabelText(/title/i)).toBeDefined();
    expect(screen.getByLabelText(/subject/i)).toBeDefined();
  });
});

// ---------------------------------------------------------------------------------------------
describe("Detail page: New version action", () => {
  it("shows an enabled 'New version' button when the latest version is published; click opens the new-version dialog", async () => {
    const user = userEvent.setup();
    setUser(UPLOAD);
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    renderPage(api);
    const btn = (await screen.findByRole("button", { name: /new version/i })) as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    expect(btn.className).toContain("min-h-[44px]");
    await user.click(btn);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "Upload new version" })).toBeDefined();
    expect(within(dialog).queryByLabelText(/title/i)).toBeNull();
  });

  for (const status of ["queued", "processing", "ready"]) {
    it(`latest version ${status}: button is disabled with the reason "Publish or delete the draft first" linked by aria-describedby`, async () => {
      setUser(UPLOAD);
      const api = makeApi();
      api.get.mockResolvedValue(makeDoc(makeVersion({ id: "v-3", version_no: 3, status, published_at: null })));
      renderPage(api);
      const btn = (await screen.findByRole("button", { name: /new version/i })) as HTMLButtonElement;
      expect(btn.disabled).toBe(true);
      const reason = document.getElementById(btn.getAttribute("aria-describedby") ?? "");
      expect(reason?.textContent).toContain("Publish or delete the draft first");
    });
  }

  it("can_manage false: disabled with the lock reason (not the draft reason), for new version and delete", async () => {
    setUser(ALL);
    const api = makeApi();
    api.get.mockResolvedValue(
      makeDoc(makeVersion(), { can_manage: false, blocked_reason: "document_manage_requires_all_classes" })
    );
    renderPage(api);
    const nv = (await screen.findByRole("button", { name: /new version/i })) as HTMLButtonElement;
    const del = screen.getByRole("button", { name: /delete (this )?material|delete document/i }) as HTMLButtonElement;
    expect(nv.disabled).toBe(true);
    expect(del.disabled).toBe(true);
    expect(document.getElementById(nv.getAttribute("aria-describedby") ?? "")?.textContent).toContain(
      "only some of the linked classes"
    );
    expect(document.getElementById(del.getAttribute("aria-describedby") ?? "")?.textContent).toContain(
      "only some of the linked classes"
    );
  });

  it("no document:upload: no New version button at all; student (no permissions): no new version and no delete", async () => {
    setUser(["document:view"]);
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    const first = renderPage(api);
    await screen.findAllByText("Chapter 4 notes");
    expect(screen.queryByRole("button", { name: /new version/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /delete (this )?material|delete document/i })).toBeNull();
    first.unmount();

    vi.restoreAllMocks();
    Element.prototype.scrollIntoView = vi.fn();
    setUser([], ["student"]);
    const api2 = makeApi();
    api2.get.mockResolvedValue(makeDoc(makeVersion()));
    renderPage(api2);
    await screen.findAllByText("Chapter 4 notes");
    expect(screen.queryByRole("button", { name: /new version/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /delete/i })).toBeNull();
  });

  it("after a successful upload the old published version stays Published next to the new Waiting one (both chips)", async () => {
    const user = userEvent.setup();
    setUser(UPLOAD);
    const published = makeVersion();
    const queued = makeVersion({ id: "v-3", version_no: 3, status: "queued", published_at: null });
    const api = makeApi();
    api.get.mockResolvedValueOnce(makeDoc(published)).mockResolvedValue(makeDoc(queued, { published_version: published }));
    api.uploadVersion.mockResolvedValue(queued);
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

    await user.click(await screen.findByRole("button", { name: /new version/i }));
    const dialog = await screen.findByRole("dialog");
    await user.upload(within(dialog).getByLabelText(/file/i, { selector: "input" }), PDF());
    await user.click(within(dialog).getByRole("button", { name: /^upload$/i }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThan(1));
    expect((await screen.findAllByText("Published")).length).toBeGreaterThan(0);
    expect((await screen.findAllByText("Waiting")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("v2").length).toBeGreaterThan(0);
    expect(screen.getAllByText("v3").length).toBeGreaterThan(0);
  });

  it("duplicate_version 'Show it': the dialog closes, the row of that version is scrolled to and focused on the same page", async () => {
    const user = userEvent.setup();
    setUser(UPLOAD);
    const published = makeVersion();
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(published));
    api.uploadVersion.mockRejectedValue(httpError(409, "duplicate_version", { version_id: "v-2" }));
    renderPage(api);

    await user.click(await screen.findByRole("button", { name: /new version/i }));
    const dialog = await screen.findByRole("dialog");
    await user.upload(within(dialog).getByLabelText(/file/i, { selector: "input" }), PDF());
    await user.click(within(dialog).getByRole("button", { name: /^upload$/i }));
    await user.click(await within(dialog).findByRole("button", { name: /show it/i }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => {
      const active = document.activeElement as HTMLElement;
      expect(active.getAttribute("data-version-row")).toBe("v-2");
    });
    expect(Element.prototype.scrollIntoView).toHaveBeenCalled();
  });

  it("document_processing_in_progress refetches the document so the button state catches up", async () => {
    const user = userEvent.setup();
    setUser(UPLOAD);
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    api.uploadVersion.mockRejectedValue(httpError(409, "document_processing_in_progress"));
    renderPage(api);
    await user.click(await screen.findByRole("button", { name: /new version/i }));
    const dialog = await screen.findByRole("dialog");
    await user.upload(within(dialog).getByLabelText(/file/i, { selector: "input" }), PDF());
    const before = api.get.mock.calls.length;
    await user.click(within(dialog).getByRole("button", { name: /^upload$/i }));
    await within(dialog).findByText(/Publish or delete it first/);
    await waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThan(before));
  });

  it("keyboard: Tab reaches New version, Enter opens the dialog, Escape closes it and focus returns", async () => {
    const user = userEvent.setup();
    setUser(UPLOAD);
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    renderPage(api);
    const btn = await screen.findByRole("button", { name: /new version/i });
    btn.focus();
    expect(document.activeElement).toBe(btn);
    await user.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog");
    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(document.activeElement).toBe(btn));
  });
});

// ---------------------------------------------------------------------------------------------
describe("Detail page: delete document", () => {
  async function openDeleteDialog(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: /delete (this )?material|delete document/i }));
    return screen.findByRole("dialog");
  }

  it("the dialog names the document, explains the effect, focuses Cancel, and is separated from the safe action", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    renderPage(api);

    const trigger = await screen.findByRole("button", { name: /delete (this )?material|delete document/i });
    // destructive block is visually separated from New version and is touch sized
    expect(trigger.className).toContain("min-h-[44px]");
    expect(trigger.parentElement?.className).toMatch(/border-t/);

    await user.click(trigger);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("Delete 'Chapter 4 notes'?")).toBeDefined();
    expect(
      within(dialog).getByText("Students will no longer see it and it cannot be restored from here.")
    ).toBeDefined();
    await waitFor(() => expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "Cancel" })));
    // bottom sheet on phones (anchored to the bottom edge, centred from sm up); buttons fill the width
    expect(dialog.className).toContain("items-end");
    expect(dialog.className).toContain("sm:items-center");
    expect(within(dialog).getByRole("button", { name: "Cancel" }).className).toContain("min-h-[44px]");
    expect(within(dialog).getByRole("button", { name: "Delete document" }).className).toContain("min-h-[44px]");
  });

  it("the title in the dialog has bidi and control characters removed", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion(), { title: "Safe\u202Egnp.exe\u0000 notes" }));
    renderPage(api);
    const dialog = await openDeleteDialog(user);
    expect(within(dialog).getByText("Delete 'Safegnp.exe notes'?")).toBeDefined();
  });

  it("Cancel, Escape and the backdrop close it without deleting", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    renderPage(api);
    let dialog = await openDeleteDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    dialog = await openDeleteDialog(user);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.deleteDocument).not.toHaveBeenCalled();
  });

  it("confirm deletes once (double click), shows a toast and goes back to the materials list", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    let resolve!: () => void;
    api.deleteDocument.mockImplementation(() => new Promise<void>((r) => (resolve = r)));
    renderPage(api);
    const dialog = await openDeleteDialog(user);
    const confirm = within(dialog).getByRole("button", { name: "Delete document" });
    await user.click(confirm);
    await user.click(confirm);
    await waitFor(() => expect(api.deleteDocument).toHaveBeenCalledTimes(1));
    expect(api.deleteDocument).toHaveBeenCalledWith("doc-1");
    // cannot be cancelled while it runs
    expect((within(dialog).getByRole("button", { name: "Cancel" }) as HTMLButtonElement).disabled).toBe(true);

    resolve();
    expect(await screen.findByText("MATERIALS LIST ROUTE")).toBeDefined();
    expect(await screen.findByText("Material deleted.")).toBeDefined();
    expect(api.deleteDocument).toHaveBeenCalledTimes(1);
  });

  it("a double submit in the same tick still sends one request", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    api.deleteDocument.mockImplementation(() => new Promise(() => undefined));
    renderPage(api);
    const dialog = await openDeleteDialog(user);
    const confirm = within(dialog).getByRole("button", { name: "Delete document" });
    confirm.click();
    confirm.click();
    await waitFor(() => expect(api.deleteDocument).toHaveBeenCalledTimes(1));
  });

  it("404 counts as already removed: toast says so, back to the list, no error", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    api.deleteDocument.mockRejectedValue(httpError(404, "not_found"));
    renderPage(api);
    const dialog = await openDeleteDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Delete document" }));
    expect(await screen.findByText("MATERIALS LIST ROUTE")).toBeDefined();
    expect(await screen.findByText("That material was already removed.")).toBeDefined();
    expect(screen.queryByText("We could not find this material. It may have been removed.")).toBeNull();
  });

  it("403 document_manage_requires_all_classes: message in the dialog, the document is refetched, no retry, still on the page", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.get
      .mockResolvedValueOnce(makeDoc(makeVersion()))
      .mockResolvedValue(
        makeDoc(makeVersion(), { can_manage: false, blocked_reason: "document_manage_requires_all_classes" })
      );
    api.deleteDocument.mockRejectedValue(httpError(403, "document_manage_requires_all_classes"));
    renderPage(api);
    const dialog = await openDeleteDialog(user);
    const before = api.get.mock.calls.length;
    await user.click(within(dialog).getByRole("button", { name: "Delete document" }));

    expect(
      await within(dialog).findByText(
        "You can view this material, but you teach this subject in only some of its classes, so you cannot change it. Ask an administrator."
      )
    ).toBeDefined();
    await waitFor(() => expect(api.get.mock.calls.length).toBeGreaterThan(before));
    await new Promise((r) => setTimeout(r, 50));
    expect(api.deleteDocument).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("MATERIALS LIST ROUTE")).toBeNull();
    // the refreshed can_manage turns the confirm button off
    await waitFor(() =>
      expect((within(dialog).getByRole("button", { name: "Delete document" }) as HTMLButtonElement).disabled).toBe(true)
    );
  });

  it("other errors show the fixed text only and keep the dialog open", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    api.deleteDocument.mockRejectedValue(httpError(503, "service_unavailable"));
    renderPage(api);
    const dialog = await openDeleteDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Delete document" }));
    expect(
      await within(dialog).findByText(
        "Uploads, search or storage are temporarily unavailable. Please try again in a few minutes."
      )
    ).toBeDefined();
    expect(screen.queryByText(/RAW SERVER TEXT/)).toBeNull();
    expect(screen.queryByText("MATERIALS LIST ROUTE")).toBeNull();
  });

  it("keyboard: Tab to Delete, Enter opens, Tab stays inside, Enter on the focused Cancel closes", async () => {
    const user = userEvent.setup();
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion()));
    renderPage(api);
    const trigger = await screen.findByRole("button", { name: /delete (this )?material|delete document/i });
    trigger.focus();
    await user.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(document.activeElement).toBe(within(dialog).getByRole("button", { name: "Cancel" })));
    await user.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    await user.tab({ shift: true });
    await user.keyboard("{Enter}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(api.deleteDocument).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------------------------
describe("Versions table: file name (leftover B)", () => {
  it("a bidi-spoofed file name is shown without the override character, wrapped with break-words", async () => {
    setUser(UPLOAD);
    const spoof = "invoice\u202Egnp.exe";
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion({ original_filename: spoof })));
    renderPage(api);
    const cells = await screen.findAllByText("invoicegnp.exe");
    expect(cells.length).toBeGreaterThan(0);
    for (const c of cells) {
      expect(c.textContent).not.toContain("\u202E");
      expect(c.className).toContain("break-words");
    }
    expect(screen.queryByText(spoof)).toBeNull();
  });

  it("control characters and path parts are removed; a long name is kept whole for the detail page", async () => {
    setUser(UPLOAD);
    const long = `${"a".repeat(120)}.pdf`;
    const api = makeApi();
    api.get.mockResolvedValue(makeDoc(makeVersion({ original_filename: `C:\\temp\\${long}\u0007` })));
    renderPage(api);
    const cells = await screen.findAllByText(long);
    expect(cells.length).toBeGreaterThan(0);
  });
});
