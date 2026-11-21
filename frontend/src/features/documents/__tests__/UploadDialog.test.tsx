import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { UploadDialog } from "../components/UploadDialog";
import { MaterialsListPage } from "../pages/MaterialsListPage";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentVersionItem } from "../schemas/documentSchemas";
import { academicApi, type MyClassItem } from "@/features/academic/api/academicApi";
import { authApi } from "@/api/auth";
import { getAccessToken, setAccessToken } from "@/api/client";
import * as AuthContextModule from "@/features/auth/AuthContext";
import * as documentsApiClient from "../api/documentsClient";
import { ToastProvider } from "@/components/ui/Toast";

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

function renderWithProviders(ui: React.ReactElement, client = createTestQueryClient()) {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/teacher/materials"]}>
        <ToastProvider>{ui}</ToastProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

const mockClasses: MyClassItem[] = [
  {
    id: "cls-1",
    name: "Class 10A",
    kind: "standard",
    subjects: [
      { id: "sub-cs", name: "Computer Science" },
      { id: "sub-math", name: "Mathematics" },
    ],
  },
  {
    id: "cls-2",
    name: "Class 10B",
    kind: "standard",
    subjects: [{ id: "sub-cs", name: "Computer Science" }],
  },
  {
    id: "cls-3",
    name: "Class 11A",
    kind: "standard",
    subjects: [{ id: "sub-physics", name: "Physics" }],
  },
];

function makeLimitsApi(): DocumentsApi {
  return {
    retryVersion: vi.fn(),
    uploadVersion: vi.fn(),
    deleteDocument: vi.fn(),
    deleteVersion: vi.fn(),
    publishVersion: vi.fn(),
    flags: vi.fn(),
    list: vi.fn(),
    get: vi.fn(),
    limits: vi.fn().mockResolvedValue({
      max_file_bytes: 50 * 1024 * 1024,
      max_pdf_pages: 200,
      max_pptx_slides: 100,
      uploads_per_user_per_hour: 20,
      uploads_per_tenant_per_hour: 100,
      accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
    }),
    status: vi.fn(),
    upload: vi.fn(),
  };
}

describe("UploadDialog and Upload flow", () => {
  let mockUser: any;

  beforeEach(() => {
    vi.restoreAllMocks();
    mockUser = {
      id: "teacher-1",
      isAdmin: false,
      roles: ["teacher"],
      permissions: ["document:view", "document:upload"],
      exp: Math.floor(Date.now() / 1000) + 3600, // 1 hour valid
    };

    vi.spyOn(AuthContextModule, "useAuth").mockImplementation(() => ({
      user: mockUser,
      isAuthenticated: true,
      isLoading: false,
      login: vi.fn(),
      register: vi.fn(),
      logout: vi.fn(),
    }));

    vi.spyOn(academicApi, "getMyClasses").mockResolvedValue(mockClasses);
  });

  it("upload material button is hidden for user without document:upload", async () => {
    mockUser.permissions = ["document:view"]; // Missing document:upload

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn(),
    };

    renderWithProviders(<MaterialsListPage api={fakeApi} />);

    await waitFor(() => {
      expect(screen.queryByRole("button", { name: /upload material/i })).toBeNull();
    });
  });

  it("upload material button is visible for user with document:upload (positive test using findBy)", async () => {
    mockUser.permissions = ["document:view", "document:upload"];

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn().mockResolvedValue({ items: [], next_cursor: null }),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn(),
    };

    renderWithProviders(<MaterialsListPage api={fakeApi} />);

    const uploadBtn = await screen.findByRole("button", { name: /upload material/i });
    expect(uploadBtn).toBeDefined();
  });

  it("keeps focus in Title field when typing and focus lands on first focusable control only once on open", async () => {
    const user = userEvent.setup();
    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} />);

    // On open, focus lands on the first focusable control (Close dialog button)
    await waitFor(() => {
      const closeBtn = screen.getByRole("button", { name: /close dialog/i });
      expect(document.activeElement).toBe(closeBtn);
    });

    const titleInput = screen.getByLabelText(/title/i);
    await user.click(titleInput);
    expect(document.activeElement).toBe(titleInput);

    await user.type(titleInput, "Chapter 4 notes");
    expect(document.activeElement).toBe(titleInput);
    expect((titleInput as HTMLInputElement).value).toBe("Chapter 4 notes");
  });

  it("works without injected api: fetches limits, helper text shows tenant limit, and refuses file exceeding tenant limit", async () => {
    const limitsSpy = vi.spyOn(documentsApiClient.documentsApi, "limits").mockResolvedValue({
      max_file_bytes: 10 * 1024 * 1024, // 10 MB
      max_pdf_pages: 100,
      max_pptx_slides: 50,
      uploads_per_user_per_hour: 10,
      uploads_per_tenant_per_hour: 50,
      accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
    });

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} />);

    // Helper text shows "Max 10 MB."
    await waitFor(() => {
      expect(screen.getByText(/max 10 mb/i)).toBeDefined();
    });
    expect(limitsSpy).toHaveBeenCalled();

    const input = document.querySelector("input[type='file']") as HTMLInputElement;

    // 11 MB file exceeds 10 MB tenant limit
    const elevenMbFile = new File([""], "test.pdf", { type: "application/pdf" });
    Object.defineProperty(elevenMbFile, "size", { value: 11 * 1024 * 1024 });

    fireEvent.change(input, { target: { files: [elevenMbFile] } });

    await waitFor(() => {
      expect(
        screen.getByText(/this file is larger than your organisation allows\. make it smaller or split it\./i)
      ).toBeDefined();
    });
  });

  it("falls back to 50 MB limit and helper text when limits query is loading or failed", async () => {
    vi.spyOn(documentsApiClient.documentsApi, "limits").mockRejectedValue(new Error("Network error"));

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} />);

    // Helper text falls back to 50 MB
    await waitFor(() => {
      expect(screen.getByText(/max 50 mb/i)).toBeDefined();
    });

    const input = document.querySelector("input[type='file']") as HTMLInputElement;

    // 11 MB file should NOT be refused when falling back to 50 MB limit
    const elevenMbFile = new File([""], "test.pdf", { type: "application/pdf" });
    Object.defineProperty(elevenMbFile, "size", { value: 11 * 1024 * 1024 });

    fireEvent.change(input, { target: { files: [elevenMbFile] } });

    // Ensure error does not appear for 11 MB file
    expect(
      screen.queryByText(/this file is larger than your organisation allows/i)
    ).toBeNull();
  });

  it("limits class list to chosen subject and user pairs from /me/classes", async () => {
    const user = userEvent.setup();

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} />);

    // Wait for subjects to load from /me/classes
    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    // Select Computer Science
    const subjectSelect = screen.getByLabelText(/subject/i);
    await user.selectOptions(subjectSelect, "sub-cs");

    // Class 10A and Class 10B offer CS; Class 11A offers Physics only
    await waitFor(() => {
      expect(screen.getByLabelText(/class 10a/i)).toBeDefined();
      expect(screen.getByLabelText(/class 10b/i)).toBeDefined();
      expect(screen.queryByLabelText(/class 11a/i)).toBeNull();
    });
  });

  it("refuses wrong file types, empty files, and over-50-MB files before sending", async () => {
    const uploadMock = vi.fn();

    renderWithProviders(
      <UploadDialog
        isOpen={true}
        onClose={vi.fn()}
        api={{
          retryVersion: vi.fn(),
          uploadVersion: vi.fn(),
          deleteDocument: vi.fn(),
          deleteVersion: vi.fn(),
          publishVersion: vi.fn(),
          flags: vi.fn(),
          list: vi.fn(),
          get: vi.fn(),
          limits: vi.fn().mockResolvedValue({
            max_file_bytes: 50 * 1024 * 1024,
            max_pdf_pages: 200,
            max_pptx_slides: 100,
            uploads_per_user_per_hour: 20,
            uploads_per_tenant_per_hour: 100,
            accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
          }),
          status: vi.fn(),
          upload: uploadMock,
        }}
      />
    );

    await waitFor(() => {
      expect(screen.getByLabelText(/title/i)).toBeDefined();
    });

    const input = document.querySelector("input[type='file']") as HTMLInputElement;

    // 1. Spreadsheet / CSV check
    const csvFile = new File(["a,b,c"], "data.csv", { type: "text/csv" });
    fireEvent.change(input, { target: { files: [csvFile] } });

    await waitFor(() => {
      expect(
        screen.getByText(/spreadsheets are not accepted here; export it as pdf or docx/i)
      ).toBeDefined();
    });
    expect(uploadMock).not.toHaveBeenCalled();

    // 2. Empty file check
    const emptyFile = new File([], "empty.pdf", { type: "application/pdf" });
    fireEvent.change(input, { target: { files: [emptyFile] } });
    await waitFor(() => {
      expect(screen.getByText(/the file is empty/i)).toBeDefined();
    });
    expect(uploadMock).not.toHaveBeenCalled();

    // 3. Over-50MB check
    const bigFile = new File([""], "huge.pdf", { type: "application/pdf" });
    Object.defineProperty(bigFile, "size", { value: 50 * 1024 * 1024 + 10 });
    fireEvent.change(input, { target: { files: [bigFile] } });
    await waitFor(() => {
      expect(screen.getByText(/larger than your organisation allows/i)).toBeDefined();
    });
    expect(uploadMock).not.toHaveBeenCalled();
  });

  it("sends exact multipart body, invalidates documents query on success, closes dialog, calls onSuccess, and displays toasts", async () => {
    const user = userEvent.setup();
    const onCloseMock = vi.fn();
    const onSuccessMock = vi.fn();
    const testQueryClient = createTestQueryClient();
    const invalidateSpy = vi.spyOn(testQueryClient, "invalidateQueries");

    let capturedPayload: any = null;
    const fakeVersion: DocumentVersionItem = {
      id: "v-new",
      document_id: "doc-new",
      version_no: 1,
      status: "queued",
      stage: null,
      error_code: null,
      file_kind: "pdf",
      original_filename: "lecture.pdf",
      size_bytes: 1024,
      page_count: null,
      chunk_count: null,
      flagged_chunk_count: null,
      created_at: "2026-11-21T10:00:00Z",
      published_at: null,
      retryable: null,
      warnings: ["duplicate_content"],
    };

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockImplementation((form: any) => {
        capturedPayload = form;
        return Promise.resolve(fakeVersion);
      }),
    };

    renderWithProviders(
      <UploadDialog
        isOpen={true}
        onClose={onCloseMock}
        onSuccess={onSuccessMock}
        api={fakeApi}
      />,
      testQueryClient
    );

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    // Fill title
    const titleInput = screen.getByLabelText(/title/i);
    await user.type(titleInput, "Computer Systems Overview");

    // Select subject
    const subjectSelect = screen.getByLabelText(/subject/i);
    await user.selectOptions(subjectSelect, "sub-cs");

    // Check Class 10A and Class 10B
    const class10A = await screen.findByLabelText(/class 10a/i);
    const class10B = await screen.findByLabelText(/class 10b/i);
    await user.click(class10A);
    await user.click(class10B);

    // Upload file
    const file = new File(["dummy pdf content"], "lecture.pdf", { type: "application/pdf" });
    const input = document.querySelector("input[type='file']") as HTMLInputElement;
    await user.upload(input, file);

    // Submit
    const uploadBtn = screen.getByRole("button", { name: /^upload$/i });
    expect((uploadBtn as HTMLButtonElement).disabled).toBe(false);
    await user.click(uploadBtn);

    await waitFor(() => {
      expect(fakeApi.upload).toHaveBeenCalled();
      expect(onCloseMock).toHaveBeenCalled();
      expect(onSuccessMock).toHaveBeenCalledWith(fakeVersion);
    });

    // Check exact fields sent
    expect(capturedPayload.title).toBe("Computer Systems Overview");
    expect(capturedPayload.subject_id).toBe("sub-cs");
    expect(capturedPayload.class_ids).toEqual(["cls-1", "cls-2"]);
    expect(capturedPayload.file.name).toBe("lecture.pdf");

    // Check list invalidated
    expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["documents"] });

    // Check toasts in ToastProvider
    expect(await screen.findByText("Uploaded. We are reading the file now.")).toBeDefined();
    expect(
      await screen.findByText(
        "This file matches another material in your organisation. You can still publish it."
      )
    ).toBeDefined();
    expect(await screen.findByText("Duplicate content notice")).toBeDefined();
  });

  it("handles cancel button, progress rendering, asserts AbortSignal is aborted, and prevents double submit", async () => {
    const user = userEvent.setup();
    const onCloseMock = vi.fn();

    let resolveUpload: any;
    const uploadPromise = new Promise((resolve) => {
      resolveUpload = resolve;
    });

    let capturedSignal: AbortSignal | undefined;
    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockImplementation((_form, options) => {
        capturedSignal = options?.signal;
        options?.onProgress?.(45);
        return uploadPromise;
      }),
    };

    renderWithProviders(
      <UploadDialog isOpen={true} onClose={onCloseMock} api={fakeApi} />
    );

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "Test In Flight");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["data"], "test.pdf", { type: "application/pdf" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    const uploadBtn = screen.getByRole("button", { name: /^upload$/i });
    // Double submit click
    await user.click(uploadBtn);
    await user.click(uploadBtn);

    expect(fakeApi.upload).toHaveBeenCalledTimes(1);

    // Shows progress and disables submit to prevent double submit
    await waitFor(() => {
      expect(screen.getByText("45%")).toBeDefined();
      expect((uploadBtn as HTMLButtonElement).disabled).toBe(true);
    });

    // Cancel while uploading aborts signal and closes
    const cancelBtn = screen.getByRole("button", { name: /cancel/i });
    await user.click(cancelBtn);
    expect(onCloseMock).toHaveBeenCalled();
    expect(capturedSignal?.aborted).toBe(true);

    resolveUpload({});
  });

  it("shows field-level error for invalid_title and keeps form values", async () => {
    const user = userEvent.setup();

    const error422 = {
      response: {
        status: 422,
        data: {
          detail: {
            code: "invalid_title",
            message: "The title must not be empty (and at most 200 characters).",
          },
        },
      },
    };

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockRejectedValue(error422),
    };

    renderWithProviders(
      <UploadDialog isOpen={true} onClose={vi.fn()} api={fakeApi} />
    );

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "Valid title but server rejects");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["content"], "notes.pdf", { type: "application/pdf" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    // Error message appears beside title field
    await waitFor(() => {
      expect(
        screen.getByText(/the title must not be empty \(and at most 200 characters\)/i)
      ).toBeDefined();
    });

    // Form kept its values
    expect((screen.getByLabelText(/title/i) as HTMLInputElement).value).toBe(
      "Valid title but server rejects"
    );
    expect((screen.getByLabelText(/subject/i) as HTMLSelectElement).value).toBe("sub-cs");
  });

  it("shows plain message for duplicate_version conflict (409) with only version_id and no 'Open it' link", async () => {
    const user = userEvent.setup();

    const error409 = {
      response: {
        status: 409,
        data: {
          detail: {
            code: "duplicate_version",
            message: "This exact file is already an active version of this material.",
            version_id: "v-existing-1",
          },
        },
      },
    };

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockRejectedValue(error409),
    };

    renderWithProviders(
      <UploadDialog isOpen={true} onClose={vi.fn()} api={fakeApi} />
    );

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "Duplicate material");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["content"], "notes.pdf", { type: "application/pdf" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/this exact file is already an active version of this material/i)
      ).toBeDefined();
      expect(screen.queryByRole("link", { name: /open it/i })).toBeNull();
    });
  });

  it("network drop mid-upload displays 'The upload did not finish. Check your connection and try again'", async () => {
    const user = userEvent.setup();

    const networkError = {
      isAxiosError: true,
      message: "Network Error",
      code: "ERR_NETWORK",
      request: {},
    };

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockRejectedValue(networkError),
    };

    renderWithProviders(
      <UploadDialog isOpen={true} onClose={vi.fn()} api={fakeApi} />
    );

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "Network drop test");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["content"], "notes.pdf", { type: "application/pdf" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    await waitFor(() => {
      expect(
        screen.getByText(/the upload did not finish\. check your connection and try again/i)
      ).toBeDefined();
    });
  });

  it("displays file-code error at file zone and keeps form fields and file selected", async () => {
    const user = userEvent.setup();
    const error422 = {
      response: {
        status: 422,
        data: {
          detail: {
            code: "corrupt_archive",
          },
        },
      },
    };

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockRejectedValue(error422),
    };

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} api={fakeApi} />);

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "My Corrupt Material");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["bad zip"], "damaged.docx", { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    // File-code error appears at file zone
    await waitFor(() => {
      expect(
        screen.getByText("This file looks damaged or is not a valid Office file.")
      ).toBeDefined();
    });

    // Form fields and file still there
    expect((screen.getByLabelText(/title/i) as HTMLInputElement).value).toBe("My Corrupt Material");
    expect((screen.getByLabelText(/subject/i) as HTMLSelectElement).value).toBe("sub-cs");
    expect((screen.getByLabelText(/class 10a/i) as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText("damaged.docx")).toBeDefined();
  });

  it("displays quota error in the Alert and keeps form fields and file selected", async () => {
    const user = userEvent.setup();
    const error422 = {
      response: {
        status: 422,
        data: {
          detail: {
            code: "quota_storage_exceeded",
          },
        },
      },
    };

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockRejectedValue(error422),
    };

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} api={fakeApi} />);

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "Storage Quota Test");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["some content"], "doc.pdf", { type: "application/pdf" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    // Quota error appears in Alert
    await waitFor(() => {
      expect(
        screen.getByText("Your organisation has reached its storage limit. Ask an administrator.")
      ).toBeDefined();
    });

    // Form fields and file still there
    expect((screen.getByLabelText(/title/i) as HTMLInputElement).value).toBe("Storage Quota Test");
    expect((screen.getByLabelText(/subject/i) as HTMLSelectElement).value).toBe("sub-cs");
    expect((screen.getByLabelText(/class 10a/i) as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText("doc.pdf")).toBeDefined();
  });

  it("displays 429 wait text and keeps form fields and file selected", async () => {
    const user = userEvent.setup();
    const error429 = {
      response: {
        status: 429,
        headers: { "retry-after": "30" },
        data: {
          detail: {
            code: "rate_limit_exceeded",
          },
        },
      },
    };

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockRejectedValue(error429),
    };

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} api={fakeApi} />);

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "Rate Limit Test");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["some content"], "doc.pdf", { type: "application/pdf" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    // 429 shows wait text
    await waitFor(() => {
      expect(
        screen.getByText(/You have uploaded too many files this hour\. Please try again in 30 seconds\./i)
      ).toBeDefined();
    });

    // Form fields and file still there
    expect((screen.getByLabelText(/title/i) as HTMLInputElement).value).toBe("Rate Limit Test");
    expect((screen.getByLabelText(/subject/i) as HTMLSelectElement).value).toBe("sub-cs");
    expect((screen.getByLabelText(/class 10a/i) as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText("doc.pdf")).toBeDefined();
  });

  it("displays too_many_classes error beside Classes and keeps form fields and file selected", async () => {
    const user = userEvent.setup();
    const error422 = {
      response: {
        status: 422,
        data: {
          detail: {
            code: "too_many_classes",
          },
        },
      },
    };

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockRejectedValue(error422),
    };

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} api={fakeApi} />);

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "Too Many Classes Test");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["some content"], "doc.pdf", { type: "application/pdf" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    // too_many_classes appears beside Classes
    await waitFor(() => {
      expect(
        screen.getByText("A material can be linked to at most 50 classes.")
      ).toBeDefined();
    });

    // Form fields and file still there
    expect((screen.getByLabelText(/title/i) as HTMLInputElement).value).toBe("Too Many Classes Test");
    expect((screen.getByLabelText(/subject/i) as HTMLSelectElement).value).toBe("sub-cs");
    expect((screen.getByLabelText(/class 10a/i) as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText("doc.pdf")).toBeDefined();
  });

  function makeJwt(expSecondsFromNow: number): string {
    const exp = Math.floor(Date.now() / 1000) + expSecondsFromNow;
    const header = btoa(JSON.stringify({ alg: "HS256", typ: "JWT" }));
    const payload = btoa(JSON.stringify({ sub: "user-1", exp }));
    return `${header}.${payload}.sig`;
  }

  it("does not refresh token when getAccessToken() exp is fresh (> 60s), even if AuthUser.exp is stale", async () => {
    const user = userEvent.setup();
    mockUser.exp = Math.floor(Date.now() / 1000) - 100; // Stale in AuthUser!
    setAccessToken(makeJwt(3600)); // Fresh (1 hour) in getAccessToken()!

    const refreshSpy = vi.spyOn(authApi, "refresh");

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockResolvedValue({
        id: "v-ok",
        document_id: "doc-ok",
        version_no: 1,
        status: "queued",
        stage: null,
        error_code: null,
        file_kind: "pdf",
        original_filename: "doc.pdf",
        size_bytes: 100,
        created_at: "2026-11-21T10:00:00Z",
        warnings: [],
      }),
    };

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} api={fakeApi} />);

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "Fresh Token Test");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["content"], "doc.pdf", { type: "application/pdf" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    await waitFor(() => {
      expect(fakeApi.upload).toHaveBeenCalled();
      expect(refreshSpy).not.toHaveBeenCalled();
    });
  });

  it("refreshes token when getAccessToken() exp is near (< 60s) and uses the refreshed token", async () => {
    const user = userEvent.setup();
    setAccessToken(makeJwt(20)); // Near (20s) in getAccessToken()!

    const refreshedJwt = makeJwt(3600);
    const refreshSpy = vi.spyOn(authApi, "refresh").mockImplementation(async () => {
      setAccessToken(refreshedJwt);
      return { access_token: refreshedJwt };
    });

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn().mockImplementation(() => {
        expect(getAccessToken()).toBe(refreshedJwt);
        return Promise.resolve({
          id: "v-ok",
          document_id: "doc-ok",
          version_no: 1,
          status: "queued",
          stage: null,
          error_code: null,
          file_kind: "pdf",
          original_filename: "doc.pdf",
          size_bytes: 100,
          created_at: "2026-11-21T10:00:00Z",
          warnings: [],
        });
      }),
    };

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} api={fakeApi} />);

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "Near Token Test");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["content"], "doc.pdf", { type: "application/pdf" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    await waitFor(() => {
      expect(refreshSpy).toHaveBeenCalled();
      expect(fakeApi.upload).toHaveBeenCalled();
    });
  });

  it("if token refresh fails with 401, does not send upload and shows 'Your session has ended. Please sign in again.'", async () => {
    const user = userEvent.setup();
    setAccessToken(makeJwt(15)); // Expiring in 15 seconds

    const error401 = {
      response: {
        status: 401,
        data: {
          detail: {
            code: "invalid_token",
          },
        },
      },
    };
    vi.spyOn(authApi, "refresh").mockRejectedValue(error401);

    const fakeApi: DocumentsApi = {
      retryVersion: vi.fn(),
      uploadVersion: vi.fn(),
      deleteDocument: vi.fn(),
      deleteVersion: vi.fn(),
      publishVersion: vi.fn(),
      flags: vi.fn(),
      list: vi.fn(),
      get: vi.fn(),
      limits: vi.fn().mockResolvedValue({
        max_file_bytes: 50 * 1024 * 1024,
        max_pdf_pages: 200,
        max_pptx_slides: 100,
        uploads_per_user_per_hour: 20,
        uploads_per_tenant_per_hour: 100,
        accepted_extensions: [".pdf", ".docx", ".pptx", ".txt", ".md"],
      }),
      status: vi.fn(),
      upload: vi.fn(),
    };

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} api={fakeApi} />);

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    await user.type(screen.getByLabelText(/title/i), "Failed Refresh Test");
    await user.selectOptions(screen.getByLabelText(/subject/i), "sub-cs");
    await user.click(await screen.findByLabelText(/class 10a/i));

    const file = new File(["content"], "doc.pdf", { type: "application/pdf" });
    await user.upload(document.querySelector("input[type='file']")!, file);

    await user.click(screen.getByRole("button", { name: /^upload$/i }));

    await waitFor(() => {
      expect(
        screen.getByText("Your session has ended. Please sign in again.")
      ).toBeDefined();
    });

    // Upload was NOT sent!
    expect(fakeApi.upload).not.toHaveBeenCalled();
  });

  it("keeps the typed title when the subject and class lists resolve after the user typed", async () => {
    const user = userEvent.setup();
    let resolveClasses: (v: MyClassItem[]) => void = () => {};
    vi.spyOn(academicApi, "getMyClasses").mockReturnValue(
      new Promise<MyClassItem[]>((resolve) => {
        resolveClasses = resolve;
      })
    );

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} api={makeLimitsApi()} />);

    await user.type(screen.getByLabelText(/title/i), "Typed before options");
    resolveClasses(mockClasses);

    expect(await screen.findByRole("option", { name: "Computer Science" })).toBeDefined();
    expect((screen.getByLabelText(/title/i) as HTMLInputElement).value).toBe("Typed before options");
  });

  it("does not pull focus away from a field the user already focused when the opening focus runs late", async () => {
    const user = userEvent.setup();
    // Slow machine: the browser frame that moves focus into the dialog runs late.
    const lateFrames: FrameRequestCallback[] = [];
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((cb) => {
      lateFrames.push(cb);
      return lateFrames.length;
    });

    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} api={makeLimitsApi()} />);

    const titleInput = screen.getByLabelText(/title/i);
    await user.type(titleInput, "Slow machine title");

    // The late frame finally runs; it must not steal focus or lose the typed text.
    lateFrames.forEach((cb) => cb(0));
    expect(document.activeElement).toBe(titleInput);

    await user.type(titleInput, " more");
    expect((titleInput as HTMLInputElement).value).toBe("Slow machine title more");
  });

  it("ties the File label with htmlFor to the FileUpload input", async () => {
    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} />);

    const fileLabel = screen.getByText(/^file/i);
    expect(fileLabel.getAttribute("for")).toBe("material-file-upload");
    const fileInput = document.getElementById("material-file-upload");
    expect(fileInput).toBeDefined();
    expect(fileInput?.getAttribute("type")).toBe("file");
  });

  it("moves focus to the first error after a failed submit", async () => {
    const user = userEvent.setup();
    renderWithProviders(<UploadDialog isOpen={true} onClose={vi.fn()} />);

    await waitFor(() => {
      expect(screen.getByRole("option", { name: "Computer Science" })).toBeDefined();
    });

    const uploadBtn = screen.getByRole("button", { name: /^upload$/i });

    // 1. Submit empty form: Title is the first error -> focus moves to Title input
    await user.click(uploadBtn);
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByLabelText(/title/i));
    });

    // 2. Type title, but no subject: Subject is the first error -> focus moves to Subject select
    await user.type(screen.getByLabelText(/title/i), "Chapter 4 Notes");
    await user.click(uploadBtn);
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByLabelText(/subject/i));
    });
  });
});
