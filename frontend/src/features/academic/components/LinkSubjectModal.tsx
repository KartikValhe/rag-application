import React, { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { Badge } from "@/components/ui/Badge";
import { Alert } from "@/components/ui/Alert";
import { Skeleton } from "@/components/ui/Skeleton";
import { academicApi, ClassItem, SubjectItem } from "../api/academicApi";
import { BookOpen, Plus, Trash2 } from "lucide-react";

export interface LinkSubjectModalProps {
  isOpen: boolean;
  onClose: () => void;
  targetClass: ClassItem | null;
  allSubjects: SubjectItem[];
  onSuccess: (msg: string) => void;
}

export const LinkSubjectModal: React.FC<LinkSubjectModalProps> = ({
  isOpen,
  onClose,
  targetClass,
  allSubjects,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [selectedSubjectId, setSelectedSubjectId] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const { data: linkedSubjects = [], isLoading, refetch } = useQuery({
    queryKey: ["classSubjects", targetClass?.id],
    queryFn: () => (targetClass ? academicApi.listClassSubjects(targetClass.id) : Promise.resolve([])),
    enabled: Boolean(targetClass && isOpen),
  });

  const linkMutation = useMutation({
    mutationFn: () => {
      if (!targetClass || !selectedSubjectId) throw new Error("Missing parameters");
      return academicApi.linkSubjectToClass(targetClass.id, selectedSubjectId);
    },
    onSuccess: (res) => {
      setSelectedSubjectId("");
      setErrorMsg(null);
      refetch();
      queryClient.invalidateQueries({ queryKey: ["classSubjects"] });
      onSuccess(res.message || "Subject linked to class successfully.");
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "Failed to link subject.";
      setErrorMsg(message);
    },
  });

  const unlinkMutation = useMutation({
    mutationFn: (subjectId: string) => {
      if (!targetClass) throw new Error("Class missing");
      return academicApi.unlinkSubjectFromClass(targetClass.id, subjectId);
    },
    onSuccess: (res) => {
      setErrorMsg(null);
      refetch();
      queryClient.invalidateQueries({ queryKey: ["classSubjects"] });
      onSuccess(res.message || "Subject unlinked from class.");
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "Failed to unlink subject.";
      setErrorMsg(message);
    },
  });

  if (!targetClass) return null;

  const linkedIds = new Set(linkedSubjects.map((s) => s.id));
  const availableToLink = allSubjects.filter((s) => !linkedIds.has(s.id));

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={`Curriculum: ${targetClass.name}`}
      description={`Manage subjects taught in ${targetClass.name} (${targetClass.kind})`}
      size="md"
    >
      <div className="space-y-5">
        {errorMsg && (
          <Alert variant="danger" title="Error">
            {errorMsg}
          </Alert>
        )}

        {/* Currently Linked Subjects */}
        <div>
          <h4 className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-2">
            Active Subjects ({linkedSubjects.length})
          </h4>

          {isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
            </div>
          ) : linkedSubjects.length === 0 ? (
            <div className="p-4 text-center rounded-lg border border-dashed border-border text-text-muted text-xs">
              No subjects have been linked to this class yet.
            </div>
          ) : (
            <ul className="divide-y divide-border rounded-lg border border-border overflow-hidden bg-surface">
              {linkedSubjects.map((sub) => (
                <li
                  key={sub.id}
                  className="px-3.5 py-2.5 flex items-center justify-between hover:bg-surface-hover/40 transition-colors"
                >
                  <div className="flex items-center space-x-2.5 min-w-0">
                    <BookOpen className="w-4 h-4 text-primary shrink-0" />
                    <div className="min-w-0">
                      <div className="flex items-center space-x-2">
                        <span className="font-medium text-sm text-text truncate">
                          {sub.name}
                        </span>
                        <Badge variant="default" size="sm">
                          {sub.code}
                        </Badge>
                      </div>
                      {sub.description && (
                        <p className="text-xs text-text-muted truncate">
                          {sub.description}
                        </p>
                      )}
                    </div>
                  </div>

                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-text-muted hover:text-danger hover:bg-danger/10 p-1.5 h-auto"
                    onClick={() => unlinkMutation.mutate(sub.id)}
                    isLoading={unlinkMutation.isPending && unlinkMutation.variables === sub.id}
                    title="Unlink subject"
                    aria-label={`Unlink ${sub.name}`}
                  >
                    <Trash2 className="w-4 h-4" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Link an Additional Subject */}
        <div className="pt-3 border-t border-border space-y-3">
          <h4 className="text-xs font-semibold text-text-muted uppercase tracking-wider">
            Link Additional Subject
          </h4>

          {availableToLink.length === 0 ? (
            <p className="text-xs text-text-muted italic">
              All defined subjects are already linked to this class.
            </p>
          ) : (
            <div className="flex items-center gap-2">
              <div className="flex-1">
                <Select
                  options={[
                    { value: "", label: "Select a subject to link..." },
                    ...availableToLink.map((s) => ({
                      value: s.id,
                      label: `${s.name} (${s.code})`,
                    })),
                  ]}
                  value={selectedSubjectId}
                  onChange={(e) => setSelectedSubjectId(e.target.value)}
                />
              </div>
              <Button
                type="button"
                disabled={!selectedSubjectId}
                isLoading={linkMutation.isPending}
                onClick={() => linkMutation.mutate()}
                leftIcon={<Plus className="w-4 h-4" />}
              >
                Link
              </Button>
            </div>
          )}
        </div>

        <div className="flex justify-end pt-2 border-t border-border">
          <Button variant="outline" onClick={onClose}>
            Done
          </Button>
        </div>
      </div>
    </Modal>
  );
};

