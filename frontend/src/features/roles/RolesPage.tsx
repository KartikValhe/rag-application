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
  Tabs,
  TabList,
  TabTrigger,
  TabContent,
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "@/components/ui";
import { rolesApi, RoleItem } from "./api/rolesApi";
import { adminApi } from "@/api/admin";
import { User } from "@/api/types";
import { RoleFormModal } from "./components/RoleFormModal";
import { AssignRoleModal } from "./components/AssignRoleModal";
import { Modal } from "@/components/ui/Modal";
import { PermissionMatrix } from "./components/PermissionMatrix";
import { Shield, Plus, Edit2, Trash2, UserCheck, Eye, Lock } from "lucide-react";

export const RolesPage: React.FC = () => {
  const queryClient = useQueryClient();

  const [activeTab, setActiveTab] = useState<string>("roles");
  const [feedback, setFeedback] = useState<{ type: "success" | "danger"; message: string } | null>(null);

  // Modal states
  const [isFormOpen, setIsFormOpen] = useState(false);
  const [roleToEdit, setRoleToEdit] = useState<RoleItem | null>(null);

  const [isViewPermsOpen, setIsViewPermsOpen] = useState(false);
  const [roleToView, setRoleToView] = useState<RoleItem | null>(null);

  const [roleToDelete, setRoleToDelete] = useState<RoleItem | null>(null);
  const [isConfirmDeleteOpen, setIsConfirmDeleteOpen] = useState(false);

  const [selectedUserForRoles, setSelectedUserForRoles] = useState<User | null>(null);
  const [isAssignModalOpen, setIsAssignModalOpen] = useState(false);

  // Queries
  const { data: permissions = [] } = useQuery({
    queryKey: ["permissionCatalog"],
    queryFn: () => rolesApi.listPermissions(),
  });

  const { data: roles = [], isLoading: isRolesLoading } = useQuery({
    queryKey: ["adminRoles"],
    queryFn: () => rolesApi.listRoles(),
  });

  const { data: usersData, isLoading: isUsersLoading } = useQuery({
    queryKey: ["adminUsers"],
    queryFn: () => adminApi.listUsers({ limit: 100 }),
  });

  // Delete mutation
  const deleteMutation = useMutation({
    mutationFn: (roleId: string) => rolesApi.deleteRole(roleId),
    onSuccess: (res) => {
      queryClient.invalidateQueries({ queryKey: ["adminRoles"] });
      setFeedback({ type: "success", message: res.message || "Custom role deleted successfully." });
      setIsConfirmDeleteOpen(false);
      setRoleToDelete(null);
    },
    onError: (err: unknown) => {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
            ? detail
            : "Failed to delete role.";
      setFeedback({ type: "danger", message });
      setIsConfirmDeleteOpen(false);
    },
  });

  const systemRoles = roles.filter((r) => r.is_system);
  const customRoles = roles.filter((r) => !r.is_system);

  const customRoleColumns: Column<RoleItem>[] = [
    {
      key: "name",
      header: "Role Identifier",
      render: (r) => (
            <div className="flex items-center space-x-2 min-w-0">
              <Shield className="w-4 h-4 text-primary shrink-0" aria-hidden="true" />
              <div className="min-w-0">
                <div className="font-semibold text-text break-words">{r.name}</div>
                {r.description && <div className="text-xs text-text-muted break-words">{r.description}</div>}
              </div>
            </div>
            ),
    },
            {
              key: "permissions",
            header: "Granted Permissions",
      render: (r) => (
            <div className="flex items-center space-x-2">
              <Badge variant="secondary" size="sm">
                {r.permissions.length} Permissions
              </Badge>
              <Button
                size="sm"
                variant="ghost"
                className="p-1 h-auto text-xs text-text-muted hover:text-text min-h-[44px] sm:min-h-[28px]"
                onClick={() => {
                  setRoleToView(r);
                  setIsViewPermsOpen(true);
                }}
              >
                <Eye className="w-3.5 h-3.5 mr-1" />
                Inspect
              </Button>
            </div>
            ),
    },
            {
              key: "actions",
            header: "Actions",
            className: "text-right",
      render: (r) => (
            <div className="flex items-center justify-end space-x-1.5">
              <Button
                size="sm"
                variant="ghost"
                className="min-w-[44px] min-h-[44px] sm:min-w-[32px] sm:min-h-[32px] p-2 flex items-center justify-center"
                onClick={() => {
                  setRoleToEdit(r);
                  setIsFormOpen(true);
                }}
                title="Edit Role"
                aria-label={`Edit ${r.name}`}
              >
                <Edit2 className="w-3.5 h-3.5" />
              </Button>
              <Button
                size="sm"
                variant="ghost"
                className="text-danger hover:bg-danger/10 min-w-[44px] min-h-[44px] sm:min-w-[32px] sm:min-h-[32px] p-2 flex items-center justify-center"
                onClick={() => {
                  setRoleToDelete(r);
                  setIsConfirmDeleteOpen(true);
                }}
                title="Delete Role"
                aria-label={`Delete ${r.name}`}
              >
                <Trash2 className="w-3.5 h-3.5" />
              </Button>
            </div>
            ),
    },
            ];

            const userColumns: Column<User>[] = [
              {
                key: "full_name",
              header: "Member Details",
      render: (u) => (
                <div className="min-w-0">
                  <div className="font-semibold text-text">{u.full_name || "Member"}</div>
                  <div className="text-xs text-text-muted break-all" title={u.email}>{u.email}</div>
                </div>
                ),
    },
                {
                  key: "status",
                header: "Status",
      render: (u) => (
                <Badge variant={u.status === "active" ? "success" : "warning"} size="sm">
                  {u.status}
                </Badge>
                ),
    },
                {
                  key: "roles",
                header: "Primary Classification",
      render: (u) => (
                <Badge variant={u.is_admin ? "danger" : "default"} size="sm">
                  {u.is_admin ? "tenant_admin" : "member"}
                </Badge>
                ),
    },
                {
                  key: "actions",
                header: "Manage Access",
                className: "text-right",
      render: (u) => (
                <Button
                  size="sm"
                  variant="outline"
                  className="min-h-[44px] sm:min-h-[32px]"
                  leftIcon={<UserCheck className="w-3.5 h-3.5" />}
                  onClick={() => {
                    setSelectedUserForRoles(u);
                    setIsAssignModalOpen(true);
                  }}
                >
                  Assign Roles
                </Button>
                ),
    },
                ];

                return (
                <AppShell>
                  <PageHeader
                    title="Roles & Permissions"
                    description="Configure role-based access control policies, system archetypes, and custom permissions."
                    breadcrumbs={[
                      { label: "Admin", href: "/admin" },
                      { label: "Roles & Permissions" },
                    ]}
                    actions={
                      <Button
                        onClick={() => {
                          setRoleToEdit(null);
                          setIsFormOpen(true);
                        }}
                        leftIcon={<Plus className="w-4 h-4" />}
                        className="w-full sm:w-auto"
                      >
                        Create Custom Role
                      </Button>
                    }
                  />

                  <div className="space-y-6">
                    {feedback && (
                      <Alert variant={feedback.type} title="Notification" onClose={() => setFeedback(null)}>
                        {feedback.message}
                      </Alert>
                    )}

                    <Tabs value={activeTab} onValueChange={setActiveTab}>
                      <TabList>
                        <TabTrigger value="roles">Role Catalog ({roles.length})</TabTrigger>
                        <TabTrigger value="users">User Role Matrix ({usersData?.total || 0})</TabTrigger>
                      </TabList>

                      {/* TAB 1: ROLES DEFINITIONS */}
                      <TabContent value="roles" className="space-y-6 pt-4">
                        {/* System Archetype Roles */}
                        <div>
                            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mb-3">
                              <div>
                                <h3 className="text-base font-semibold text-text">System Archetype Roles</h3>
                                <p className="text-xs text-text-muted">
                                  Pre-seeded immutable security roles enforced by tenancy boundary.
                                </p>
                              </div>
                              <Badge variant="primary" className="self-start sm:self-auto">Read-Only</Badge>
                            </div>

                            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                              {systemRoles.map((role) => (
                                <Card key={role.id}>
                                  <CardHeader>
                                    <div className="flex items-center justify-between">
                                      <div className="p-2 bg-primary/10 rounded-lg text-primary">
                                        <Lock className="w-4 h-4" />
                                      </div>
                                      <Badge variant="primary" size="sm">
                                        System
                                      </Badge>
                                    </div>
                                    <CardTitle className="mt-2 text-base capitalize">{role.name}</CardTitle>
                                    <CardDescription>{role.description || "System standard role"}</CardDescription>
                                  </CardHeader>
                                  <CardContent>
                                    <div className="flex items-center justify-between pt-2 border-t border-border text-xs">
                                      <span className="text-text-muted">{role.permissions.length} Permissions</span>
                                      <Button
                                        size="sm"
                                        variant="ghost"
                                        className="min-h-[44px] sm:min-h-[32px]"
                                        onClick={() => {
                                          setRoleToView(role);
                                          setIsViewPermsOpen(true);
                                        }}
                                      >
                                        <Eye className="w-3.5 h-3.5 mr-1" />
                                        View Matrix
                                      </Button>
                                    </div>
                                  </CardContent>
                                </Card>
                              ))}
                            </div>
                          </div>

                          {/* Custom Roles */}
                          <div className="pt-4 border-t border-border">
                            <div className="flex items-center justify-between mb-3">
                              <div>
                                <h3 className="text-base font-semibold text-text">Custom Tenant Roles</h3>
                                <p className="text-xs text-text-muted">
                                  Custom roles composed of selected permission strings.
                                </p>
                              </div>
                            </div>

                            <Table<RoleItem>
                              columns={customRoleColumns}
                              data={customRoles}
                              keyExtractor={(r) => r.id}
                              isLoading={isRolesLoading}
                              emptyTitle="No Custom Roles"
                              emptyDescription="No custom roles have been created yet. Click 'Create Custom Role' to define one."
                            />
                          </div>
                      </TabContent>

                      {/* TAB 2: USER ROLE ASSIGNMENTS */}
                      <TabContent value="users" className="pt-4">
                        <Table<User>
                          columns={userColumns}
                          data={usersData?.items || []}
                          keyExtractor={(u) => u.id}
                          isLoading={isUsersLoading}
                          emptyTitle="No Members Found"
                          emptyDescription="No users are currently registered in this tenant workspace."
                        />
                      </TabContent>
                    </Tabs>
                  </div>

                  {/* Role Form Modal (Create / Edit) */}
                  <RoleFormModal
                    isOpen={isFormOpen}
                    onClose={() => {
                      setIsFormOpen(false);
                      setRoleToEdit(null);
                    }}
                    roleToEdit={roleToEdit}
                    permissions={permissions}
                    onSuccess={(msg) => setFeedback({ type: "success", message: msg })}
                  />

                  {/* Role Permissions Viewer Modal (Inspect Matrix) */}
                  <Modal
                    isOpen={isViewPermsOpen}
                    onClose={() => {
                      setIsViewPermsOpen(false);
                      setRoleToView(null);
                    }}
                    title={`Permissions: ${roleToView?.name || "Role"}`}
                    description="Assigned platform capabilities and operational boundaries."
                    size="lg"
                  >
                        {roleToView && (
          <div className="space-y-4">
            <div className="flex items-center space-x-2 text-xs text-text-muted pb-2 border-b border-border">
              <span className="font-semibold text-text">Role Type:</span>
              <Badge variant={roleToView.is_system ? "primary" : "secondary"}>
                {roleToView.is_system ? "System Archetype" : "Custom Tenant Role"}
              </Badge>
            </div>
            <PermissionMatrix
              permissions={permissions}
              selectedPermissions={roleToView.permissions || []}
              readOnly={true}
            />
          </div>
        )}
                  </Modal>

                  {/* Assign User Roles Modal */}
                  <AssignRoleModal
                    isOpen={isAssignModalOpen}
                    onClose={() => {
                      setIsAssignModalOpen(false);
                      setSelectedUserForRoles(null);
                    }}
                    user={selectedUserForRoles}
                    allRoles={roles}
                    onSuccess={(msg) => setFeedback({ type: "success", message: msg })}
                  />

                  {/* Confirm Delete Role Dialog */}
                  <ConfirmDialog
                    isOpen={isConfirmDeleteOpen}
                    onClose={() => {
                      setIsConfirmDeleteOpen(false);
                      setRoleToDelete(null);
                    }}
                    onConfirm={() => {
                      if (roleToDelete) {
                        deleteMutation.mutate(roleToDelete.id);
                      }
                    }}
                    title="Delete Custom Role"
                    message={`Are you sure you want to permanently delete the role "${roleToDelete?.name}"? Users currently mapped to this role will lose its permissions.`}
                    confirmText="Delete Role"
                    variant="danger"
                    isLoading={deleteMutation.isPending}
                  />
                </AppShell>
                );
};
