import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StudentClassDetailPage } from "../StudentClassDetailPage";
import { DashboardPage } from "@/features/dashboard/DashboardPage";
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
  useTheme: () => ({ theme: "light", setTheme: vi.fn(), resolvedTheme: "light", tenantSlug: "acme" }),
}));
vi.mock("@/features/timetable", () => ({
  NextLectureCard: () => <div>next lecture stub</div>,
}));

function setUser(roles: string[], permissions: string[] = []) {
  vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
    user: { id: "u1", isAdmin: roles.includes("tenant_admin"), roles, permissions },
    isAuthenticated: true,
    isLoading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  } as never);
}

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
}

// Words that would promise a student a list of documents (Phase 5f-10a, plan question Q8).
const DOCUMENT_LIST_PROMISE = /instructor uploads|will appear here|document ingestion|document upload|lecture handouts|document repository|Phase 5/i;

beforeEach(() => {
  vi.restoreAllMocks();
});

describe("5f-10a change 5: student class page promises no document list", () => {
  async function renderClassPage() {
    setUser(["student"]);
    vi.spyOn(academicApi, "getMyClasses").mockResolvedValue([
      { id: "cls-1", name: "Class 10A", kind: "standard", subjects: [{ id: "s1", name: "Physics" }] },
    ] as never);
    render(
      <QueryClientProvider client={client()}>
        <MemoryRouter initialEntries={["/student/classes/cls-1"]}>
          <Routes>
            <Route path="/student/classes/:classId" element={<StudentClassDetailPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
    await screen.findByRole("heading", { name: "Class 10A", level: 1 });
  }

  it("shows no card about course materials, uploads or Phase 5", async () => {
    await renderClassPage();
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(DOCUMENT_LIST_PROMISE);
    expect(screen.queryByText("Course Materials & Lecture Notes")).toBeNull();
  });

  it("keeps one placeholder, about the Phase 6 chat only", async () => {
    await renderClassPage();
    expect(screen.getByText("AI Academic Tutor & Chat")).toBeDefined();
    expect(screen.getAllByText(/Phase 6/).length).toBeGreaterThan(0);
  });
});

describe("5f-10a change 5: student dashboard promises no document upload", () => {
  function renderDashboard(path: string, roles: string[]) {
    setUser(roles);
    render(
      <QueryClientProvider client={client()}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path="*" element={<DashboardPage />} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>
    );
  }

  it("the student card talks about the Phase 6 chat only", async () => {
    renderDashboard("/student/dashboard", ["student"]);
    await screen.findByText("AI Knowledge Base");
    const text = document.body.textContent ?? "";
    expect(text).not.toMatch(/Phase 5/);
    expect(text).not.toMatch(/document upload|vector chunking|ingestion/i);
    expect(screen.getByText("Coming in Phase 6")).toBeDefined();
    expect(screen.getByText(/Phase 6/, { selector: "p" })).toBeDefined();
  });

  it("the teacher view no longer promises Phase 5 (changed in 5f-10b: materials exist, chat is Phase 6)", async () => {
    renderDashboard("/teacher/dashboard", ["teacher"]);
    await screen.findByText("AI Knowledge Base");
    expect(screen.getByText("Materials available, chat in Phase 6")).toBeDefined();
    expect(document.body.textContent ?? "").not.toMatch(/Phase 5/);
  });
});
