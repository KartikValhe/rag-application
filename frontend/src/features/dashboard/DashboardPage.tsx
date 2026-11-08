import React from "react";
import { useLocation } from "react-router-dom";
import { useAuth } from "@/features/auth/AuthContext";
import { useTheme } from "@/theme/ThemeProvider";
import { usePermission } from "@/hooks/usePermission";
import { Badge } from "@/components/ui/Badge";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/Card";
import { AppShell } from "@/components/layout/AppShell";
import { PageHeader } from "@/components/layout/PageHeader";
import { GraduationCap, ShieldCheck, BookOpen, Layers } from "lucide-react";

export const DashboardPage: React.FC = () => {
  const location = useLocation();
  const { user } = useAuth();
  const { tenantSlug } = useTheme();
  const { isAdmin, isTeacher, isStudent } = usePermission();

  const isTeacherView = location.pathname.includes("/teacher");
  const isStudentView = location.pathname.includes("/student");

  const portalTitle = isTeacherView
    ? "Teaching Workspace"
    : isStudentView
    ? "Student Workspace"
    : "Academic Workspace";

  const roleLabel = isTeacherView
    ? "Teacher / Faculty"
    : isStudentView
    ? "Enrolled Student"
    : isAdmin
    ? "Tenant Administrator"
    : isTeacher
    ? "Teacher / Faculty"
    : isStudent
    ? "Enrolled Student"
    : "Member";

  return (
    <AppShell tenantName={tenantSlug ? `${tenantSlug.toUpperCase()} Academic` : "Academic Portal"}>
      <PageHeader
        title={portalTitle}
        description="Phase 2 role-based access control and academic structure enabled."
        breadcrumbs={[
          { label: "Portal", href: location.pathname },
          { label: portalTitle },
        ]}
        actions={
          <Badge
            variant={
              isTeacherView
                ? "primary"
                : isStudentView
                ? "success"
                : isAdmin
                ? "danger"
                : "default"
            }
            size="md"
          >
            {roleLabel}
          </Badge>
        }
      />

      <div className="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8">
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">
                Tenant Realm
              </span>
              <div className="p-2 bg-primary/10 rounded-lg text-primary">
                <Layers className="w-5 h-5" />
              </div>
            </div>
            <CardTitle className="mt-2 text-xl capitalize">
              {tenantSlug || "Default"}
            </CardTitle>
            <CardDescription>
              Isolated PostgreSQL schema & RLS boundary
            </CardDescription>
          </CardHeader>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">
                Active Identity
              </span>
              <div className="p-2 bg-success/10 rounded-lg text-success">
                <GraduationCap className="w-5 h-5" />
              </div>
            </div>
            <CardTitle className="mt-2 text-xl truncate">
              {user?.email || (user?.id ? user.id.slice(0, 12) : "Authenticated")}
            </CardTitle>
            <CardDescription>
              Session authenticated via HttpOnly refresh cookie
            </CardDescription>
          </CardHeader>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold text-text-muted uppercase tracking-wider">
                Security & RBAC
              </span>
              <div className="p-2 bg-primary/10 rounded-lg text-primary">
                <ShieldCheck className="w-5 h-5" />
              </div>
            </div>
            <CardTitle className="mt-2 text-xl">
              Default-Deny Enforced
            </CardTitle>
            <CardDescription>
              Server-side scoped authorization active
            </CardDescription>
          </CardHeader>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Welcome to Your Workspace</CardTitle>
          <CardDescription>
            Navigate using the sidebar to explore classes, subjects, enrollments, and academic structure.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-4 text-sm text-text">
            <div className="p-4 rounded-lg bg-surface-muted border border-border space-y-2">
              <div className="flex items-center gap-2">
                <span className="font-semibold text-text-muted">User ID:</span>
                <code className="text-xs bg-surface px-2 py-1 rounded border border-border font-mono">
                  {user?.id}
                </code>
              </div>
              <div className="flex items-center gap-2">
                <span className="font-semibold text-text-muted">Assigned Roles:</span>
                <span className="text-xs font-medium text-text">
                  {user?.roles && user.roles.length > 0
                    ? user.roles.join(", ")
                    : isAdmin
                    ? "tenant_admin (superuser)"
                    : "member"}
                </span>
              </div>
            </div>

            <div className="flex items-start gap-3 p-4 rounded-lg bg-primary/5 border border-primary/20">
              <BookOpen className="w-5 h-5 text-primary shrink-0 mt-0.5" />
              <div>
                <h4 className="font-semibold text-text">Design System & Layout Shell Ready</h4>
                <p className="text-xs text-text-muted mt-1 leading-relaxed">
                  The design system tokens are dynamically styled through CSS variables in light and dark modes with WCAG AA compliance. Use the sun/moon button in the top bar to toggle themes.
                </p>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>
    </AppShell>
  );
};
