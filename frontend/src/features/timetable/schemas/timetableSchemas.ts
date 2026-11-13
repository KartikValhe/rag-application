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

// Email is only sent to tenant admins; for everyone else it is null or missing.
export const AssignedTeacherSchema = z.object({
  id: z.string(),
  full_name: z.string().nullable().optional(),
  email: z.string().nullable().optional(),
});

export const AssignedTeacherListSchema = z.array(AssignedTeacherSchema);

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
