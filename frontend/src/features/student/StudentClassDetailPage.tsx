import React from "react";
import { useParams, Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { AppShell, PageHeader, NotFoundState } from "@/components/layout";
import {
  Badge,
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  Skeleton,
} from "@/components/ui";
import { academicApi } from "@/features/academic/api/academicApi";
import { BookOpen, MessageSquare, ArrowLeft, Sparkles, FolderArchive } from "lucide-react";

export const StudentClassDetailPage: React.FC = () => {
  const { classId } = useParams<{ classId: string }>();

  const { data: classes = [], isLoading } = useQuery({
    queryKey: ["myEnrolledClasses"],
    queryFn: () => academicApi.getMyClasses(),
  });

  if (isLoading) {
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
          description="The class you are looking for does not exist or you are not currently enrolled in it."
          homePath="/student/classes"
          homeLabel="Return to My Classes"
        />
      </AppShell>
    );
  }

  return (
    <AppShell>
      <PageHeader
        title={currentClass.name}
        description={`Student learning workspace for cohort ${currentClass.name} (${currentClass.kind}).`}
        breadcrumbs={[
          { label: "Student Portal", href: "/student/dashboard" },
          { label: "My Classes", href: "/student/classes" },
          { label: currentClass.name },
        ]}
        actions={
          <div className="flex items-center space-x-2">
            <Badge variant="primary" size="md" className="capitalize">
              {currentClass.kind}
            </Badge>
            <Link to="/student/classes">
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
        {/* Enrolled Subjects Section */}
        <div>
          <div className="flex items-center space-x-2 mb-3">
            <BookOpen className="w-5 h-5 text-primary" />
            <h3 className="text-base font-semibold text-text">Enrolled Curriculum Subjects</h3>
          </div>

          {currentClass.subjects.length === 0 ? (
            <div className="p-6 rounded-xl border border-dashed border-border bg-surface text-center text-xs text-text-muted">
              No subjects have been linked to this class yet.
            </div>
          ) : (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
              {currentClass.subjects.map((sub) => (
                <Card key={sub.id} className="bg-surface hover:border-primary/30 transition-colors">
                  <CardHeader>
                    <div className="flex items-center justify-between mb-1.5">
                      <div className="p-1.5 bg-primary/10 rounded-md text-primary">
                        <BookOpen className="w-4 h-4" />
                      </div>
                      {sub.code && (
                        <code className="font-mono text-xs font-bold text-text bg-surface-muted px-2 py-0.5 rounded border border-border">
                          {sub.code}
                        </code>
                      )}
                    </div>
                    <CardTitle className="text-sm font-semibold">{sub.name}</CardTitle>
                    <CardDescription className="text-xs">Active curriculum course</CardDescription>
                  </CardHeader>
                </Card>
              ))}
            </div>
          )}
        </div>

        {/* Phase 5 / Phase 6 Future Placeholders */}
        <div className="pt-6 border-t border-border space-y-6">
          <div className="flex items-center space-x-2">
            <Sparkles className="w-5 h-5 text-warning" />
            <h3 className="text-base font-semibold text-text">Upcoming Academic Capabilities</h3>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
            {/* Phase 5 Document Placeholder */}
            <Card className="border-border/80 bg-surface-muted/30 relative overflow-hidden">
              <CardHeader>
                <div className="flex items-center justify-between mb-2">
                  <div className="p-2.5 bg-surface rounded-lg border border-border text-text-muted">
                    <FolderArchive className="w-5 h-5" />
                  </div>
                  <Badge variant="warning" size="sm">
                    Phase 5 (Coming Next)
                  </Badge>
                </div>
                <CardTitle className="text-base">Course Materials & Lecture Notes</CardTitle>
                <CardDescription>
                  Document repository and textbook syllabus
                </CardDescription>
              </CardHeader>
              <CardContent>
                <p className="text-xs text-text-muted leading-relaxed">
                  Instructor uploads (PDFs, PPTs, syllabus notes, and lecture handouts) for this class will appear here once document ingestion is configured in Phase 5.
                </p>
              </CardContent>
            </Card>

            {/* Phase 6 AI Chat Placeholder */}
            <Card className="border-border/80 bg-surface-muted/30 relative overflow-hidden">
              <CardHeader>
                <div className="flex items-center justify-between mb-2">
                  <div className="p-2.5 bg-surface rounded-lg border border-border text-text-muted">
                    <MessageSquare className="w-5 h-5" />
                  </div>
                  <Badge variant="info" size="sm">
                    Phase 6 (Coming Soon)
                  </Badge>
                </div>
                <CardTitle className="text-base">AI Academic Tutor & Chat</CardTitle>
                <CardDescription>
                  Interactive Retrieval-Augmented Assistant
                </CardDescription>
              </CardHeader>
              <CardContent>
                <p className="text-xs text-text-muted leading-relaxed">
                  Ask questions, summarize chapters, and query lecture concepts directly against the verified course materials of this class. Unlocks in Phase 6.
                </p>
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    </AppShell>
  );
};
