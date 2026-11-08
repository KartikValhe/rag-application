import { apiClient } from "@/api/client";

export interface PermissionItem {
  id: string;
  category: string;
  description: string;
}

export interface RoleItem {
  id: string;
  name: string;
  description?: string | null;
  is_system: boolean;
  permissions: string[];
}

export interface CreateRolePayload {
  name: string;
  description?: string;
  permissions: string[];
}

export interface UpdateRolePayload {
  name?: string;
  description?: string;
  permissions?: string[];
}

export interface UserRoleAssignment {
  userId: string;
  roleId: string;
}

export const rolesApi = {
  listPermissions: async (): Promise<PermissionItem[]> => {
    const res = await apiClient.get<PermissionItem[]>("/roles/permissions");
    return res.data;
  },

  listRoles: async (): Promise<RoleItem[]> => {
    const res = await apiClient.get<RoleItem[]>("/roles");
    return res.data;
  },

  getRole: async (roleId: string): Promise<RoleItem> => {
    const res = await apiClient.get<RoleItem>(`/roles/${roleId}`);
    return res.data;
  },

  createRole: async (payload: CreateRolePayload): Promise<RoleItem> => {
    const res = await apiClient.post<RoleItem>("/roles", payload);
    return res.data;
  },

  updateRole: async (roleId: string, payload: UpdateRolePayload): Promise<RoleItem> => {
    const res = await apiClient.patch<RoleItem>(`/roles/${roleId}`, payload);
    return res.data;
  },

  deleteRole: async (roleId: string): Promise<{ message: string }> => {
    const res = await apiClient.delete<{ message: string }>(`/roles/${roleId}`);
    return res.data;
  },

  getUserRoles: async (userId: string): Promise<RoleItem[]> => {
    const res = await apiClient.get<RoleItem[]>(`/users/${userId}/roles`);
    return res.data;
  },

  assignRoleToUser: async (userId: string, roleId: string): Promise<{ message: string }> => {
    const res = await apiClient.post<{ message: string }>(`/users/${userId}/roles`, { role_id: roleId });
    return res.data;
  },

  removeRoleFromUser: async (userId: string, roleId: string): Promise<{ message: string }> => {
    const res = await apiClient.delete<{ message: string }>(`/users/${userId}/roles/${roleId}`);
    return res.data;
  },
};

