import React from "react";
import { useLocation, Link } from "react-router-dom";
import { useAuth } from "@/features/auth/AuthContext";
import { useTheme } from "@/theme/ThemeProvider";
import { usePermission } from "@/hooks/usePermission";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from "@/components/ui/Card";
import { AppShell } from "@/components/layout/AppShell";
import { PageHeader } from "@/components/layout/PageHeader";
import { NextLectureCard } from "@/features/timetable";
import {
  GraduationCap,
  ShieldCheck,
  BookOpen,
  Layers,
  ArrowRight,
  School,
  Sparkles,
} from "lucide-react";

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

      {/* Next Lecture Real-Time Schedule Card */}
      <div className="mb-8">
        <NextLectureCard isAdmin={isAdmin && !isTeacherView && !isStudentView} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-8">
        {/* Quick Launch Card */}
        <Card className="hover:border-primary/40 transition-colors">
          <CardHeader>
            <div className="flex items-center space-x-3 mb-1">
              <div className="p-2 rounded-lg bg-primary/10 text-primary">
                <School className="w-5 h-5" />
              </div>
              <CardTitle className="text-lg">
                {isTeacherView
                  ? "Assigned Classes & Subjects"
                  : isStudentView
                  ? "Enrolled Classes & Curriculum"
                  : "Academic Classes"}
              </CardTitle>
            </div>
            <CardDescription>
              {isTeacherView
                ? "Browse your teaching cohorts, assigned courses, and view student rosters."
                : isStudentView
                ? "Access enrolled academic batches, syllabus subjects, and curriculum guides."
                : "Manage tenant classes, cohorts, sections, and subjects."}
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-2">
            <Link
              to={
                isTeacherView
                  ? "/teacher/classes"
                  : isStudentView
                  ? "/student/classes"
                  : isAdmin
                  ? "/admin/academic"
                  : "/dashboard"
              }
            >
              <Button variant="primary" size="sm" className="w-full justify-between">
                <span>
                  {isTeacherView
                    ? "Go to My Classes"
                    : isStudentView
                    ? "View My Enrolled Classes"
                    : "Manage Classes"}
                </span>
                <ArrowRight className="w-4 h-4 ml-1" />
              </Button>
            </Link>
          </CardContent>
        </Card>

        {/* Phase 5/6 Placeholder Preview Card */}
        <Card className="border-dashed border-border bg-surface/50">
          <CardHeader>
            <div className="flex items-center justify-between mb-1">
              <div className="flex items-center space-x-3">
                <div className="p-2 rounded-lg bg-accent/10 text-accent">
                  <Sparkles className="w-5 h-5" />
                </div>
                <CardTitle className="text-lg">AI Knowledge Base</CardTitle>
              </div>
              <Badge variant="primary" size="sm">
                Coming in Phase 5 & 6
              </Badge>
            </div>
            <CardDescription>
              {isTeacherView
                ? "Course document ingestion, PDF syllabus parsing, and vector knowledge indexing."
                : isStudentView
                ? "Curriculum-grounded AI tutor chat with citations, sources, and instant answers."
                : "Tenant-wide vector embeddings, document pipeline, and RAG configuration."}
            </CardDescription>
          </CardHeader>
          <CardContent className="pt-2 text-xs text-text-muted">
            <p>
              Document upload, vector chunking, and contextual multi-turn Q&amp;A will be unlocked in Phase 5 and Phase 6.
            </p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Workspace Credentials & Active Roles</CardTitle>
          <CardDescription>
            Identity context bound to your active tenant session.
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
                <h4 className="font-semibold text-text">Phase 2 RBAC & Scoped Authorization Active</h4>
                <p className="text-xs text-text-muted mt-1 leading-relaxed">
                  Every API request enforces server-side default-deny permissions and PostgreSQL row-level security.
                  Class resources outside your assigned cohort return a generic 404 response to eliminate ID probing.
                </p>
              </div>
            </div>
          </div>
        </CardContent>
      </Card>
    </AppShell>
  );
};
