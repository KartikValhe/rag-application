import React, { useState, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { Alert } from "@/components/ui/Alert";
import { assignmentsApi } from "../api/assignmentsApi";
import { academicApi, ClassItem } from "@/features/academic/api/academicApi";
import { adminApi } from "@/api/admin";
import { UserCheck } from "lucide-react";

export interface AssignTeacherModalProps {
  isOpen: boolean;
  onClose: () => void;
  classes: ClassItem[];
  onSuccess: (msg: string) => void;
}

export const AssignTeacherModal: React.FC<AssignTeacherModalProps> = ({
  isOpen,
  onClose,
  classes,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [selectedClassId, setSelectedClassId] = useState("");
  const [selectedSubjectId, setSelectedSubjectId] = useState("");
  const [selectedTeacherId, setSelectedTeacherId] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Auto-select initial class if available
  useEffect(() => {
    if (classes.length > 0 && !selectedClassId && classes[0]) {
      setSelectedClassId(classes[0].id);
    }
  }, [classes, selectedClassId]);

  // Fetch linked subjects when selectedClassId changes
  const { data: linkedSubjects = [], isLoading: isSubjectsLoading } = useQuery({
    queryKey: ["classSubjects", selectedClassId],
    queryFn: () => (selectedClassId ? academicApi.listClassSubjects(selectedClassId) : Promise.resolve([])),
    enabled: Boolean(selectedClassId && isOpen),
  });

  // Reset selected subject when class changes
  useEffect(() => {
    setSelectedSubjectId("");
  }, [selectedClassId]);

  // Fetch users (potential teachers)
  const { data: usersData, isLoading: isUsersLoading } = useQuery({
    queryKey: ["adminUsers"],
    queryFn: () => adminApi.listUsers({ limit: 100 }),
    enabled: isOpen,
  });

  const mutation = useMutation({
    mutationFn: () => {
      if (!selectedClassId || !selectedSubjectId || !selectedTeacherId) {
        throw new Error("Please select Class, Subject, and Teacher.");
      }
      return assignmentsApi.assignTeacher({
        classId: selectedClassId,
        subjectId: selectedSubjectId,
        teacherId: selectedTeacherId,
      });
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["teacherAssignments"] });
      setSelectedTeacherId("");
      setSelectedSubjectId("");
      setErrorMsg(null);
      onSuccess(res.message || "Teacher assigned successfully.");
      onClose();
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "Failed to assign teacher.";
      setErrorMsg(message);
    },
  });

  const users = usersData?.items || [];

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title="Assign Faculty / Teacher"
      description="Designate an instructor for a specific class-subject pair."
      size="md"
    >
      <div className="space-y-4">
        {errorMsg && (
          <Alert variant="danger" title="Assignment Error">
            {errorMsg}
          </Alert>
        )}

        {/* 1. Class Selection */}
        <div>
          <label className="block text-sm font-medium text-text mb-1">
            1. Target Class / Cohort
          </label>
          <Select
            options={[
              { value: "", label: "Select a class..." },
              ...classes.map((c) => ({
                value: c.id,
                label: `${c.name} (${c.kind})`,
              })),
            ]}
            value={selectedClassId}
            onChange={(e) => setSelectedClassId(e.target.value)}
          />
        </div>

        {/* 2. Subject Selection */}
        <div>
          <label className="block text-sm font-medium text-text mb-1">
            2. Curriculum Subject
          </label>
          {isSubjectsLoading ? (
            <p className="text-xs text-text-muted">Loading subjects for selected class...</p>
          ) : linkedSubjects.length === 0 ? (
            <div className="p-3 bg-warning/10 border border-warning/20 rounded-lg text-xs text-warning">
              No subjects have been linked to this class yet. Please link a subject in Classes & Subjects first.
            </div>
          ) : (
            <Select
              options={[
                { value: "", label: "Select a linked subject..." },
                ...linkedSubjects.map((s) => ({
                  value: s.id,
                  label: `${s.name}${s.code ? ` (${s.code})` : ""}`,
                })),
              ]}
              value={selectedSubjectId}
              onChange={(e) => setSelectedSubjectId(e.target.value)}
            />
          )}
        </div>

        {/* 3. Teacher Selection */}
        <div>
          <label className="block text-sm font-medium text-text mb-1">
            3. Instructor / Teacher
          </label>
          {isUsersLoading ? (
            <p className="text-xs text-text-muted">Loading instructors...</p>
          ) : (
            <Select
              options={[
                { value: "", label: "Select an instructor..." },
                ...users.map((u) => ({
                  value: u.id,
                  label: `${u.full_name || "Faculty Member"} (${u.email})`,
                })),
              ]}
              value={selectedTeacherId}
              onChange={(e) => setSelectedTeacherId(e.target.value)}
            />
          )}
        </div>

        <div className="flex justify-end gap-3 pt-3 border-t border-border">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={
              !selectedClassId ||
              !selectedSubjectId ||
              !selectedTeacherId ||
              mutation.isPending
            }
            isLoading={mutation.isPending}
            onClick={() => mutation.mutate()}
            leftIcon={<UserCheck className="w-4 h-4" />}
          >
            Assign Teacher
          </Button>
        </div>
      </div>
    </Modal>
  );
};
