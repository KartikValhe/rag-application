// src/api/types.ts
// Strict TypeScript types for all API requests and responses

export interface User {
  id: string;
  email: string;
  full_name: string;
  is_admin: boolean;
  status: "pending" | "active" | "deactivated";
  created_at: string;
}

export interface Tenant {
  id: string;
  slug: string;
  name: string;
  kind: "school" | "college" | "institute" | "class" | "company" | "other";
  registration_mode: "open" | "approval" | "invite_only";
  status: "active" | "suspended" | "archived";
  created_at: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  is_admin: boolean;
}

export interface RegisterResponse {
  user_id: string;
  email: string;
  status: string;
  message: string;
}

export interface MessageResponse {
  message: string;
}

export interface PlatformLoginResponse {
  access_token: string;
  token_type: string;
}

export interface TenantListResponse {
  items: Tenant[];
  total: number;
}

export interface UserListResponse {
  items: User[];
  total: number;
}

export interface ImportResultRow {
  row_number: number;
  email: string;
  full_name: string;
  role_name: string;
  success: boolean;
  error: string | null;
}

export interface ImportResponse {
  total: number;
  succeeded: number;
  failed: number;
  rows: ImportResultRow[];
}
