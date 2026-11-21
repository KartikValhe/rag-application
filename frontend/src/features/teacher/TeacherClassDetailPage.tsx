import React from "react";
import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { AppShell, PageHeader, NotFoundState } from "@/components/layout";
import {
  Badge,
  Card,
  CardHeader,
  CardTitle,
  CardContent,
  Skeleton,
  Table,
  EmptyState,
} from "@/components/ui";
import { academicApi } from "@/features/academic/api/academicApi";
import { enrollmentsApi } from "@/features/enrollments/api/enrollmentsApi";
import {
  BookOpen,
  FolderArchive,
  ArrowLeft,
  Users,
  Calendar,
  Lock,
} from "lucide-react";

export const TeacherClassDetailPage: React.FC = () => {
  const { classId } = useParams<{ classId: string }>();

  // Fetch teaching classes scoped to teacher
  const { data: classes = [], isLoading: isLoadingClasses } = useQuery({
    queryKey: ["myTeacherClasses"],
    queryFn: () => academicApi.getMyClasses(),
  });

  // Query roster (requires enrollment permission; if forbidden, gracefully catches error)
  const {
    data: rosterData,
    isLoading: isLoadingRoster,
    isError: isRosterError,
  } = useQuery({
    queryKey: ["classRoster", classId],
    queryFn: () => enrollmentsApi.getClassRoster(classId || ""),
    enabled: Boolean(classId && classes.some((c) => c.id === classId)),
    retry: false,
  });

  if (isLoadingClasses) {
    return (
      <AppShell>
        <div className="space-y-6">
          <Skeleton className="h-12 w-1/3" />
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            <Skeleton className="h-48 w-full" />
            <Skeleton className="h-48 w-full" />
          </div>
        </div>
      </AppShell>
    );
  }

  const currentClass = classes.find((c) => c.id === classId);

  // Security Rule: A class id the user doesn't belong to shows the friendly "not found" page (404),
  // NEVER "forbidden" (403), so class ids cannot be probed.
  if (!currentClass) {
    return (
      <AppShell>
        <NotFoundState
          title="Class Not Found (404)"
          description="The class you are looking for does not exist or is not currently assigned to you."
          homePath="/teacher/classes"
          homeLabel="Return to My Classes"
        />
      </AppShell>
    );
  }

  const students = rosterData?.items || [];

  return (
    <AppShell>
      <PageHeader
        title={currentClass.name}
        description={`Faculty workspace for cohort ${currentClass.name} (${currentClass.kind}).`}
        breadcrumbs={[
          { label: "Teacher Portal", href: "/teacher/dashboard" },
          { label: "My Classes", href: "/teacher/classes" },
          { label: currentClass.name },
        ]}
        actions={
          <div className="flex items-center space-x-2">
            <Badge variant="primary" size="md" className="capitalize">
              {currentClass.kind}
            </Badge>
            <Link to="/teacher/classes">
              <button
                type="button"
                className="btn btn-outline text-xs inline-flex items-center gap-1.5"
              >
                <ArrowLeft className="w-3.5 h-3.5" />
                <span>All Classes</span>
              </button>
            </Link>
          </div>
        }
      />

      <div className="space-y-8">
        {/* Assigned Subjects Section */}
        <div>
          <div className="flex items-center space-x-2 mb-3">
            <BookOpen className="w-5 h-5 text-primary" />
            <h3 className="text-base font-semibold text-text">Assigned Teaching Subjects</h3>
          </div>

          {currentClass.subjects.length === 0 ? (
            <div className="p-6 rounded-xl border border-dashed border-border bg-surface text-center text-xs text-text-muted">
              No specific subjects have been linked to your assignment in this class.
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {currentClass.subjects.map((sub) => (
                <Card key={sub.id} className="bg-surface hover:border-primary/30 transition-colors">
                  <CardHeader>
                    <div className="flex items-center justify-between">
                      <CardTitle className="text-sm font-semibold">{sub.name}</CardTitle>
                      {sub.code && (
                        <code className="font-mono text-[10px] bg-surface-muted px-1.5 py-0.5 rounded border border-border">
                          {sub.code}
                        </code>
                      )}
                    </div>
                  </CardHeader>
                </Card>
              ))}
            </div>
          )}
        </div>

        {/* Student Roster Section */}
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-2">
              <Users className="w-5 h-5 text-primary" />
              <h3 className="text-base font-semibold text-text">Class Roster</h3>
              {!isLoadingRoster && !isRosterError && (
                <Badge variant="default" size="sm">
                  {students.length} {students.length === 1 ? "Student" : "Students"}
                </Badge>
              )}
            </div>
          </div>

          {isLoadingRoster ? (
            <div className="p-6 rounded-xl border border-border bg-surface space-y-3">
              <Skeleton className="h-6 w-1/4" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : isRosterError ? (
            <div className="p-6 rounded-xl border border-border bg-surface flex items-start gap-3">
              <div className="p-2 rounded-lg bg-surface-muted text-text-muted shrink-0 mt-0.5">
                <Lock className="w-4 h-4" />
              </div>
              <div className="space-y-1">
                <h4 className="text-xs font-semibold text-text">Roster View Restricted</h4>
                <p className="text-xs text-text-muted leading-relaxed">
                  Your teaching role is assigned to curriculum instruction. Detailed enrollment modification
                  and full contact roster access is maintained by administrative staff.
                </p>
              </div>
            </div>
          ) : students.length === 0 ? (
            <div className="py-8 bg-surface rounded-xl border border-border">
              <EmptyState
                icon={Users}
                title="No Students Enrolled"
                description="There are currently no students registered in this class section."
              />
            </div>
          ) : (
            <Card>
              <Table
                columns={[
                  {
                    key: "name",
                    header: "Student",
                    render: (st) => (
                      <div className="flex items-center space-x-2.5">
                        <div className="w-7 h-7 rounded-full bg-primary/10 text-primary font-semibold flex items-center justify-center text-xs">
                          {(st.full_name || st.email || "S")[0].toUpperCase()}
                        </div>
                        <div>
                          <div className="font-medium text-xs text-text">
                            {st.full_name || "Enrolled Student"}
                          </div>
                          <div className="text-[11px] text-text-muted">{st.email}</div>
                        </div>
                      </div>
                    ),
                  },
                  {
                    key: "enrolled_at",
                    header: "Enrolled Date",
                    render: (st) => (
                      <div className="flex items-center gap-1.5 text-xs text-text-muted">
                        <Calendar className="w-3.5 h-3.5" />
                        <span>{new Date(st.enrolled_at).toLocaleDateString()}</span>
                      </div>
                    ),
                  },
                ]}
                data={students}
                keyExtractor={(st) => st.id}
              />
            </Card>
          )}
        </div>

        {/* Course Materials Link Section */}
        <div className="pt-2">
          <div className="flex items-center justify-between mb-3">
            <div className="flex items-center space-x-2">
              <FolderArchive className="w-5 h-5 text-primary" />
              <h3 className="text-base font-semibold text-text">Course Materials</h3>
            </div>
            <Link
              to={`/teacher/materials?class=${classId}`}
              className="text-xs font-medium text-primary hover:underline"
            >
              View materials for this class &rarr;
            </Link>
          </div>

          <Card className="border-border bg-surface">
            <CardContent className="pt-6">
              <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-4">
                <div className="space-y-1">
                  <h4 className="text-sm font-semibold text-text">
                    Course Materials for this Class
                  </h4>
                  <p className="text-xs text-text-muted leading-relaxed max-w-2xl">
                    Browse published textbooks, lecture slides, syllabus guides, and drafts assigned to this class.
                  </p>
                </div>
                <Link
                  to={`/teacher/materials?class=${classId}`}
                  className="shrink-0 inline-flex items-center justify-center rounded-lg bg-primary px-4 py-2 text-xs font-semibold text-white shadow-sm hover:bg-primary-hover min-h-[44px] transition-colors"
                >
                  View Course Materials
                </Link>
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </AppShell>
  );
};
