import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  MyTimetableResponseSchema,
  NextLectureResponseSchema,
  validateResponse,
} from "../schemas/timetableSchemas.ts";

describe("Timetable API Schema Contract & Zod Validation", () => {
  it("successfully parses valid MyTimetableResponse payload", () => {
    const rawData = {
      tenant_timezone: "Asia/Kolkata",
      classes: [
        {
          id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
          tenant_id: "00000000-0000-0000-0000-000000000001",
          class_id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
          class_name: "Class 10-A",
          academic_year: "2026-2027",
          term: "Term 1",
          status: "published",
          version: 1,
          entries: [
            {
              id: "11111111-1111-1111-1111-111111111111",
              timetable_id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
              class_id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
              class_name: "Class 10-A",
              subject_id: "22222222-2222-2222-2222-222222222222",
              subject_name: "Mathematics",
              subject_code: "MATH101",
              teacher_id: "33333333-3333-3333-3333-333333333333",
              teacher_name: "John Doe",
              day_of_week: 0,
              day_name: "Monday",
              start_time: "09:00",
              end_time: "10:00",
              room: "Room 101",
              note: null,
              created_at: "2026-11-11T10:00:00Z",
            },
          ],
          created_at: "2026-11-11T09:00:00Z",
          updated_at: "2026-11-11T09:30:00Z",
        },
      ],
      teaching_entries: [],
    };

    const validated = validateResponse(MyTimetableResponseSchema, rawData, "timetable");
    assert.equal(validated.tenant_timezone, "Asia/Kolkata");
    assert.equal(validated.classes.length, 1);
    assert.equal(validated.classes[0].class_name, "Class 10-A");
    assert.equal(validated.classes[0].entries[0].subject_name, "Mathematics");
  });

  it("successfully parses valid NextLectureResponse payload with current and next lecture", () => {
    const rawData = {
      status: "upcoming_today",
      message: "Your next lecture today is 'Physics' at 11:00.",
      current_lecture: null,
      next_lecture: {
        entry_id: "44444444-4444-4444-4444-444444444444",
        class_id: "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        class_name: "Class 10-A",
        subject_id: "55555555-5555-5555-5555-555555555555",
        subject_name: "Physics",
        subject_code: "PHY101",
        teacher_id: "33333333-3333-3333-3333-333333333333",
        teacher_name: "Jane Smith",
        room: "Lab 2",
        day_of_week: 1,
        day_name: "Tuesday",
        date: "2026-11-11",
        start_time: "11:00",
        end_time: "12:00",
        is_current: false,
        note: null,
      },
      tenant_timezone: "Asia/Kolkata",
    };

    const validated = validateResponse(NextLectureResponseSchema, rawData, "next lecture");
    assert.equal(validated.status, "upcoming_today");
    assert.equal(validated.current_lecture, null);
    assert.equal(validated.next_lecture?.subject_name, "Physics");
    assert.equal(validated.next_lecture?.entry_id, "44444444-4444-4444-4444-444444444444");
  });

  it("throws friendly error message on schema mismatch", () => {
    const badData = {
      tenant_timezone: 12345, // invalid type
      classes: "not an array",
    };

    assert.throws(
      () => validateResponse(MyTimetableResponseSchema, badData, "timetable"),
      (err: Error) => {
        assert.match(err.message, /Data format error while loading timetable/);
        return true;
      }
    );
  });
});

