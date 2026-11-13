import { z } from "zod";
import { SCHEMA_MISMATCH_NAME } from "../../../api/errorMessages.ts";

export const TimetableEntryItemSchema = z.object({
  id: z.string(),
  timetable_id: z.string(),
  class_id: z.string(),
  class_name: z.string().nullable().optional(),
  subject_id: z.string(),
  subject_name: z.string().nullable().optional(),
  subject_code: z.string().nullable().optional(),
  teacher_id: z.string().nullable().optional(),
  teacher_name: z.string().nullable().optional(),
  day_of_week: z.number().int().min(0).max(6),
  day_name: z.string().nullable().optional(),
  start_time: z.string(),
  end_time: z.string(),
  room: z.string().nullable().optional(),
  note: z.string().nullable().optional(),
  created_at: z.string(),
});

export const TimetableItemSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  class_id: z.string(),
  class_name: z.string().nullable().optional(),
  academic_year: z.string().nullable().optional(),
  term: z.string().nullable().optional(),
  status: z.enum(["draft", "published"]),
  version: z.number(),
  entries: z.array(TimetableEntryItemSchema),
  created_at: z.string(),
  updated_at: z.string(),
});

export const MyTimetableResponseSchema = z.object({
  tenant_timezone: z.string(),
  classes: z.array(TimetableItemSchema),
  teaching_entries: z.array(TimetableEntryItemSchema),
});

export const NextLectureItemSchema = z.object({
  entry_id: z.string().nullable().optional(),
  class_id: z.string(),
  class_name: z.string(),
  subject_id: z.string(),
  subject_name: z.string().nullable().optional(),
  subject_code: z.string().nullable().optional(),
  teacher_id: z.string().nullable().optional(),
  teacher_name: z.string().nullable().optional(),
  room: z.string().nullable().optional(),
  note: z.string().nullable().optional(),
  day_of_week: z.number().int().min(0).max(6),
  day_name: z.string(),
  date: z.string().optional(),
  start_time: z.string(),
  end_time: z.string(),
  is_current: z.boolean().optional(),
});

export const NextLectureResponseSchema = z.object({
  status: z.string(),
  message: z.string(),
  current_lecture: NextLectureItemSchema.nullable(),
  next_lecture: NextLectureItemSchema.nullable(),
  tenant_timezone: z.string(),
});

// Mirrors backend TimetableConflict (app/schemas/timetable.py). Every key is required;
// strict() rejects unknown keys so contract drift fails loudly instead of crashing the UI.
export const ConflictItemSchema = z
  .object({
    conflict_type: z.enum(["teacher_overlap", "room_overlap", "class_overlap"]),
    day_of_week: z.number().int().min(0).max(6),
    day_name: z.string(),
    start_time: z.string(),
    end_time: z.string(),
    subject_name: z.string(),
    teacher_name: z.string().nullable(),
    room: z.string().nullable(),
    other_class_name: z.string(),
    other_subject_name: z.string(),
    other_start_time: z.string(),
    other_end_time: z.string(),
    description: z.string(),
  })
  .strict();

export const ConflictCheckResponseSchema = z
  .object({
    has_conflicts: z.boolean(),
    conflicts: z.array(ConflictItemSchema),
  })
  .strict();

// 409 body from POST /timetables/publish without confirm_conflicts.
export const PublishConflictErrorSchema = z.object({
  code: z.literal("timetable_conflicts_detected"),
  message: z.string(),
  conflicts: z.array(ConflictItemSchema),
});

// Email is only sent to tenant admins; for everyone else it is null or missing.
export const AssignedTeacherSchema = z.object({
  id: z.string(),
  full_name: z.string().nullable().optional(),
  email: z.string().nullable().optional(),
});

export const AssignedTeacherListSchema = z.array(AssignedTeacherSchema);

export const ImportPreviewRowSchema = z.object({
  row_index: z.number().int(),
  day_of_week: z.number().int().nullable().optional(),
  start_time: z.string().nullable().optional(),
  end_time: z.string().nullable().optional(),
  subject_id: z.string().nullable().optional(),
  teacher_id: z.string().nullable().optional(),
  room: z.string().nullable().optional(),
  note: z.string().nullable().optional(),
  raw_data: z.record(z.string().nullable().optional()).optional(),
  period_label: z.string().nullable().optional(),
  requires_period_time: z.boolean().optional(),
  warnings: z.array(z.string()).optional(),
});

export const ImportPreviewResponseSchema = z.object({
  filename: z.string().optional(),
  total_rows: z.number(),
  valid_rows: z.number(),
  error_rows: z.number(),
  requires_period_times: z.boolean().optional(),
  period_labels: z.array(z.string()).optional(),
  rows: z.array(ImportPreviewRowSchema),
  available_subjects: z.array(
    z.object({ id: z.string(), code: z.string().nullable().optional(), name: z.string() })
  ),
  available_teachers: z.array(
    z.object({ id: z.string(), full_name: z.string(), email: z.string().nullable().optional() })
  ),
});

/**
 * Validates data from an API response against a Zod schema.
 * On schema mismatch:
 * - Logs full details in development only (import.meta.env.DEV)
 * - Throws a friendly error message suitable for end-user display.
 */
export function validateResponse<T>(
  schema: z.ZodType<T>,
  data: unknown,
  endpointName: string
): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    const isDev =
      (typeof import.meta !== "undefined" &&
        typeof import.meta.env !== "undefined" &&
        Boolean(import.meta.env?.DEV)) ||
      (typeof process !== "undefined" && process.env?.NODE_ENV !== "production");

    if (isDev) {
      console.error(
        `[API Schema Validation Failed] ${endpointName}:`,
        result.error.format(),
        "\nPayload received:",
        data
      );
    }
    const mismatch = new Error(
      `Data format error while loading ${endpointName}. Please refresh or contact support.`
    );
    mismatch.name = SCHEMA_MISMATCH_NAME;
    throw mismatch;
  }
  return result.data;
}
