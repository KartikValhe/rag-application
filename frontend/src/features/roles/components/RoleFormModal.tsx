import React, { useState, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Alert } from "@/components/ui/Alert";
import { PermissionMatrix } from "./PermissionMatrix";
import { rolesApi, RoleItem, PermissionItem } from "../api/rolesApi";

export interface RoleFormModalProps {
  isOpen: boolean;
  onClose: () => void;
  roleToEdit?: RoleItem | null;
  permissions: PermissionItem[];
  onSuccess: (msg: string) => void;
}

export const RoleFormModal: React.FC<RoleFormModalProps> = ({
  isOpen,
  onClose,
  roleToEdit,
  permissions,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selectedPermissions, setSelectedPermissions] = useState<string[]>([]);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const isEditing = Boolean(roleToEdit);

  useEffect(() => {
    if (roleToEdit) {
      setName(roleToEdit.name);
      setDescription(roleToEdit.description || "");
      setSelectedPermissions(roleToEdit.permissions || []);
    } else {
      setName("");
      setDescription("");
      setSelectedPermissions([]);
    }
    setErrorMsg(null);
  }, [roleToEdit, isOpen]);

  const mutation = useMutation({
    mutationFn: async () => {
      if (isEditing && roleToEdit) {
        return rolesApi.updateRole(roleToEdit.id, {
          name: name.trim(),
          description: description.trim() || undefined,
          permissions: selectedPermissions,
        });
      }
      return rolesApi.createRole({
        name: name.trim(),
        description: description.trim() || undefined,
        permissions: selectedPermissions,
      });
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["adminRoles"] });
      onSuccess(isEditing ? `Role "${data.name}" updated successfully.` : `Role "${data.name}" created successfully.`);
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
          : "Failed to save role. Please check the inputs.";
      setErrorMsg(message);
    },
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) {
      setErrorMsg("Role name is required.");
      return;
    }
    if (selectedPermissions.length === 0) {
      setErrorMsg("Please select at least one permission for this role.");
      return;
    }
    setErrorMsg(null);
    mutation.mutate();
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={isEditing ? `Edit Custom Role: ${roleToEdit?.name}` : "Create Custom Role"}
      description="Define role identity and pick granular permissions from the catalog."
      size="xl"
    >
      <form onSubmit={handleSubmit} className="space-y-5">
        {errorMsg && (
          <Alert variant="danger" title="Error">
            {errorMsg}
          </Alert>
        )}

        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <Input
            label="Role Identifier / Name"
            placeholder="e.g. Exam Coordinator, HOD"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
            helper="Unique name within this tenant workspace"
          />

          <Input
            label="Description (Optional)"
            placeholder="e.g. Manages exam timetable and grades"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
          />
        </div>

        <div>
          <label className="block text-sm font-medium text-text mb-2">
            Permissions ({selectedPermissions.length} selected)
          </label>
          <div className="max-h-96 overflow-y-auto pr-1">
            <PermissionMatrix
              permissions={permissions}
              selectedPermissions={selectedPermissions}
              onChange={setSelectedPermissions}
            />
          </div>
        </div>

        <div className="flex justify-end gap-3 pt-3 border-t border-border">
          <Button type="button" variant="outline" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" isLoading={mutation.isPending}>
            {isEditing ? "Save Changes" : "Create Role"}
          </Button>
        </div>
      </form>
    </Modal>
  );
};

