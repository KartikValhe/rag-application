import { apiClient } from "@/api/client";
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
};

export const timetableApi = {
  // Published or active timetable for a class
  getClassTimetable: async (classId: string): Promise<TimetableItem> => {
    const res = await apiClient.get<TimetableItem>(`/classes/${classId}/timetables`);
    return res.data;
  },

  // Get or auto-create draft timetable for editing
  getDraftTimetable: async (classId: string): Promise<TimetableItem> => {
    const res = await apiClient.get<TimetableItem>(`/classes/${classId}/timetables/draft`);
    return res.data;
  },

  // Explicitly create new draft
  createDraft: async (
    classId: string,
    payload?: { academic_year?: string; term?: string }
  ): Promise<TimetableItem> => {
    const res = await apiClient.post<TimetableItem>(`/classes/${classId}/timetables/draft`, payload || {});
    return res.data;
  },

  // Add a single entry to draft
  addDraftEntry: async (classId: string, payload: EntryFormPayload): Promise<TimetableEntryItem> => {
    const res = await apiClient.post<TimetableEntryItem>(
      `/classes/${classId}/timetables/draft/entries`,
      payload
    );
    return res.data;
  },

  // Update a single entry in draft
  updateDraftEntry: async (
    classId: string,
    entryId: string,
    payload: EntryFormPayload
  ): Promise<TimetableEntryItem> => {
    const res = await apiClient.put<TimetableEntryItem>(
      `/classes/${classId}/timetables/draft/entries/${entryId}`,
      payload
    );
    return res.data;
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
    const res = await apiClient.post<TimetableItem>(`/classes/${classId}/timetables/publish`, {
      confirm_conflicts: confirmConflicts,
    });
    return res.data;
  },

  // Revert published timetable to draft
  unpublishTimetable: async (classId: string): Promise<TimetableItem> => {
    const res = await apiClient.post<TimetableItem>(`/classes/${classId}/timetables/unpublish`);
    return res.data;
  },

  // Next lecture for current user (student, teacher, or admin)
  getMyNextLecture: async (classId?: string): Promise<NextLectureResponse> => {
    const res = await apiClient.get<NextLectureResponse>("/me/next-lecture", {
      params: classId ? { class_id: classId } : undefined,
    });
    return res.data;
  },

  // Published timetables for current user
  getMyTimetable: async (classId?: string): Promise<MyTimetableResponse> => {
    const res = await apiClient.get<MyTimetableResponse>("/me/timetable", {
      params: classId ? { class_id: classId } : undefined,
    });
    return res.data;
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
