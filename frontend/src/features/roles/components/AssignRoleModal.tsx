import React, { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Modal } from "@/components/ui/Modal";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { Badge } from "@/components/ui/Badge";
import { Alert } from "@/components/ui/Alert";
import { Skeleton } from "@/components/ui/Skeleton";
import { rolesApi, RoleItem } from "../api/rolesApi";
import { User } from "@/api/types";
import { Shield, Plus, Trash2 } from "lucide-react";

export interface AssignRoleModalProps {
  isOpen: boolean;
  onClose: () => void;
  user: User | null;
  allRoles: RoleItem[];
  onSuccess: (msg: string) => void;
}

export const AssignRoleModal: React.FC<AssignRoleModalProps> = ({
  isOpen,
  onClose,
  user,
  allRoles,
  onSuccess,
}) => {
  const queryClient = useQueryClient();
  const [selectedRoleId, setSelectedRoleId] = useState<string>("");
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  // Fetch roles for this specific user
  const { data: userRoles, isLoading, refetch } = useQuery({
    queryKey: ["userRoles", user?.id],
    queryFn: () => (user ? rolesApi.getUserRoles(user.id) : Promise.resolve([])),
    enabled: Boolean(user && isOpen),
  });

  const assignMutation = useMutation({
    mutationFn: () => {
      if (!user || !selectedRoleId) throw new Error("Select a role");
      return rolesApi.assignRoleToUser(user.id, selectedRoleId);
    },
    onSuccess: (res) => {
      setSelectedRoleId("");
      setErrorMsg(null);
      refetch();
      queryClient.invalidateQueries({ queryKey: ["adminUsers"] });
      onSuccess(res.message || "Role assigned successfully.");
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "Failed to assign role.";
      setErrorMsg(message);
    },
  });

  const removeMutation = useMutation({
    mutationFn: (roleId: string) => {
      if (!user) throw new Error("User missing");
      return rolesApi.removeRoleFromUser(user.id, roleId);
    },
    onSuccess: (res) => {
      setErrorMsg(null);
      refetch();
      queryClient.invalidateQueries({ queryKey: ["adminUsers"] });
      onSuccess(res.message || "Role removed successfully.");
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
          ? detail
          : "Failed to remove role.";
      setErrorMsg(message);
    },
  });

  if (!user) return null;

  const assignedIds = new Set(userRoles?.map((r) => r.id) || []);
  const availableRoles = allRoles.filter((r) => !assignedIds.has(r.id));

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      title={`Manage Roles: ${user.full_name || user.email}`}
      description="View, grant, or revoke workspace roles for this account."
      size="md"
    >
      <div className="space-y-5">
        {errorMsg && (
          <Alert variant="danger" title="Error">
            {errorMsg}
          </Alert>
        )}

        {/* User Summary Card */}
        <div className="p-3 bg-surface-muted/40 rounded-lg border border-border text-xs space-y-1">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-text">{user.full_name || "Member"}</span>
            <span className="text-text-muted">{user.email}</span>
          </div>
          <div className="text-text-muted">
            User ID: <code className="font-mono text-[11px] text-text">{user.id}</code>
          </div>
        </div>

        {/* Currently Assigned Roles */}
        <div>
          <h4 className="text-xs font-semibold text-text-muted uppercase tracking-wider mb-2">
            Active Roles
          </h4>

          {isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-9 w-full" />
              <Skeleton className="h-9 w-full" />
            </div>
          ) : !userRoles || userRoles.length === 0 ? (
            <div className="p-4 text-center rounded-lg border border-dashed border-border text-text-muted text-xs">
              No specific roles assigned yet.
            </div>
          ) : (
            <ul className="divide-y divide-border rounded-lg border border-border overflow-hidden bg-surface">
              {userRoles.map((role) => (
                <li
                  key={role.id}
                  className="px-3.5 py-2.5 flex items-center justify-between hover:bg-surface-hover/40 transition-colors"
                >
                  <div className="flex items-center space-x-2.5 min-w-0">
                    <Shield className="w-4 h-4 text-primary shrink-0" />
                    <div className="min-w-0">
                      <div className="flex items-center space-x-2">
                        <span className="font-medium text-sm text-text truncate">
                          {role.name}
                        </span>
                        {role.is_system && (
                          <Badge variant="primary" size="sm">
                            System
                          </Badge>
                        )}
                      </div>
                      {role.description && (
                        <p className="text-xs text-text-muted truncate">
                          {role.description}
                        </p>
                      )}
                    </div>
                  </div>

                  <Button
                    size="sm"
                    variant="ghost"
                    className="text-text-muted hover:text-danger hover:bg-danger/10 p-1.5 h-auto"
                    onClick={() => removeMutation.mutate(role.id)}
                    isLoading={removeMutation.isPending && removeMutation.variables === role.id}
                    title="Remove role"
                    aria-label={`Remove role ${role.name}`}
                  >
                    <Trash2 className="w-4 h-4" />
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {/* Assign Additional Role */}
        <div className="pt-3 border-t border-border space-y-3">
          <h4 className="text-xs font-semibold text-text-muted uppercase tracking-wider">
            Assign Additional Role
          </h4>

          {availableRoles.length === 0 ? (
            <p className="text-xs text-text-muted italic">
              All defined roles have already been assigned to this user.
            </p>
          ) : (
            <div className="flex items-center gap-2">
              <div className="flex-1">
                <Select
                  options={[
                    { value: "", label: "Select a role to assign..." },
                    ...availableRoles.map((r) => ({
                      value: r.id,
                      label: `${r.name}${r.is_system ? " (System)" : ""}`,
                    })),
                  ]}
                  value={selectedRoleId}
                  onChange={(e) => setSelectedRoleId(e.target.value)}
                />
              </div>
              <Button
                type="button"
                disabled={!selectedRoleId}
                isLoading={assignMutation.isPending}
                onClick={() => assignMutation.mutate()}
                leftIcon={<Plus className="w-4 h-4" />}
              >
                Assign
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

