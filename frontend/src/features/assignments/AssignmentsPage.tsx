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
} from "@/components/ui";
import { assignmentsApi, TeacherAssignmentItem } from "./api/assignmentsApi";
import { academicApi } from "@/features/academic/api/academicApi";
import { AssignTeacherModal } from "./components/AssignTeacherModal";
import { Plus, Trash2, School, BookOpen } from "lucide-react";

export const AssignmentsPage: React.FC = () => {
  const queryClient = useQueryClient();

  const [classFilter, setClassFilter] = useState("");
  const [page, setPage] = useState(1);
  const pageSize = 10;

  const [feedback, setFeedback] = useState<{ type: "success" | "danger"; message: string } | null>(null);

  // Modals state
  const [isAssignModalOpen, setIsAssignModalOpen] = useState(false);
  const [assignmentToRemove, setAssignmentToRemove] = useState<TeacherAssignmentItem | null>(null);
  const [isConfirmRemoveOpen, setIsConfirmRemoveOpen] = useState(false);

  // 1. Fetch available classes for filtering & assignment
  const { data: classesData } = useQuery({
    queryKey: ["academicClassesAll"],
    queryFn: () => academicApi.listClasses({ page: 1, page_size: 100 }),
  });

  const classes = classesData?.items || [];

  // 2. Fetch assignments list
  const { data: assignmentsData, isLoading: isAssignmentsLoading } = useQuery({
    queryKey: ["teacherAssignments", page, classFilter],
    queryFn: () =>
      assignmentsApi.listAssignments({
        page,
        page_size: pageSize,
        class_id: classFilter || undefined,
      }),
  });

  // Remove mutation
  const removeMutation = useMutation({
    mutationFn: () => {
      if (!assignmentToRemove) throw new Error("Missing assignment");
      return assignmentsApi.removeAssignment(
        assignmentToRemove.class_id,
        assignmentToRemove.subject_id,
        assignmentToRemove.user_id
      );
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["teacherAssignments"] });
      setFeedback({ type: "success", message: res.message || "Teacher assignment removed." });
      setIsConfirmRemoveOpen(false);
      setAssignmentToRemove(null);
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "Failed to remove teacher assignment.";
      setFeedback({ type: "danger", message });
      setIsConfirmRemoveOpen(false);
    },
  });

  const columns: Column<TeacherAssignmentItem>[] = [
    {
      key: "class_name",
      header: "Class / Cohort",
      render: (a) => (
        <div className="flex items-center space-x-2">
          <div className="p-1.5 bg-primary/10 rounded-md text-primary shrink-0">
            <School className="w-4 h-4" />
          </div>
          <span className="font-semibold text-text">{a.class_name}</span>
        </div>
      ),
    },
    {
      key: "subject_name",
      header: "Curriculum Subject",
      render: (a) => (
        <div className="flex items-center space-x-2">
          <div className="p-1.5 bg-success/10 rounded-md text-success shrink-0">
            <BookOpen className="w-4 h-4" />
          </div>
          <span className="font-semibold text-text">{a.subject_name}</span>
        </div>
      ),
    },
    {
      key: "teacher_name",
      header: "Instructor",
      render: (a) => (
        <div className="flex items-center space-x-2.5">
          <div className="w-7 h-7 rounded-full bg-surface-muted border border-border flex items-center justify-center font-bold text-xs text-text shrink-0">
            {(a.teacher_name || a.teacher_email).slice(0, 2).toUpperCase()}
          </div>
          <div>
            <div className="font-semibold text-text">{a.teacher_name || "Faculty Member"}</div>
            <div className="text-xs text-text-muted">{a.teacher_email}</div>
          </div>
        </div>
      ),
    },
    {
      key: "assigned_at",
      header: "Assigned Date",
      render: (a) => (
        <span className="text-xs text-text-muted">
          {new Date(a.assigned_at).toLocaleDateString(undefined, {
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
      render: (a) => (
        <Button
          size="sm"
          variant="ghost"
          className="text-danger hover:bg-danger/10"
          onClick={() => {
            setAssignmentToRemove(a);
            setIsConfirmRemoveOpen(true);
          }}
          title="Remove assignment"
          aria-label={`Remove assignment for ${a.teacher_name || a.teacher_email}`}
        >
          <Trash2 className="w-4 h-4" />
        </Button>
      ),
    },
  ];

  return (
    <AppShell>
      <PageHeader
        title="Teacher Assignments"
        description="Designate faculty instructors to teach specific subjects within cohorts and academic batches."
        breadcrumbs={[
          { label: "Admin", href: "/admin" },
          { label: "Teacher Assignments" },
        ]}
        actions={
          <Button
            onClick={() => setIsAssignModalOpen(true)}
            leftIcon={<Plus className="w-4 h-4" />}
          >
            Assign Teacher
          </Button>
        }
      />

      <div className="space-y-6">
        {feedback && (
          <Alert variant={feedback.type} title="Notification" onClose={() => setFeedback(null)}>
            {feedback.message}
          </Alert>
        )}

        {/* Filter bar */}
        <div className="flex flex-col sm:flex-row items-center justify-between gap-4 p-4 rounded-xl border border-border bg-surface shadow-xs">
          <div className="w-full sm:w-72">
            <Select
              options={[
                { value: "", label: "All Classes & Cohorts" },
                ...classes.map((c) => ({
                  value: c.id,
                  label: `${c.name} (${c.kind})`,
                })),
              ]}
              value={classFilter}
              onChange={(e) => {
                setClassFilter(e.target.value);
                setPage(1);
              }}
            />
          </div>

          <Badge variant="primary">
            {assignmentsData?.total || 0} Total Active Assignments
          </Badge>
        </div>

        {/* Assignments Table */}
        <Table<TeacherAssignmentItem>
          columns={columns}
          data={assignmentsData?.items || []}
          keyExtractor={(a) => a.id}
          isLoading={isAssignmentsLoading}
          emptyTitle="No Teacher Assignments"
          emptyDescription="No instructors have been assigned to class-subject pairings yet. Click 'Assign Teacher' to create one."
          pagination={{
            page,
            pageSize,
            total: assignmentsData?.total || 0,
            onPageChange: setPage,
          }}
        />
      </div>

      {/* Assign Teacher Modal */}
      <AssignTeacherModal
        isOpen={isAssignModalOpen}
        onClose={() => setIsAssignModalOpen(false)}
        classes={classes}
        onSuccess={(msg) => setFeedback({ type: "success", message: msg })}
      />

      {/* Confirm Remove Assignment */}
      <ConfirmDialog
        isOpen={isConfirmRemoveOpen}
        onClose={() => setIsConfirmRemoveOpen(false)}
        title="Revoke Teacher Assignment"
        message={`Are you sure you want to unassign ${assignmentToRemove?.teacher_name || assignmentToRemove?.teacher_email} from ${assignmentToRemove?.subject_name} (${assignmentToRemove?.class_name})?`}
        confirmText="Revoke Assignment"
        variant="danger"
        isLoading={removeMutation.isPending}
        onConfirm={() => removeMutation.mutate()}
      />
    </AppShell>
  );
};
