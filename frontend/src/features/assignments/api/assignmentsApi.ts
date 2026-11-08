import { apiClient } from "@/api/client";

export interface TeacherAssignmentItem {
  id: string;
  user_id: string;
  teacher_name: string;
  teacher_email: string;
  class_id: string;
  class_name: string;
  subject_id: string;
  subject_name: string;
  assigned_at: string;
}

export interface TeacherAssignmentListResponse {
  items: TeacherAssignmentItem[];
  total: number;
}

export interface AssignTeacherPayload {
  classId: string;
  subjectId: string;
  teacherId: string;
}

export const assignmentsApi = {
  listAssignments: async (params?: {
    page?: number;
    page_size?: number;
    class_id?: string;
    teacher_id?: string;
  }): Promise<TeacherAssignmentListResponse> => {
    const page = params?.page || 1;
    const limit = params?.page_size || 50;
    const skip = (page - 1) * limit;

    const res = await apiClient.get<TeacherAssignmentListResponse>("/teacher-assignments", {
      params: {
        skip,
        limit,
        class_id: params?.class_id || undefined,
        teacher_id: params?.teacher_id || undefined,
      },
    });
    return res.data;
  },

  assignTeacher: async (payload: AssignTeacherPayload): Promise<{ message: string }> => {
    const res = await apiClient.post<{ message: string }>(
      `/classes/${payload.classId}/subjects/${payload.subjectId}/teachers`,
      {
        user_id: payload.teacherId,
      }
    );
    return res.data;
  },

  removeAssignment: async (
    classId: string,
    subjectId: string,
    teacherId: string
  ): Promise<{ message: string }> => {
    const res = await apiClient.delete<{ message: string }>(
      `/classes/${classId}/subjects/${subjectId}/teachers/${teacherId}`
    );
    return res.data;
  },
};
