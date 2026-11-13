import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  ConflictCheckResponseSchema,
  validateResponse,
} from "../schemas/timetableSchemas.ts";
import {
  conflictsFromPublishError,
  formatConflictDetails,
  nextPublishStep,
} from "../utils/timetableHelpers.ts";

// Real check-conflicts response for the dev scenario (captured from
// backend/tests/test_timetable_conflicts.py): draft 10-A Wed 11-12 vs published 10-B Wed 11:30-12:30.
const REAL_RESPONSE = {
  has_conflicts: true,
  conflicts: [
    {
      conflict_type: "teacher_overlap",
      day_of_week: 2,
      day_name: "Wednesday",
      start_time: "11:00",
      end_time: "12:00",
      subject_name: "Mathematics",
      teacher_name: "Ravi Teacher",
      room: "Room 101",
      other_class_name: "Class 10-B",
      other_subject_name: "Mathematics",
      other_start_time: "11:30",
      other_end_time: "12:30",
      description:
        "Teacher 'Ravi Teacher' is already scheduled in 'Class 10-B' on Wednesday 11:30-12:30.",
    },
  ],
};

// The shape the backend sent before the fix: no other_* fields and no `details` object.
const OLD_RESPONSE = {
  has_conflicts: true,
  conflicts: [
    {
      conflict_type: "teacher_overlap",
      day_of_week: 2,
      day_name: "Wednesday",
      start_time: "11:00:00",
      end_time: "12:00:00",
      description: "Teacher 'Ravi Teacher' is already scheduled in class 'Class 10-B' ...",
      class_name: null,
      subject_name: null,
      teacher_name: "Ravi Teacher",
      room: null,
    },
  ],
};

describe("check-conflicts contract", () => {
  it("parses the real backend response and renders the dialog text without crashing", () => {
    const parsed = validateResponse(ConflictCheckResponseSchema, REAL_RESPONSE, "conflict check");
    assert.equal(nextPublishStep(parsed), "confirm");
    const { title, description, lecture } = formatConflictDetails(parsed.conflicts[0]);
    assert.equal(title, "Teacher Double-Booked");
    assert.match(description, /Ravi Teacher/);
    assert.match(description, /Class 10-B/);
    assert.match(description, /Wednesday/);
    assert.match(description, /11:30 AM – 12:30 PM/);
    assert.match(lecture, /Mathematics/);
    assert.match(lecture, /11:00 AM – 12:00 PM/);
    assert.match(lecture, /Room 101/);
  });

  it("rejects the old loose payload with a friendly error instead of a TypeError", () => {
    assert.throws(
      () => validateResponse(ConflictCheckResponseSchema, OLD_RESPONSE, "conflict check"),
      /Data format error while loading conflict check/
    );
  });

  it("rejects unknown extra fields so drift is caught early", () => {
    const drifted = structuredClone(REAL_RESPONSE) as { conflicts: Record<string, unknown>[] };
    drifted.conflicts[0].details = {};
    assert.equal(ConflictCheckResponseSchema.safeParse(drifted).success, false);
  });

  it("empty conflicts list publishes directly", () => {
    const parsed = validateResponse(
      ConflictCheckResponseSchema,
      { has_conflicts: false, conflicts: [] },
      "conflict check"
    );
    assert.equal(nextPublishStep(parsed), "publish");
  });

  it("room and class overlaps render with null teacher/room", () => {
    const base = REAL_RESPONSE.conflicts[0];
    const room = formatConflictDetails({
      ...base,
      conflict_type: "room_overlap",
      teacher_name: null,
    });
    assert.equal(room.title, "Room Double-Booked");
    assert.match(room.description, /Room 101 is already booked by Class 10-B/);
    assert.match(room.lecture, /No teacher/);

    const cls = formatConflictDetails({
      ...base,
      conflict_type: "class_overlap",
      room: null,
      other_class_name: "Class 10-A",
      other_subject_name: "Physics",
    });
    assert.equal(cls.title, "Overlapping Lectures In This Class");
    assert.match(cls.description, /Physics/);
    assert.match(cls.lecture, /No room/);
  });

  it("reads conflicts from a 409 publish error and ignores malformed ones", () => {
    const err = {
      response: {
        status: 409,
        data: { detail: { code: "timetable_conflicts_detected", message: "x", conflicts: REAL_RESPONSE.conflicts } },
      },
    };
    assert.equal(conflictsFromPublishError(err)?.length, 1);
    const bad = { response: { status: 409, data: { detail: { conflicts: OLD_RESPONSE.conflicts } } } };
    assert.equal(conflictsFromPublishError(bad), null);
    assert.equal(conflictsFromPublishError(new Error("network")), null);
  });
});
