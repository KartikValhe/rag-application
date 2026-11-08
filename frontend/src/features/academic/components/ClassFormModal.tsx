import React, { useState, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Alert } from "@/components/ui/Alert";
import { academicApi, ClassItem } from "../api/academicApi";

export interface ClassFormModalProps {
  isOpen: boolean;
  onClose: () => void;
  classToEdit?: ClassItem | null;
  onSuccess: (msg: string) => void;
}

export const ClassFormModal: React.FC<ClassFormModalProps> = ({
  isOpen,
  onClose,
  classToEdit,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [kind, setKind] = useState("section");
  const [description, setDescription] = useState("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const isEditing = Boolean(classToEdit);

  useEffect(() => {
    if (classToEdit) {
      setName(classToEdit.name);
      setKind(classToEdit.kind || "section");
      setDescription(classToEdit.description || "");
    } else {
      setName("");
      setKind("section");
      setDescription("");
    }
    setErrorMsg(null);
  }, [classToEdit, isOpen]);

  const mutation = useMutation({
    mutationFn: () => {
      if (isEditing && classToEdit) {
        return academicApi.updateClass(classToEdit.id, {
          name: name.trim(),
          kind: kind.trim(),
          description: description.trim() || undefined,
        });
      }
      return academicApi.createClass({
        name: name.trim(),
        kind: kind.trim(),
        description: description.trim() || undefined,
      });
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["academicClasses"] });
      onSuccess(isEditing ? `Class "${data.name}" updated successfully.` : `Class "${data.name}" created successfully.`);
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
          : "Failed to save class. Please ensure name is unique.";
      setErrorMsg(message);
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setErrorMsg("Class name is required.");
      return;
    }
    setErrorMsg(null);
    mutation.mutate();
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={isEditing ? `Edit Class: ${classToEdit?.name}` : "Create Academic Class"}
      description="Define an academic batch, section, or cohort."
      size="md"
    >
      <form onSubmit={handleSubmit} className="space-y-4">
        {errorMsg && (
          <Alert variant="danger" title="Error">
            {errorMsg}
          </Alert>
        )}

        <Input
          label="Class / Cohort Name"
          placeholder="e.g. Computer Science - Year 3"
          value={name}
          onChange={(e) => setName(e.target.value)}
          required
        />

        <Select
          label="Cohort Type / Category"
          options={[
            { value: "section", label: "Section (e.g. Section A, Section B)" },
            { value: "batch", label: "Batch (e.g. Class of 2026, 2027)" },
            { value: "course", label: "Course Cohort (e.g. Elective Group)" },
            { value: "cohort", label: "General Cohort" },
          ]}
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        />

        <Input
          label="Description (Optional)"
          placeholder="e.g. Academic year 2026-2027 batch"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />

        <div className="flex justify-end gap-3 pt-3 border-t border-border">
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" isLoading={mutation.isPending}>
            {isEditing ? "Save Changes" : "Create Class"}
          </Button>
        </div>
      </form>
    </Modal>
  );
};
