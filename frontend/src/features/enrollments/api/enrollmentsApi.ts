import { apiClient } from "@/api/client";

export interface StudentRosterItem {
  id: string;
  user_id: string;
  full_name?: string | null;
  email: string;
  enrolled_at: string;
}

export interface RosterResponse {
  items: StudentRosterItem[];
  total: number;
}

export interface RowErrorItem {
  row: number;
  email: string;
  error: string;
}

export interface EnrollmentImportResponse {
  total_rows: number;
  succeeded: number;
  failed: number;
  errors: RowErrorItem[];
}

export const enrollmentsApi = {
  getClassRoster: async (classId: string, params?: { page?: number; page_size?: number }): Promise<RosterResponse> => {
    const page = params?.page || 1;
    const limit = params?.page_size || 50;
    const skip = (page - 1) * limit;

    const res = await apiClient.get<RosterResponse>(`/classes/${classId}/students`, {
      params: { skip, limit },
    });
    return res.data;
  },

  addStudentToClass: async (classId: string, userId: string): Promise<{ message: string }> => {
    const res = await apiClient.post<{ message: string }>(`/classes/${classId}/students`, {
      user_id: userId,
    });
    return res.data;
  },

  removeStudentFromClass: async (classId: string, userId: string): Promise<{ message: string }> => {
    const res = await apiClient.delete<{ message: string }>(`/classes/${classId}/students/${userId}`);
    return res.data;
  },

  importEnrollmentsCsv: async (classId: string, file: File): Promise<EnrollmentImportResponse> => {
    const formData = new FormData();
    formData.append("file", file);
    const res = await apiClient.post<EnrollmentImportResponse>(
      `/classes/${classId}/students/import`,
      formData,
      {
        headers: {
          "Content-Type": "multipart/form-data",
        },
      }
    );
    return res.data;
  },
};
