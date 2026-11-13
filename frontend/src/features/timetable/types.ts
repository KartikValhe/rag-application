export interface TimetableEntryItem {
  id: string;
  timetable_id: string;
  class_id: string;
  class_name?: string | null;
  subject_id: string;
  subject_name?: string | null;
  subject_code?: string | null;
  teacher_id?: string | null;
  teacher_name?: string | null;
  day_of_week: number; // 0 = Monday, ..., 6 = Sunday
  day_name?: string | null;
  start_time: string; // "09:00" or "09:00:00"
  end_time: string;
  room?: string | null;
  note?: string | null;
  created_at: string;
}

export interface TimetableItem {
  id: string;
  tenant_id: string;
  class_id: string;
  class_name?: string | null;
  academic_year?: string | null;
  term?: string | null;
  status: "draft" | "published";
  version: number;
  entries: TimetableEntryItem[];
  created_at: string;
  updated_at: string;
}

export interface ConflictItem {
  conflict_type: "teacher_overlap" | "room_overlap" | "class_overlap";
  day_of_week: number;
  start_time: string;
  end_time: string;
  existing_entry_id?: string | null;
  details: {
    teacher_id?: string;
    teacher_name?: string;
    room?: string;
    class_id?: string;
    class_name?: string;
    subject_id?: string;
    subject_name?: string;
    [key: string]: unknown;
  };
}

export interface ConflictCheckResponse {
  has_conflicts: boolean;
  conflicts: ConflictItem[];
}

export interface NextLectureItem {
  entry_id?: string | null;
  class_id: string;
  class_name: string;
  subject_id: string;
  subject_name?: string | null;
  subject_code?: string | null;
  teacher_id?: string | null;
  teacher_name?: string | null;
  room?: string | null;
  note?: string | null;
  day_of_week: number;
  day_name: string;
  date?: string;
  start_time: string;
  end_time: string;
  is_current?: boolean;
}

export interface NextLectureResponse {
  status: string;
  message: string;
  current_lecture: NextLectureItem | null;
  next_lecture: NextLectureItem | null;
  tenant_timezone: string;
}

export interface EntryFormPayload {
  day_of_week: number;
  start_time: string;
  end_time: string;
  subject_id: string;
  teacher_id?: string | null;
  room?: string | null;
  note?: string | null;
}

export interface TimezoneResponse {
  timezone: string;
}

export interface MyTimetableResponse {
  tenant_timezone: string;
  classes: TimetableItem[];
  teaching_entries: TimetableEntryItem[];
}



export interface ImportPreviewResponse {
  filename?: string;
  total_rows: number;
  valid_rows: number;
  error_rows: number;
  requires_period_times?: boolean;
  period_labels?: string[];
  rows: Array<{
    row_index: number;
    day_of_week?: number | null;
    start_time?: string | null;
    end_time?: string | null;
    subject_id?: string | null;
    teacher_id?: string | null;
    room?: string | null;
    note?: string | null;
    raw_data?: Record<string, string | null | undefined>;
    period_label?: string | null;
    requires_period_time?: boolean;
    warnings?: string[];
  }>;
  available_subjects: Array<{ id: string; code?: string | null; name: string }>;
  available_teachers: Array<{ id: string; full_name: string; email?: string | null }>;
}

export interface AssignedTeacher {
  id: string;
  full_name?: string | null;
  email?: string | null; // admins only; null or absent for everyone else
}
