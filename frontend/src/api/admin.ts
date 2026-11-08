import { apiClient } from "./client";
import { ImportResponse, User, UserListResponse } from "./types";

export interface InviteUserPayload {
  email: string;
  role_name?: "student" | "teacher" | "admin";
}

export interface AcceptInvitationPayload {
  token: string;
  full_name: string;
  password: string;
}

export const adminApi = {
  listUsers: async (params?: {
    status_filter?: string;
    skip?: number;
    limit?: number;
  }): Promise<UserListResponse> => {
    const res = await apiClient.get<UserListResponse>("/admin/users", { params });
    return res.data;
  },

  deactivateUser: async (userId: string): Promise<User> => {
    const res = await apiClient.patch<User>(`/admin/users/${userId}/deactivate`);
    return res.data;
  },

  inviteUser: async (
    payload: InviteUserPayload
  ): Promise<{ invitation_id: string; email: string; message: string }> => {
    const res = await apiClient.post("/admin/users/invite", payload);
    return res.data;
  },

  acceptInvitation: async (
    payload: AcceptInvitationPayload
  ): Promise<{ message: string; user_id: string }> => {
    const res = await apiClient.post("/admin/invitations/accept", null, {
      params: payload,
    });
    return res.data;
  },

  importUsers: async (file: File): Promise<ImportResponse> => {
    const formData = new FormData();
    formData.append("file", file);
    const res = await apiClient.post<ImportResponse>("/admin/users/import", formData, {
      headers: {
        "Content-Type": "multipart/form-data",
      },
    });
    return res.data;
  },

  getRegistrationMode: async (): Promise<{ registration_mode: string }> => {
    const res = await apiClient.get<{ registration_mode: string }>(
      "/admin/settings/registration-mode"
    );
    return res.data;
  },

  setRegistrationMode: async (
    mode: "open" | "approval" | "invite_only"
  ): Promise<{ registration_mode: string }> => {
    const res = await apiClient.patch<{ registration_mode: string }>(
      "/admin/settings/registration-mode",
      { registration_mode: mode }
    );
    return res.data;
  },

  listPendingApprovals: async (): Promise<UserListResponse> => {
    const res = await apiClient.get<UserListResponse>("/admin/pending-approvals");
    return res.data;
  },

  approveUser: async (userId: string): Promise<User> => {
    const res = await apiClient.post<User>(
      `/admin/pending-approvals/${userId}/approve`
    );
    return res.data;
  },

  rejectUser: async (userId: string): Promise<User> => {
    const res = await apiClient.post<User>(
      `/admin/pending-approvals/${userId}/reject`
    );
    return res.data;
  },
};

