import type { TimetableEntryItem, ConflictItem } from "../types.ts";

export const WEEKDAYS = [
  { index: 0, label: "Monday", short: "Mon" },
  { index: 1, label: "Tuesday", short: "Tue" },
  { index: 2, label: "Wednesday", short: "Wed" },
  { index: 3, label: "Thursday", short: "Thu" },
  { index: 4, label: "Friday", short: "Fri" },
  { index: 5, label: "Saturday", short: "Sat" },
  { index: 6, label: "Sunday", short: "Sun" },
] as const;

export const DAY_NAMES: Record<number, string> = {
  0: "Monday",
  1: "Tuesday",
  2: "Wednesday",
  3: "Thursday",
  4: "Friday",
  5: "Saturday",
  6: "Sunday",
};

export const SHORT_DAY_NAMES: Record<number, string> = {
  0: "Mon",
  1: "Tue",
  2: "Wed",
  3: "Thu",
  4: "Fri",
  5: "Sat",
  6: "Sun",
};

/**
 * Converts a standard Date into Python's weekday index:
 * 0 = Monday, ..., 6 = Sunday
 */
export function getPythonWeekday(date: Date = new Date()): number {
  const jsDay = date.getDay(); // 0 = Sun, 1 = Mon, ..., 6 = Sat
  return (jsDay + 6) % 7;
}

/**
 * Normalizes time string ("09:00", "09:00:00") into "HH:MM"
 */
export function normalizeTimeString(timeStr: string | null | undefined): string {
  if (!timeStr) return "";
  const parts = timeStr.trim().split(":");
  if (parts.length >= 2) {
    return `${parts[0].padStart(2, "0")}:${parts[1].padStart(2, "0")}`;
  }
  return timeStr;
}

/**
 * Formats "09:00" into 12-hour format "9:00 AM" or "09:00"
 */
export function formatTime12h(timeStr: string | null | undefined): string {
  if (!timeStr) return "";
  const normalized = normalizeTimeString(timeStr);
  const [hh, mm] = normalized.split(":");
  const hour = parseInt(hh, 10);
  if (isNaN(hour)) return timeStr;

  const ampm = hour >= 12 ? "PM" : "AM";
  const displayHour = hour % 12 || 12;
  return `${displayHour}:${mm} ${ampm}`;
}

/**
 * Formats start and end times into a readable range (e.g., "9:00 AM – 10:00 AM")
 */
export function formatTimeRange(start: string, end: string): string {
  const fStart = formatTime12h(start);
  const fEnd = formatTime12h(end);
  if (!fStart) return fEnd;
  if (!fEnd) return fStart;
  return `${fStart} – ${fEnd}`;
}

/**
 * Validates that end time is strictly greater than start time.
 */
export function isEndTimeAfterStartTime(start: string, end: string): boolean {
  const nStart = normalizeTimeString(start);
  const nEnd = normalizeTimeString(end);
  return nEnd > nStart;
}

/**
 * Groups and sorts timetable entries by day of week (0..6)
 */
export function groupEntriesByDay(
  entries: TimetableEntryItem[] = []
): Record<number, TimetableEntryItem[]> {
  const grouped: Record<number, TimetableEntryItem[]> = {
    0: [],
    1: [],
    2: [],
    3: [],
    4: [],
    5: [],
    6: [],
  };

  for (const entry of entries) {
    if (grouped[entry.day_of_week]) {
      grouped[entry.day_of_week].push(entry);
    }
  }

  // Sort each day's entries chronologically
  for (let day = 0; day <= 6; day++) {
    grouped[day].sort((a, b) => {
      const aStart = normalizeTimeString(a.start_time);
      const bStart = normalizeTimeString(b.start_time);
      return aStart.localeCompare(bStart);
    });
  }

  return grouped;
}

/**
 * Formats a single conflict item into a human-readable title and description.
 */
export function formatConflictDetails(conflict: ConflictItem): {
  title: string;
  description: string;
} {
  const dayName = DAY_NAMES[conflict.day_of_week] || `Day ${conflict.day_of_week}`;
  const range = formatTimeRange(conflict.start_time, conflict.end_time);

  switch (conflict.conflict_type) {
    case "teacher_overlap": {
      const teacher = conflict.details.teacher_name || "Assigned teacher";
      const otherClass = conflict.details.class_name || "another class";
      return {
        title: "Teacher Double-Booked",
        description: `${teacher} is already scheduled to teach in ${otherClass} on ${dayName} during ${range}.`,
      };
    }
    case "room_overlap": {
      const room = conflict.details.room || "Specified room";
      const otherClass = conflict.details.class_name || "another class";
      return {
        title: "Room Overlap",
        description: `${room} is already booked by ${otherClass} on ${dayName} during ${range}.`,
      };
    }
    case "class_overlap": {
      return {
        title: "Class Schedule Overlap",
        description: `This class already has another lecture scheduled on ${dayName} during ${range}.`,
      };
    }
    default:
      return {
        title: "Schedule Conflict",
        description: `Conflict detected on ${dayName} during ${range}.`,
      };
  }
}

/** Display name for a teacher. Uses the name only and never falls back to an email. */
export function teacherDisplayName(t: { full_name?: string | null }): string {
  const name = t.full_name?.trim();
  return name ? name : "Unnamed teacher";
}
