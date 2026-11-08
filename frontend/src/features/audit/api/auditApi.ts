import { apiClient } from "@/api/client";

export interface AuditLogItem {
  id: string;
  actor_id?: string | null;
  actor_email?: string | null;
  action: string;
  target_type: string;
  target_id?: string | null;
  details?: Record<string, unknown> | null;
  ip_address?: string | null;
  request_id?: string | null;
  created_at: string;
}

export interface AuditLogListResponse {
  items: AuditLogItem[];
  total: number;
}

export interface AuditLogFilterParams {
  page?: number;
  page_size?: number;
  action?: string;
  actor_id?: string;
  target_type?: string;
  from_date?: string;
  to_date?: string;
}

export const auditApi = {
  listLogs: async (params?: AuditLogFilterParams): Promise<AuditLogListResponse> => {
    const page = params?.page || 1;
    const limit = params?.page_size || 15;
    const skip = (page - 1) * limit;

    const res = await apiClient.get<AuditLogListResponse>("/audit-logs", {
      params: {
        skip,
        limit,
        action: params?.action || undefined,
        target_type: params?.target_type || undefined,
        date_from: params?.from_date || undefined,
        date_to: params?.to_date || undefined,
      },
    });
    return res.data;
  },
};
