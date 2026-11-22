import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { DashboardPage } from "../DashboardPage";
import * as AuthContextModule from "@/features/auth/AuthContext";

vi.mock("@/theme/ThemeProvider", () => ({
  useTheme: () => ({ tenantSlug: "acme", theme: "light", setTheme: vi.fn(), resolvedTheme: "light" }),
}));
vi.mock("@/components/layout/AppShell", () => ({
  AppShell: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
}));
vi.mock("@/features/timetable", () => ({ NextLectureCard: () => null }));

function setUser(opts: { permissions: string[]; roles: string[]; isAdmin?: boolean }) {
  vi.spyOn(AuthContextModule, "useAuth").mockReturnValue({
    user: { id: "u-1", isAdmin: opts.isAdmin ?? false, roles: opts.roles, permissions: opts.permissions },
    isAuthenticated: true,
    isLoading: false,
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  } as never);
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <DashboardPage />
    </MemoryRouter>
  );
}

describe("Phase 5f-10b: dashboard materials card", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("teacher: no 'will be unlocked in Phase 5' text; a link to /teacher/materials", () => {
    setUser({ permissions: ["document:view"], roles: ["teacher"] });
    const { container } = renderAt("/teacher/dashboard");
    expect(container.textContent ?? "").not.toMatch(/unlocked in Phase 5/i);
    expect(container.textContent ?? "").not.toMatch(/Coming in Phase 5/i);
    const link = screen.getByRole("link", { name: /course materials/i });
    expect(link.getAttribute("href")).toBe("/teacher/materials");
  });

  it("admin: no Phase 5 promise; a link to /admin/materials", () => {
    setUser({ permissions: [], roles: ["tenant_admin"], isAdmin: true });
    const { container } = renderAt("/dashboard");
    expect(container.textContent ?? "").not.toMatch(/unlocked in Phase 5/i);
    expect(container.textContent ?? "").not.toMatch(/Coming in Phase 5/i);
    const link = screen.getByRole("link", { name: /course materials/i });
    expect(link.getAttribute("href")).toBe("/admin/materials");
  });

  it("the card text says materials can be uploaded and checked, and that chat comes in Phase 6", () => {
    setUser({ permissions: ["document:view"], roles: ["teacher"] });
    const { container } = renderAt("/teacher/dashboard");
    expect(container.textContent ?? "").toMatch(/upload course materials/i);
    expect(container.textContent ?? "").toMatch(/Phase 6/);
  });

  it("a teacher without document:view gets no materials link", () => {
    setUser({ permissions: [], roles: ["teacher"] });
    renderAt("/teacher/dashboard");
    expect(screen.queryByRole("link", { name: /course materials/i })).toBeNull();
  });

  it("student: unchanged Phase 6 text and no materials link", () => {
    setUser({ permissions: [], roles: ["student"] });
    const { container } = renderAt("/student/dashboard");
    expect(screen.queryByRole("link", { name: /course materials/i })).toBeNull();
    expect(container.textContent ?? "").toMatch(/Coming in Phase 6/);
    expect(container.textContent ?? "").not.toMatch(/Phase 5/);
  });
  it("a student who somehow holds document:view still gets no materials link on the student dashboard", () => {
    setUser({ permissions: ["document:view"], roles: ["student"] });
    renderAt("/student/dashboard");
    expect(screen.queryByRole("link", { name: /course materials/i })).toBeNull();
  });
});
