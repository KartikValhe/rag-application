export interface TimetableEntryItem {
  id: string;
  timetable_id: string;
  class_id: string;
  subject_id: string;
  subject_name: string;
  subject_code?: string | null;
  teacher_id?: string | null;
  teacher_name?: string | null;
  day_of_week: number; // 0 = Monday, ..., 6 = Sunday
  day_name: string;
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
  entry_id: string;
  class_id: string;
  class_name: string;
  subject_id: string;
  subject_name: string;
  subject_code?: string | null;
  teacher_id?: string | null;
  teacher_name?: string | null;
  room?: string | null;
  note?: string | null;
  day_of_week: number;
  day_name: string;
  date: string;
  start_time: string;
  end_time: string;
  is_current: boolean;
}

export interface NextLectureResponse {
  now_local: string;
  timezone: string;
  current_lecture: NextLectureItem | null;
  next_lecture: NextLectureItem | null;
  has_timetable: boolean;
  has_more_today: boolean;
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


