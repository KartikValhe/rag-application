import test from "node:test";
import assert from "node:assert/strict";
import {
  getPythonWeekday,
  normalizeTimeString,
  formatTime12h,
  formatTimeRange,
  isEndTimeAfterStartTime,
  groupEntriesByDay,
  formatConflictDetails,
  WEEKDAYS,
  DAY_NAMES,
} from "../utils/timetableHelpers.ts";
import type { TimetableEntryItem, ConflictItem } from "../types.ts";

test("WEEKDAYS & DAY_NAMES: complete 7-day mapping matching Python (0 = Monday, 6 = Sunday)", () => {
  assert.equal(WEEKDAYS.length, 7);
  assert.equal(DAY_NAMES[0], "Monday");
  assert.equal(DAY_NAMES[1], "Tuesday");
  assert.equal(DAY_NAMES[2], "Wednesday");
  assert.equal(DAY_NAMES[3], "Thursday");
  assert.equal(DAY_NAMES[4], "Friday");
  assert.equal(DAY_NAMES[5], "Saturday");
  assert.equal(DAY_NAMES[6], "Sunday");
});

test("getPythonWeekday: accurately translates JavaScript day to Python 0..6 weekday index", () => {
  // Monday: 2026-11-09
  const monday = new Date("2026-11-09T10:00:00Z");
  assert.equal(getPythonWeekday(monday), 0);

  // Wednesday: 2026-11-11
  const wednesday = new Date("2026-11-11T12:00:00Z");
  assert.equal(getPythonWeekday(wednesday), 2);

  // Friday: 2026-11-13
  const friday = new Date("2026-11-13T15:00:00Z");
  assert.equal(getPythonWeekday(friday), 4);

  // Sunday: 2026-11-15
  const sunday = new Date("2026-11-15T08:00:00Z");
  assert.equal(getPythonWeekday(sunday), 6);
});

test("normalizeTimeString: converts various time shapes to HH:MM format", () => {
  assert.equal(normalizeTimeString("9:00"), "09:00");
  assert.equal(normalizeTimeString("09:00"), "09:00");
  assert.equal(normalizeTimeString("09:00:00"), "09:00");
  assert.equal(normalizeTimeString("14:30:45"), "14:30");
  assert.equal(normalizeTimeString(""), "");
  assert.equal(normalizeTimeString(undefined), "");
});

test("formatTime12h: formats 24h string to 12h AM/PM string", () => {
  assert.equal(formatTime12h("09:00"), "9:00 AM");
  assert.equal(formatTime12h("09:30:00"), "9:30 AM");
  assert.equal(formatTime12h("12:00"), "12:00 PM");
  assert.equal(formatTime12h("13:15"), "1:15 PM");
  assert.equal(formatTime12h("00:00"), "12:00 AM");
});

test("formatTimeRange: formats start and end times into friendly range", () => {
  assert.equal(formatTimeRange("09:00", "10:00"), "9:00 AM – 10:00 AM");
  assert.equal(formatTimeRange("13:00", "14:30"), "1:00 PM – 2:30 PM");
});

test("isEndTimeAfterStartTime: strictly validates start < end", () => {
  assert.equal(isEndTimeAfterStartTime("09:00", "10:00"), true);
  assert.equal(isEndTimeAfterStartTime("09:00", "09:01"), true);
  assert.equal(isEndTimeAfterStartTime("10:00", "09:00"), false);
  assert.equal(isEndTimeAfterStartTime("09:00", "09:00"), false);
  assert.equal(isEndTimeAfterStartTime("14:00", "12:00"), false);
});

test("groupEntriesByDay: groups entries by weekday index and sorts chronologically", () => {
  const mockEntries: TimetableEntryItem[] = [
    {
      id: "1",
      timetable_id: "tt-1",
      class_id: "c-1",
      subject_id: "s-1",
      subject_name: "Physics",
      day_of_week: 0,
      day_name: "Monday",
      start_time: "10:15",
      end_time: "11:15",
      created_at: "",
    },
    {
      id: "2",
      timetable_id: "tt-1",
      class_id: "c-1",
      subject_id: "s-2",
      subject_name: "Math",
      day_of_week: 0,
      day_name: "Monday",
      start_time: "09:00",
      end_time: "10:00",
      created_at: "",
    },
    {
      id: "3",
      timetable_id: "tt-1",
      class_id: "c-1",
      subject_id: "s-3",
      subject_name: "Chemistry",
      day_of_week: 2,
      day_name: "Wednesday",
      start_time: "11:30",
      end_time: "12:30",
      created_at: "",
    },
  ];

  const grouped = groupEntriesByDay(mockEntries);

  // Monday (0) must have 2 entries, sorted with 09:00 before 10:15
  assert.equal(grouped[0].length, 2);
  assert.equal(grouped[0][0].subject_name, "Math");
  assert.equal(grouped[0][1].subject_name, "Physics");

  // Tuesday (1) must be empty
  assert.equal(grouped[1].length, 0);

  // Wednesday (2) must have 1 entry
  assert.equal(grouped[2].length, 1);
  assert.equal(grouped[2][0].subject_name, "Chemistry");
});

test("formatConflictDetails: formats teacher, room, and class overlap conflicts clearly", () => {
  // 1. Teacher overlap
  const teacherConflict: ConflictItem = {
    conflict_type: "teacher_overlap",
    day_of_week: 0,
    start_time: "09:00",
    end_time: "10:00",
    details: {
      teacher_name: "Prof. Einstein",
      class_name: "Class 10-B",
    },
  };
  const teacherFmt = formatConflictDetails(teacherConflict);
  assert.equal(teacherFmt.title, "Teacher Double-Booked");
  assert.match(teacherFmt.description, /Prof\. Einstein is already scheduled to teach in Class 10-B/);
  assert.match(teacherFmt.description, /Monday during 9:00 AM – 10:00 AM/);

  // 2. Room overlap
  const roomConflict: ConflictItem = {
    conflict_type: "room_overlap",
    day_of_week: 2,
    start_time: "11:00",
    end_time: "12:00",
    details: {
      room: "Lab 301",
      class_name: "Class 12-A",
    },
  };
  const roomFmt = formatConflictDetails(roomConflict);
  assert.equal(roomFmt.title, "Room Overlap");
  assert.match(roomFmt.description, /Lab 301 is already booked by Class 12-A/);
  assert.match(roomFmt.description, /Wednesday during 11:00 AM – 12:00 PM/);

  // 3. Class overlap
  const classConflict: ConflictItem = {
    conflict_type: "class_overlap",
    day_of_week: 4,
    start_time: "14:00",
    end_time: "15:00",
    details: {},
  };
  const classFmt = formatConflictDetails(classConflict);
  assert.equal(classFmt.title, "Class Schedule Overlap");
  assert.match(classFmt.description, /Friday during 2:00 PM – 3:00 PM/);
});
