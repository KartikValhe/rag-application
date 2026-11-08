import React, { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { Alert } from "@/components/ui/Alert";
import { enrollmentsApi } from "../api/enrollmentsApi";
import { adminApi } from "@/api/admin";
import { ClassItem } from "@/features/academic/api/academicApi";
import { UserPlus } from "lucide-react";

export interface AddStudentModalProps {
  isOpen: boolean;
  onClose: () => void;
  targetClass: ClassItem | null;
  enrolledUserIds: Set<string>;
  onSuccess: (msg: string) => void;
}

export const AddStudentModal: React.FC<AddStudentModalProps> = ({
  isOpen,
  onClose,
  targetClass,
  enrolledUserIds,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [selectedUserId, setSelectedUserId] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const { data: usersData, isLoading: isUsersLoading } = useQuery({
    queryKey: ["adminUsers"],
    queryFn: () => adminApi.listUsers({ limit: 100 }),
    enabled: isOpen,
  });

  const mutation = useMutation({
    mutationFn: () => {
      if (!targetClass || !selectedUserId) throw new Error("Missing selection");
      return enrollmentsApi.addStudentToClass(targetClass.id, selectedUserId);
    },
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["classRoster", targetClass?.id] });
      setSelectedUserId("");
      setErrorMsg(null);
      onSuccess(res.message || "Student enrolled successfully.");
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
          : "Failed to enroll student.";
      setErrorMsg(message);
    },
  });

  if (!targetClass) return null;

  const availableStudents = (usersData?.items || []).filter(
    (u) => !enrolledUserIds.has(u.id) && u.status === "active"
  );

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={`Enroll Student: ${targetClass.name}`}
      description={`Add an active student to ${targetClass.name} (${targetClass.kind})`}
      size="md"
    >
      <div className="space-y-4">
        {errorMsg && (
          <Alert variant="danger" title="Enrollment Error">
            {errorMsg}
          </Alert>
        )}

        {isUsersLoading ? (
          <p className="text-sm text-text-muted">Loading available users...</p>
        ) : availableStudents.length === 0 ? (
          <p className="text-sm text-text-muted italic">
            No unenrolled active students available to add to this class.
          </p>
        ) : (
          <div>
            <label className="block text-sm font-medium text-text mb-1.5">
              Select Student to Enroll
            </label>
            <Select
              options={[
                { value: "", label: "Choose a student from tenant directory..." },
                ...availableStudents.map((u) => ({
                  value: u.id,
                  label: `${u.full_name || "Member"} (${u.email})`,
                })),
              ]}
              value={selectedUserId}
              onChange={(e) => setSelectedUserId(e.target.value)}
            />
          </div>
        )}

        <div className="flex justify-end gap-3 pt-3 border-t border-border">
          <Button variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!selectedUserId || mutation.isPending}
            isLoading={mutation.isPending}
            onClick={() => mutation.mutate()}
            leftIcon={<UserPlus className="w-4 h-4" />}
          >
            Enroll Student
          </Button>
        </div>
      </div>
    </Modal>
  );
};

