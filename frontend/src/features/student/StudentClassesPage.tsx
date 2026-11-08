import React from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { AppShell, PageHeader } from "@/components/layout";
import {
  Badge,
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
  Button,
  Skeleton,
  EmptyState,
} from "@/components/ui";
import { academicApi } from "@/features/academic/api/academicApi";
import { School, BookOpen, ArrowRight } from "lucide-react";

export const StudentClassesPage: React.FC = () => {
  const { data: classes = [], isLoading } = useQuery({
    queryKey: ["myEnrolledClasses"],
    queryFn: () => academicApi.getMyClasses(),
  });

  return (
    <AppShell>
      <PageHeader
        title="My Enrolled Classes"
        description="View your active student cohorts, syllabus curriculum, and assigned learning subjects."
        breadcrumbs={[
          { label: "Student Portal", href: "/student/dashboard" },
          { label: "My Classes" },
        ]}
      />

      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={`skel-${i}`} className="p-6 rounded-xl border border-border bg-surface space-y-4">
              <Skeleton className="h-6 w-3/4" />
              <Skeleton className="h-4 w-1/2" />
              <div className="pt-4 space-y-2 border-t border-border">
                <Skeleton className="h-4 w-full" />
                <Skeleton className="h-4 w-5/6" />
              </div>
            </div>
          ))}
        </div>
      ) : classes.length === 0 ? (
        <div className="py-12 bg-surface rounded-xl border border-border">
          <EmptyState
            icon={BookOpen}
            title="No Enrolled Classes"
            description="You are not enrolled in any academic batches or sections at this time. Please contact your institution administrator."
          />
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
          {classes.map((cls) => (
            <Card key={cls.id} className="flex flex-col hover:border-primary/40 transition-colors">
              <CardHeader>
                <div className="flex items-center justify-between mb-2">
                  <div className="p-2 bg-primary/10 rounded-lg text-primary">
                    <School className="w-5 h-5" />
                  </div>
                  <Badge variant="primary" size="sm" className="capitalize">
                    {cls.kind}
                  </Badge>
                </div>
                <CardTitle className="text-lg">{cls.name}</CardTitle>
                <CardDescription>
                  {cls.subjects.length} {cls.subjects.length === 1 ? "Subject" : "Subjects"} offered
                </CardDescription>
              </CardHeader>

              <CardContent className="flex-1">
                <div className="space-y-2 text-xs">
                  <span className="font-semibold text-text-muted uppercase tracking-wider block mb-1">
                    Enrolled Curriculum
                  </span>
                  {cls.subjects.length === 0 ? (
                    <span className="text-text-muted italic">No subjects configured in this cohort.</span>
                  ) : (
                    <ul className="space-y-1.5">
                      {cls.subjects.map((sub) => (
                        <li key={sub.id} className="flex items-center justify-between text-text">
                          <span className="truncate">{sub.name}</span>
                          {sub.code && (
                            <code className="font-mono text-[10px] bg-surface-muted px-1.5 py-0.5 rounded border border-border shrink-0 ml-2">
                              {sub.code}
                            </code>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </CardContent>

              <CardFooter className="pt-4 border-t border-border/80">
                <Link to={`/student/classes/${cls.id}`} className="w-full">
                  <Button
                    variant="outline"
                    className="w-full justify-between"
                    rightIcon={<ArrowRight className="w-4 h-4" />}
                  >
                    Enter Class
                  </Button>
                </Link>
              </CardFooter>
            </Card>
          ))}
        </div>
      )}
    </AppShell>
  );
};
