import { apiClient } from "@/api/client";
import {
  validateResponse,
  MyTimetableResponseSchema,
  NextLectureResponseSchema,
  TimetableItemSchema,
  TimetableEntryItemSchema,
  AssignedTeacherListSchema,
} from "../schemas/timetableSchemas";
import type {
  TimetableEntryItem,
  TimetableItem,
  ConflictItem,
  ConflictCheckResponse,
  NextLectureItem,
  NextLectureResponse,
  EntryFormPayload,
  TimezoneResponse,
  MyTimetableResponse,
  AssignedTeacher,
} from "../types";

export type {
  TimetableEntryItem,
  TimetableItem,
  ConflictItem,
  ConflictCheckResponse,
  NextLectureItem,
  NextLectureResponse,
  EntryFormPayload,
  TimezoneResponse,
  MyTimetableResponse,
  AssignedTeacher,
};

export const timetableApi = {
  // Published or active timetable for a class
  getClassTimetable: async (classId: string): Promise<TimetableItem | null> => {
    const res = await apiClient.get<unknown>(`/classes/${classId}/timetables`);
    if (Array.isArray(res.data)) {
      const items = res.data.map((item) =>
        validateResponse(TimetableItemSchema, item, "class timetable")
      );
      const published = items.find((t) => t.status === "published");
      return published || items[0] || null;
    }
    if (res.data) {
      return validateResponse(TimetableItemSchema, res.data, "class timetable");
    }
    return null;
  },

  // Get or auto-create draft timetable for editing
  getDraftTimetable: async (classId: string): Promise<TimetableItem> => {
    const res = await apiClient.get<unknown>(`/classes/${classId}/timetables/draft`);
    return validateResponse(TimetableItemSchema, res.data, "draft timetable");
  },

  // Explicitly create new draft
  createDraft: async (
    classId: string,
    payload?: { academic_year?: string; term?: string }
  ): Promise<TimetableItem> => {
    const res = await apiClient.post<unknown>(`/classes/${classId}/timetables/draft`, payload || {});
    return validateResponse(TimetableItemSchema, res.data, "draft timetable");
  },

  // Add a single entry to draft
  addDraftEntry: async (classId: string, payload: EntryFormPayload): Promise<TimetableEntryItem> => {
    const res = await apiClient.post<unknown>(
      `/classes/${classId}/timetables/draft/entries`,
      payload
    );
    return validateResponse(TimetableEntryItemSchema, res.data, "timetable entry");
  },

  // Update a single entry in draft
  updateDraftEntry: async (
    classId: string,
    entryId: string,
    payload: EntryFormPayload
  ): Promise<TimetableEntryItem> => {
    const res = await apiClient.put<unknown>(
      `/classes/${classId}/timetables/draft/entries/${entryId}`,
      payload
    );
    return validateResponse(TimetableEntryItemSchema, res.data, "timetable entry");
  },

  // Delete an entry from draft
  deleteDraftEntry: async (classId: string, entryId: string): Promise<void> => {
    await apiClient.delete(`/classes/${classId}/timetables/draft/entries/${entryId}`);
  },

  // Run dry conflict check across the tenant
  checkConflicts: async (classId: string): Promise<ConflictCheckResponse> => {
    const res = await apiClient.post<ConflictCheckResponse>(
      `/classes/${classId}/timetables/check-conflicts`
    );
    return res.data;
  },

  // Publish timetable (confirm_conflicts defaults to false)
  publishTimetable: async (
    classId: string,
    confirmConflicts = false
  ): Promise<TimetableItem> => {
    const res = await apiClient.post<unknown>(`/classes/${classId}/timetables/publish`, {
      confirm_conflicts: confirmConflicts,
    });
    return validateResponse(TimetableItemSchema, res.data, "published timetable");
  },

  // Revert published timetable to draft
  unpublishTimetable: async (classId: string): Promise<TimetableItem> => {
    const res = await apiClient.post<unknown>(`/classes/${classId}/timetables/unpublish`);
    return validateResponse(TimetableItemSchema, res.data, "draft timetable");
  },

  // Next lecture for current user (student, teacher, or admin)
  getMyNextLecture: async (classId?: string): Promise<NextLectureResponse> => {
    const res = await apiClient.get<unknown>("/me/next-lecture", {
      params: classId ? { class_id: classId } : undefined,
    });
    return validateResponse(NextLectureResponseSchema, res.data, "next lecture");
  },

  // Published timetables for current user
  getMyTimetable: async (classId?: string): Promise<MyTimetableResponse> => {
    const res = await apiClient.get<unknown>("/me/timetable", {
      params: classId ? { class_id: classId } : undefined,
    });
    return validateResponse(MyTimetableResponseSchema, res.data, "user timetable");
  },

  // Teachers assigned to a class (optionally one subject). Scoped endpoint, no admin rights needed.
  listAssignedTeachers: async (classId: string, subjectId?: string): Promise<AssignedTeacher[]> => {
    const url = subjectId
      ? `/classes/${classId}/subjects/${subjectId}/assigned-teachers`
      : `/classes/${classId}/assigned-teachers`;
    const res = await apiClient.get<unknown>(url);
    return validateResponse(AssignedTeacherListSchema, res.data, "assigned teachers");
  },

  // Timezone endpoints
  getTimezone: async (): Promise<TimezoneResponse> => {
    const res = await apiClient.get<TimezoneResponse>("/admin/settings/timezone");
    return res.data;
  },

  updateTimezone: async (timezone: string): Promise<TimezoneResponse> => {
    const res = await apiClient.patch<TimezoneResponse>("/admin/settings/timezone", {
      timezone,
    });
    return res.data;
  },
};
