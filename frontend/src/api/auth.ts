import { apiClient, getTenantSlug, setAccessToken } from "./client";
import {
  LoginResponse,
  RegisterResponse,
  MessageResponse,
} from "./types";

export interface RegisterPayload {
  email: string;
  full_name: string;
  password: string;
  tenant_slug?: string;
}

export interface LoginPayload {
  email: string;
  password: string;
  tenant_slug?: string;
}

export interface ResetPasswordPayload {
  token: string;
  new_password: string;
}

export const authApi = {
  register: async (payload: RegisterPayload): Promise<RegisterResponse> => {
    const slug = payload.tenant_slug || getTenantSlug();
    const res = await apiClient.post<RegisterResponse>("/auth/register", {
      ...payload,
      tenant_slug: slug,
    });
    return res.data;
  },

  login: async (payload: LoginPayload): Promise<LoginResponse> => {
    const slug = payload.tenant_slug || getTenantSlug();
    const res = await apiClient.post<LoginResponse>("/auth/login", {
      ...payload,
      tenant_slug: slug,
    });
    setAccessToken(res.data.access_token);
    return res.data;
  },

  refresh: async (): Promise<{ access_token: string }> => {
    const res = await apiClient.post<{ access_token: string }>("/auth/refresh");
    setAccessToken(res.data.access_token);
    return res.data;
  },

  logout: async (): Promise<void> => {
    try {
      await apiClient.post("/auth/logout");
    } finally {
      setAccessToken(null);
    }
  },

  verifyEmail: async (token: string): Promise<MessageResponse> => {
    const res = await apiClient.post<MessageResponse>("/auth/verify-email", { token });
    return res.data;
  },

  forgotPassword: async (email: string, tenantSlug?: string): Promise<MessageResponse> => {
    const slug = tenantSlug || getTenantSlug();
    const res = await apiClient.post<MessageResponse>("/auth/forgot-password", {
      email,
      tenant_slug: slug,
    });
    return res.data;
  },

  resetPassword: async (payload: ResetPasswordPayload): Promise<MessageResponse> => {
    const res = await apiClient.post<MessageResponse>("/auth/reset-password", payload);
    return res.data;
  },
};

