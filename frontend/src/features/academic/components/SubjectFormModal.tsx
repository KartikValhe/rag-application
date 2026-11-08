import React, { useState, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Alert } from "@/components/ui/Alert";
import { academicApi, SubjectItem } from "../api/academicApi";

export interface SubjectFormModalProps {
  isOpen: boolean;
  onClose: () => void;
  subjectToEdit?: SubjectItem | null;
  onSuccess: (msg: string) => void;
}

export const SubjectFormModal: React.FC<SubjectFormModalProps> = ({
  isOpen,
  onClose,
  subjectToEdit,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [code, setCode] = useState("");
  const [description, setDescription] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const isEditing = Boolean(subjectToEdit);

  useEffect(() => {
    if (subjectToEdit) {
      setName(subjectToEdit.name);
      setCode(subjectToEdit.code || "");
      setDescription(subjectToEdit.description || "");
    } else {
      setName("");
      setCode("");
      setDescription("");
    }
    setErrorMsg(null);
  }, [subjectToEdit, isOpen]);

  const mutation = useMutation({
    mutationFn: () => {
      if (isEditing && subjectToEdit) {
        return academicApi.updateSubject(subjectToEdit.id, {
          name: name.trim(),
          code: code.trim().toUpperCase() || undefined,
          description: description.trim() || undefined,
        });
      }
      return academicApi.createSubject({
        name: name.trim(),
        code: code.trim().toUpperCase() || undefined,
        description: description.trim() || undefined,
      });
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["academicSubjects"] });
      onSuccess(isEditing ? `Subject "${data.name}" updated successfully.` : `Subject "${data.name}" created successfully.`);
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
          : "Failed to save subject. Please ensure name is unique.";
      setErrorMsg(message);
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setErrorMsg("Subject name is required.");
      return;
    }
    setErrorMsg(null);
    mutation.mutate();
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={isEditing ? `Edit Subject: ${subjectToEdit?.name}` : "Create Subject"}
      description="Define an academic curriculum course or subject."
      size="md"
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {errorMsg && (
          <Alert variant="danger" title="Error">
            {errorMsg}
          </Alert>
        )}

        <Input
          label="Subject Name"
          placeholder="e.g. Data Structures & Algorithms"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />

        <Input
          label="Subject Code (Optional)"
          placeholder="e.g. CS-301"
          value={code}
          onChange={(e) => setCode(e.target.value.toUpperCase())}
        />

        <Input
          label="Description (Optional)"
          placeholder="e.g. Core theoretical computer science curriculum"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />

        <div className="flex justify-end gap-3 pt-3 border-t border-border">
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" isLoading={mutation.isPending}>
            {isEditing ? "Save Changes" : "Create Subject"}
          </Button>
        </div>
      </form>
    </Modal>
  );
};
