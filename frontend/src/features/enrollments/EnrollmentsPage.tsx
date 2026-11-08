import React, { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AppShell, PageHeader } from "@/components/layout";
import {
  Button,
  Badge,
  Table,
  Column,
  ConfirmDialog,
  Alert,
  Select,
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui";
import { enrollmentsApi, StudentRosterItem } from "./api/enrollmentsApi";
import { academicApi } from "@/features/academic/api/academicApi";
import { AddStudentModal } from "./components/AddStudentModal";
import { CsvImportModal } from "./components/CsvImportModal";
import { GraduationCap, UserPlus, Upload, Trash2, Users } from "lucide-react";

export const EnrollmentsPage: React.FC = () => {
  const queryClient = useQueryClient();
  const [selectedClassId, setSelectedClassId] = useState<string>("");
  const [feedback, setFeedback] = useState<{ type: "success" | "danger"; message: string } | null>(null);

  // Modals state
  const [isAddStudentOpen, setIsAddStudentOpen] = useState(false);
  const [isCsvImportOpen, setIsCsvImportOpen] = useState(false);

  const [studentToRemove, setStudentToRemove] = useState<StudentRosterItem | null>(null);
  const [isConfirmRemoveOpen, setIsConfirmRemoveOpen] = useState(false);

  // 1. Fetch available classes
  const { data: classesData, isLoading: isClassesLoading } = useQuery({
    queryKey: ["academicClassesAll"],
    queryFn: () => academicApi.listClasses({ page: 1, page_size: 100 }),
  });

  const classes = classesData?.items || [];
  const currentClass = classes.find((c) => c.id === selectedClassId) || classes[0] || null;

  // Auto-select first class if none selected
  React.useEffect(() => {
    if (!selectedClassId && classes.length > 0 && classes[0]) {
      setSelectedClassId(classes[0].id);
    }
  }, [classes, selectedClassId]);

  // 2. Fetch roster for active class
  const { data: rosterData, isLoading: isRosterLoading } = useQuery({
    queryKey: ["classRoster", currentClass?.id],
    queryFn: () => (currentClass ? enrollmentsApi.getClassRoster(currentClass.id) : null),
    enabled: Boolean(currentClass),
  });

  // Remove student mutation
  const removeMutation = useMutation({
    mutationFn: () => {
      if (!currentClass || !studentToRemove) throw new Error("Missing parameters");
      return enrollmentsApi.removeStudentFromClass(currentClass.id, studentToRemove.user_id);
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["classRoster", currentClass?.id] });
      setFeedback({ type: "success", message: res.message || "Student enrollment removed." });
      setIsConfirmRemoveOpen(false);
      setStudentToRemove(null);
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "Failed to remove student from class.";
      setFeedback({ type: "danger", message });
      setIsConfirmRemoveOpen(false);
    },
  });

  const enrolledStudents = rosterData?.items || [];
  const enrolledUserIds = new Set(enrolledStudents.map((s) => s.user_id));

  const rosterColumns: Column<StudentRosterItem>[] = [
    {
      key: "full_name",
      header: "Student Name",
      render: (s) => (
        <div className="flex items-center space-x-2.5">
          <div className="w-8 h-8 rounded-full bg-primary/10 text-primary flex items-center justify-center font-bold text-xs shrink-0">
            {(s.full_name || s.email).slice(0, 2).toUpperCase()}
          </div>
          <div>
            <div className="font-semibold text-text">{s.full_name || "Enrolled Student"}</div>
            <div className="text-xs text-text-muted">{s.email}</div>
          </div>
        </div>
      ),
    },
    {
      key: "enrolled_at",
      header: "Enrolled On",
      render: (s) => (
        <span className="text-xs text-text-muted">
          {new Date(s.enrolled_at).toLocaleDateString(undefined, {
            year: "numeric",
            month: "short",
            day: "numeric",
          })}
        </span>
      ),
    },
    {
      key: "actions",
      header: "Actions",
      className: "text-right",
      render: (s) => (
        <Button
          size="sm"
          variant="ghost"
          className="text-danger hover:bg-danger/10"
          onClick={() => {
            setStudentToRemove(s);
            setIsConfirmRemoveOpen(true);
          }}
          title="Remove from class"
          aria-label={`Remove ${s.full_name || s.email}`}
        >
          <Trash2 className="w-4 h-4" />
        </Button>
      ),
    },
  ];

  return (
    <AppShell>
      <PageHeader
        title="Student Enrollments"
        description="Manage academic class rosters, individual student enrollments, and bulk CSV ingestion."
        breadcrumbs={[
          { label: "Admin", href: "/admin" },
          { label: "Enrollments" },
        ]}
        actions={
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              disabled={!currentClass}
              onClick={() => setIsCsvImportOpen(true)}
              leftIcon={<Upload className="w-4 h-4" />}
            >
              CSV Import
            </Button>
            <Button
              disabled={!currentClass}
              onClick={() => setIsAddStudentOpen(true)}
              leftIcon={<UserPlus className="w-4 h-4" />}
            >
              Enroll Student
            </Button>
          </div>
        }
      />

      <div className="space-y-6">
        {feedback && (
          <Alert variant={feedback.type} title="Notification" onClose={() => setFeedback(null)}>
            {feedback.message}
          </Alert>
        )}

        {/* Cohort Selector Card */}
        <Card>
          <CardHeader>
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
              <div className="flex items-center space-x-3">
                <div className="p-2 bg-primary/10 rounded-lg text-primary">
                  <GraduationCap className="w-5 h-5" />
                </div>
                <div>
                  <CardTitle className="text-base">Selected Class Roster</CardTitle>
                  <CardDescription>
                    Switch cohorts to view and manage assigned students
                  </CardDescription>
                </div>
              </div>

              <div className="w-full sm:w-72">
                <Select
                  options={
                    classes.length === 0
                      ? [{ value: "", label: "No classes available" }]
                      : classes.map((c) => ({
                          value: c.id,
                          label: `${c.name} (${c.kind})`,
                        }))
                  }
                  value={currentClass?.id || ""}
                  onChange={(e) => setSelectedClassId(e.target.value)}
                  disabled={isClassesLoading || classes.length === 0}
                />
              </div>
            </div>
          </CardHeader>
        </Card>

        {/* Class Roster Table */}
        {currentClass ? (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-text uppercase tracking-wider">
                {currentClass.name} Roster ({enrolledStudents.length} Students)
              </h3>
              <Badge variant="primary" size="sm" className="capitalize">
                {currentClass.kind}
              </Badge>
            </div>

            <Table<StudentRosterItem>
              columns={rosterColumns}
              data={enrolledStudents}
              keyExtractor={(s) => s.user_id}
              isLoading={isRosterLoading}
              emptyTitle="No Students Enrolled"
              emptyDescription={`Class ${currentClass.name} has no enrolled students yet. Use 'Enroll Student' or 'CSV Import' to add students.`}
            />
          </div>
        ) : (
          <div className="p-12 text-center rounded-xl border border-dashed border-border bg-surface text-text-muted">
            <Users className="w-10 h-10 mx-auto text-text-muted/60 mb-2" />
            <h4 className="font-semibold text-text">No Classes Available</h4>
            <p className="text-xs text-text-muted mt-1">
              Please create an academic class in Classes & Subjects before enrolling students.
            </p>
          </div>
        )}
      </div>

      {/* Add Single Student Modal */}
      <AddStudentModal
        isOpen={isAddStudentOpen}
        onClose={() => setIsAddStudentOpen(false)}
        targetClass={currentClass}
        enrolledUserIds={enrolledUserIds}
        onSuccess={(msg) => setFeedback({ type: "success", message: msg })}
      />

      {/* CSV Batch Import Modal */}
      <CsvImportModal
        isOpen={isCsvImportOpen}
        onClose={() => setIsCsvImportOpen(false)}
        targetClass={currentClass}
        onSuccess={(msg) => setFeedback({ type: "success", message: msg })}
      />

      {/* Confirm Remove Student */}
      <ConfirmDialog
        isOpen={isConfirmRemoveOpen}
        onClose={() => setIsConfirmRemoveOpen(false)}
        title="Remove Student from Class"
        message={`Are you sure you want to remove ${studentToRemove?.full_name || studentToRemove?.email} from ${currentClass?.name}? The student will lose access to materials and chats for this class.`}
        confirmText="Remove Student"
        variant="danger"
        isLoading={removeMutation.isPending}
        onConfirm={() => removeMutation.mutate()}
      />
    </AppShell>
  );
};
