import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  AssignedTeacherListSchema,
  MyTimetableResponseSchema,
  NextLectureResponseSchema,
  validateResponse,
} from "../schemas/timetableSchemas.ts";
import { teacherDisplayName } from "../utils/timetableHelpers.ts";

describe("Assigned teachers: missing email handling", () => {
  it("parses teachers with null, missing and present emails", () => {
    const list = validateResponse(
      AssignedTeacherListSchema,
      [
        { id: "1", full_name: "Ada", email: null },
        { id: "2", full_name: "Bob" },
        { id: "3", full_name: "Cy", email: "cy@example.test" },
      ],
      "assigned teachers"
    );
    assert.equal(list.length, 3);
    assert.equal(list[0].email, null);
    assert.equal(list[1].email, undefined);
  });

  it("displays the name only and never the email", () => {
    assert.equal(teacherDisplayName({ full_name: "Ada" }), "Ada");
    const t = { full_name: null, email: "secret@example.test" };
    assert.equal(teacherDisplayName(t), "Unnamed teacher");
    assert.ok(!teacherDisplayName(t).includes("@"));
  });

  it("rejects a teacher list that is not an array", () => {
    assert.throws(() =>
      validateResponse(AssignedTeacherListSchema, { items: [] }, "assigned teachers")
    );
  });
});

describe("/me contracts: documented shapes and mismatches", () => {
  it("accepts a next-lecture payload exactly as documented (no date or is_current)", () => {
    const lecture = {
      class_id: "a",
      class_name: "10-A",
      subject_id: "b",
      subject_name: "Maths",
      subject_code: "M1",
      teacher_id: "c",
      teacher_name: "T",
      day_of_week: 0,
      day_name: "Monday",
      start_time: "09:00",
      end_time: "10:00",
      room: null,
      note: null,
    };
    const v = validateResponse(
      NextLectureResponseSchema,
      {
        status: "upcoming_today",
        message: "m",
        current_lecture: null,
        next_lecture: lecture,
        tenant_timezone: "UTC",
      },
      "next lecture"
    );
    assert.equal(v.next_lecture?.subject_name, "Maths");
    assert.equal(v.next_lecture?.date, undefined);
  });

  it("rejects a next-lecture payload with a wrong shape", () => {
    assert.throws(
      () =>
        validateResponse(NextLectureResponseSchema, { status: "x", next_lecture: [] }, "next lecture"),
      (err: Error) => err.name === "SchemaMismatchError"
    );
  });

  it("rejects an out-of-range weekday in a timetable entry", () => {
    const bad = {
      tenant_timezone: "UTC",
      classes: [],
      teaching_entries: [
        {
          id: "1",
          timetable_id: "t",
          class_id: "c",
          subject_id: "s",
          day_of_week: 9,
          start_time: "09:00",
          end_time: "10:00",
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
    };
    assert.throws(() => validateResponse(MyTimetableResponseSchema, bad, "timetable"));
  });
});
