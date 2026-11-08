import { apiClient } from "@/api/client";

export interface ClassItem {
  id: string;
  name: string;
  kind: string;
  description?: string | null;
  created_at: string;
}

export interface ClassListResponse {
  items: ClassItem[];
  total: number;
}

export interface SubjectItem {
  id: string;
  name: string;
  code?: string | null;
  description?: string | null;
  created_at: string;
}

export interface SubjectListResponse {
  items: SubjectItem[];
  total: number;
}

export interface MySubjectItem {
  id: string;
  name: string;
  code?: string | null;
}

export interface MyClassItem {
  id: string;
  name: string;
  kind: string;
  subjects: MySubjectItem[];
}

export interface CreateClassPayload {
  name: string;
  kind?: string;
  description?: string;
}

export interface UpdateClassPayload {
  name?: string;
  kind?: string;
  description?: string;
}

export interface CreateSubjectPayload {
  name: string;
  code?: string;
  description?: string;
}

export interface UpdateSubjectPayload {
  name?: string;
  code?: string;
  description?: string;
}

export const academicApi = {
  // Classes
  listClasses: async (params?: { page?: number; page_size?: number; search?: string }): Promise<ClassListResponse> => {
    const page = params?.page || 1;
    const limit = params?.page_size || 50;
    const skip = (page - 1) * limit;

    const res = await apiClient.get<ClassListResponse>("/classes", {
      params: { skip, limit },
    });
    return res.data;
  },

  getClass: async (classId: string): Promise<ClassItem> => {
    const res = await apiClient.get<ClassItem>(`/classes/${classId}`);
    return res.data;
  },

  createClass: async (payload: CreateClassPayload): Promise<ClassItem> => {
    const res = await apiClient.post<ClassItem>("/classes", {
      name: payload.name.trim(),
      kind: payload.kind?.trim() || "section",
      description: payload.description?.trim() || undefined,
    });
    return res.data;
  },

  updateClass: async (classId: string, payload: UpdateClassPayload): Promise<ClassItem> => {
    const res = await apiClient.patch<ClassItem>(`/classes/${classId}`, {
      name: payload.name?.trim() || undefined,
      kind: payload.kind?.trim() || undefined,
      description: payload.description?.trim() || undefined,
    });
    return res.data;
  },

  deleteClass: async (classId: string): Promise<{ message: string }> => {
    const res = await apiClient.delete<{ message: string }>(`/classes/${classId}`);
    return res.data;
  },

  // Subjects
  listSubjects: async (params?: { page?: number; page_size?: number; search?: string }): Promise<SubjectListResponse> => {
    const page = params?.page || 1;
    const limit = params?.page_size || 50;
    const skip = (page - 1) * limit;

    const res = await apiClient.get<SubjectListResponse>("/subjects", {
      params: { skip, limit },
    });
    return res.data;
  },

  createSubject: async (payload: CreateSubjectPayload): Promise<SubjectItem> => {
    const res = await apiClient.post<SubjectItem>("/subjects", {
      name: payload.name.trim(),
      code: payload.code?.trim() || undefined,
      description: payload.description?.trim() || undefined,
    });
    return res.data;
  },

  updateSubject: async (subjectId: string, payload: UpdateSubjectPayload): Promise<SubjectItem> => {
    const res = await apiClient.patch<SubjectItem>(`/subjects/${subjectId}`, {
      name: payload.name?.trim() || undefined,
      code: payload.code?.trim() || undefined,
      description: payload.description?.trim() || undefined,
    });
    return res.data;
  },

  deleteSubject: async (subjectId: string): Promise<{ message: string }> => {
    const res = await apiClient.delete<{ message: string }>(`/subjects/${subjectId}`);
    return res.data;
  },

  // Class-Subject Junction Links
  listClassSubjects: async (classId: string): Promise<SubjectItem[]> => {
    const res = await apiClient.get<SubjectItem[]>(`/classes/${classId}/subjects`);
    return res.data;
  },

  linkSubjectToClass: async (classId: string, subjectId: string): Promise<{ message: string }> => {
    const res = await apiClient.post<{ message: string }>(`/classes/${classId}/subjects/${subjectId}`);
    return res.data;
  },

  unlinkSubjectFromClass: async (classId: string, subjectId: string): Promise<{ message: string }> => {
    const res = await apiClient.delete<{ message: string }>(`/classes/${classId}/subjects/${subjectId}`);
    return res.data;
  },

  // Scoped "My Classes" for Teacher & Student
  getMyClasses: async (): Promise<MyClassItem[]> => {
    const res = await apiClient.get<MyClassItem[]>("/me/classes");
    return res.data;
  },
};
