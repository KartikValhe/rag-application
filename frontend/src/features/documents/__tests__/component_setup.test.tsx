import { describe, it, expect, vi } from "vitest";
import { render, screen, renderHook } from "@testing-library/react";
import { usePermission } from "@/hooks/usePermission";
import { getDocumentActions } from "../utils/documentPolicy.ts";

vi.mock("@/features/auth/AuthContext", () => ({
  useAuth: () => ({
    user: {
      id: "admin-1",
      isAdmin: true,
      roles: ["tenant_admin"],
      permissions: ["*"],
    },
    isAuthenticated: true,
    isLoading: false,
  }),
}));

const DummyComponent = () => <div>Course Materials Component Setup</div>;

describe("Component Test Setup", () => {
  it("proves Vitest, jsdom, and Testing Library render and query a component", () => {
    render(<DummyComponent />);
    expect(screen.getByText("Course Materials Component Setup")).toBeDefined();
  });

  it("verifies admin through the real usePermission has full access in documentPolicy", () => {
    const { result } = renderHook(() => usePermission());
    expect(result.current.isAdmin).toBe(true);
    expect(result.current.can("document:view")).toBe(true);
    expect(result.current.can("settings:manage")).toBe(true);

    const readyVersion = {
      id: "v1",
      document_id: "d1",
      version_no: 1,
      status: "ready",
      file_kind: "pdf",
      original_filename: "doc.pdf",
      size_bytes: 100,
      created_at: "2026-11-20T10:00:00Z",
      warnings: [],
    };

    const actions = getDocumentActions({
      can: result.current.can,
      document: {
        id: "d1",
        title: "Test",
        subject: { id: "s1", name: "Math" },
        classes: [],
        created_by: "u1",
        created_at: "2026-11-20T10:00:00Z",
        updated_at: "2026-11-20T10:00:00Z",
        latest_version: readyVersion,
        can_manage: true,
      },
      version: readyVersion,
    });

    const viewAction = actions.find((a) => a.id === "view");
    const uploadAction = actions.find((a) => a.id === "upload");
    const publishAction = actions.find((a) => a.id === "publish");
    const usageAction = actions.find((a) => a.id === "usage");

    expect(viewAction?.visible).toBe(true);
    expect(uploadAction?.visible).toBe(true);
    expect(publishAction?.visible).toBe(true);
    expect(publishAction?.enabled).toBe(true);
    expect(usageAction?.visible).toBe(true);
  });
});
