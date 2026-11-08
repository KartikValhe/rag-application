import { apiClient, setPlatformToken } from "./client";
import {
  PlatformLoginResponse,
  Tenant,
  TenantListResponse,
} from "./types";

export interface TenantCreatePayload {
  slug: string;
  name: string;
  kind: "school" | "college" | "institute" | "class" | "company" | "other";
  registration_mode: "open" | "approval" | "invite_only";
}

export interface TenantUpdatePayload {
  status?: "active" | "suspended" | "archived";
  registration_mode?: "open" | "approval" | "invite_only";
  name?: string;
}

export interface CreateAdminPayload {
  email: string;
  full_name: string;
}

export const platformApi = {
  login: async (email: string, password: string): Promise<PlatformLoginResponse> => {
    const res = await apiClient.post<PlatformLoginResponse>("/platform/auth/login", {
      email,
      password,
    });
    setPlatformToken(res.data.access_token);
    return res.data;
  },

  logout: () => {
    setPlatformToken(null);
  },

  listTenants: async (params?: {
    skip?: number;
    limit?: number;
    status_filter?: string;
  }): Promise<TenantListResponse> => {
    const res = await apiClient.get<TenantListResponse>("/platform/tenants", {
      params,
    });
    return res.data;
  },

  createTenant: async (payload: TenantCreatePayload): Promise<Tenant> => {
    const res = await apiClient.post<Tenant>("/platform/tenants", payload);
    return res.data;
  },

  updateTenant: async (
    tenantId: string,
    payload: TenantUpdatePayload
  ): Promise<Tenant> => {
    const res = await apiClient.patch<Tenant>(
      `/platform/tenants/${tenantId}`,
      payload
    );
    return res.data;
  },

  createTenantAdmin: async (
    tenantId: string,
    payload: CreateAdminPayload
  ): Promise<{ user_id: string; email: string; message: string }> => {
    const res = await apiClient.post(
      `/platform/tenants/${tenantId}/admins`,
      payload
    );
    return res.data;
  },
};

