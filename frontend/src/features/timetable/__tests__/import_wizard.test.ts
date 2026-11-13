import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  normalizeTime,
  toWizardRows,
  rowIssues,
  summarize,
  canSaveDraft,
  bulkMap,
  unknownNames,
  buildSaveEntries,
  buildSaveRequest,
  saveFlowReducer,
  initialSaveFlow,
  validateClientFile,
  sanitizeDisplayFilename,
  effectiveTimes,
  MAX_IMPORT_ENTRIES,
  type PreviewRowInput,
  type PeriodTimes,
  type SaveFlowState,
} from "../utils/importWizard.ts";

const ctx = {
  subjectIds: new Set(["s1", "s2"]),
  teacherIds: new Set(["t1", "t2"]),
};

const good: PreviewRowInput = {
  row_index: 0,
  day_of_week: 0,
  start_time: "09:00",
  end_time: "10:00",
  subject_id: "s1",
  teacher_id: "t1",
  raw_data: { subject: "MATH", teacher: "Turing" },
};

const periodRow: PreviewRowInput = {
  row_index: 1,
  day_of_week: 1,
  start_time: null,
  end_time: null,
  subject_id: "s1",
  teacher_id: null,
  period_label: "Period 1",
  requires_period_time: true,
  raw_data: { subject: "MATH" },
};

describe("normalizeTime", () => {
  it("accepts HH:MM, H:MM and HH:MM:SS", () => {
    assert.equal(normalizeTime("9:05"), "09:05");
    assert.equal(normalizeTime("09:00:00"), "09:00");
    assert.equal(normalizeTime("23:59"), "23:59");
  });
  it("rejects bad values", () => {
    assert.equal(normalizeTime("24:00"), null);
    assert.equal(normalizeTime("9"), null);
    assert.equal(normalizeTime(""), null);
    assert.equal(normalizeTime(null), null);
  });
});

describe("period time handling", () => {
  const rows = toWizardRows([periodRow, { ...periodRow, row_index: 2 }]);

  it("blocks rows until the period times are entered", () => {
    assert.deepEqual(rowIssues(rows[0], {}, ctx), ["period_time"]);
    assert.equal(canSaveDraft(rows, {}, ctx), false);
  });
  it("rejects incomplete or reversed period times", () => {
    const bad: PeriodTimes = { "Period 1": { start: "10:00", end: "09:00" } };
    assert.deepEqual(rowIssues(rows[0], bad, ctx), ["period_time"]);
    const half: PeriodTimes = { "Period 1": { start: "09:00", end: "" } };
    assert.deepEqual(rowIssues(rows[0], half, ctx), ["period_time"]);
  });
  it("one entry fills every row with that label", () => {
    const ok: PeriodTimes = { "Period 1": { start: "9:00", end: "09:45" } };
    assert.equal(canSaveDraft(rows, ok, ctx), true);
    assert.deepEqual(effectiveTimes(rows[1], ok), { start: "09:00", end: "09:45" });
    const entries = buildSaveEntries(rows, ok);
    assert.equal(entries.length, 2);
    assert.equal(entries[0].start_time, "09:00");
    assert.equal(entries[0].end_time, "09:45");
  });
});

describe("save-enable rules", () => {
  it("is enabled for fully valid rows", () => {
    assert.equal(canSaveDraft(toWizardRows([good]), {}, ctx), true);
  });
  it("is disabled with no rows or too many rows", () => {
    assert.equal(canSaveDraft([], {}, ctx), false);
    const many = toWizardRows(
      Array.from({ length: MAX_IMPORT_ENTRIES + 1 }, (_, i) => ({ ...good, row_index: i }))
    );
    assert.equal(canSaveDraft(many, {}, ctx), false);
  });
  it("flags missing weekday, bad time order and unknown subject", () => {
    const [a, b, c] = toWizardRows([
      { ...good, day_of_week: null },
      { ...good, start_time: "10:00", end_time: "09:00" },
      { ...good, subject_id: null },
    ]);
    assert.deepEqual(rowIssues(a, {}, ctx), ["weekday"]);
    assert.deepEqual(rowIssues(b, {}, ctx), ["time_order"]);
    assert.deepEqual(rowIssues(c, {}, ctx), ["subject"]);
  });
  it("blocks an unmatched teacher until mapped or cleared", () => {
    const [r] = toWizardRows([{ ...good, teacher_id: null }]);
    assert.deepEqual(rowIssues(r, {}, ctx), ["teacher"]);
    assert.deepEqual(rowIssues({ ...r, teacherTouched: true }, {}, ctx), []);
  });
  it("rejects a subject or teacher id outside this class", () => {
    const [r] = toWizardRows([{ ...good, subject_id: "zzz", teacher_id: "zzz" }]);
    assert.deepEqual(rowIssues(r, {}, ctx).sort(), ["subject", "teacher"]);
  });
  it("summary counts valid and needs-attention rows", () => {
    const rows = toWizardRows([good, { ...good, subject_id: null }, periodRow]);
    assert.deepEqual(summarize(rows, {}, ctx), { total: 3, valid: 1, needsAttention: 2 });
  });
  it("deleting the bad rows re-enables saving", () => {
    const rows = toWizardRows([good, { ...good, subject_id: null }]);
    assert.equal(canSaveDraft(rows, {}, ctx), false);
    assert.equal(canSaveDraft(rows.filter((r) => r.subject_id), {}, ctx), true);
  });
});

describe("bulk mapping", () => {
  const rows = toWizardRows([
    { ...good, subject_id: null, raw_data: { subject: "Maths", teacher: "Turing" } },
    { ...good, subject_id: null, raw_data: { subject: " maths ", teacher: "Turing" } },
    { ...good, subject_id: null, raw_data: { subject: "Physics", teacher: "Turing" } },
    { ...good, raw_data: { subject: "Maths", teacher: "Turing" } }, // already resolved
  ]);

  it("lists unknown names grouped case-insensitively", () => {
    const u = unknownNames(rows, "subject", ctx);
    assert.equal(u.length, 2);
    assert.equal(u.find((x) => x.name === "Maths")?.count, 2);
  });
  it("maps every matching unresolved row and leaves others alone", () => {
    const out = bulkMap(rows, "subject", "MATHS", "s2", ctx);
    assert.deepEqual(out.map((r) => r.subject_id), ["s2", "s2", null, "s1"]);
  });
  it("ignores targets that are not this class's options", () => {
    assert.equal(bulkMap(rows, "subject", "Maths", "other", ctx), rows);
  });
  it("maps teachers and marks them chosen", () => {
    const t = toWizardRows([
      { ...good, teacher_id: null, raw_data: { subject: "M", teacher: "Alan" } },
    ]);
    const out = bulkMap(t, "teacher", "alan", "t2", ctx);
    assert.equal(out[0].teacher_id, "t2");
    assert.equal(out[0].teacherTouched, true);
    assert.equal(unknownNames(out, "teacher", ctx).length, 0);
  });
});

describe("replace-draft flow (409)", () => {
  const run = (...steps: Parameters<typeof saveFlowReducer>[1][]): SaveFlowState =>
    steps.reduce(saveFlowReducer, initialSaveFlow);

  it("409 moves to confirm with old and new counts", () => {
    const s = run(
      { type: "save" },
      { type: "conflict", oldCount: 5, newCount: 12 }
    );
    assert.deepEqual(s, { phase: "confirm_replace", oldCount: 5, newCount: 12 });
  });
  it("confirming re-saves with replace and then succeeds", () => {
    const s = run(
      { type: "save" },
      { type: "conflict", oldCount: 5, newCount: 12 },
      { type: "confirm_replace" }
    );
    assert.deepEqual(s, { phase: "saving", replace: true });
    assert.deepEqual(saveFlowReducer(s, { type: "success" }), { phase: "saved" });
  });
  it("cancelling keeps the user in review", () => {
    const s = run(
      { type: "save" },
      { type: "conflict", oldCount: null, newCount: 3 },
      { type: "cancel_replace" }
    );
    assert.deepEqual(s, { phase: "idle" });
  });
  it("a first save never replaces, and errors allow retry", () => {
    assert.deepEqual(run({ type: "save" }), { phase: "saving", replace: false });
    const e = run({ type: "save" }, { type: "failure", message: "x" });
    assert.equal(e.phase, "error");
    assert.deepEqual(saveFlowReducer(e, { type: "save" }), { phase: "saving", replace: false });
  });
  it("ignores confirm when no conflict happened", () => {
    assert.deepEqual(run({ type: "confirm_replace" }), { phase: "idle" });
  });
});

describe("file checks and display filename", () => {
  it("checks extension and size", () => {
    assert.equal(validateClientFile("a.csv", 100), null);
    assert.equal(validateClientFile("a.PDF", 100), null);
    assert.ok(validateClientFile("a.exe", 100));
    assert.ok(validateClientFile("a.csv", 3 * 1024 * 1024));
    assert.ok(validateClientFile("a.csv", 0));
  });
  it("sanitizes and truncates names", () => {
    assert.equal(sanitizeDisplayFilename("C:\\x\\y\\plan.csv"), "plan.csv");
    assert.equal(sanitizeDisplayFilename("<img onerror=x>.csv"), "img onerror=x.csv");
    assert.equal(sanitizeDisplayFilename("a\u202eb.csv"), "ab.csv");
    const long = sanitizeDisplayFilename("x".repeat(200) + ".xlsx", 30);
    assert.ok(long.length <= 30 && long.endsWith(".xlsx"));
  });
});

describe("save request body", () => {
  it("uses the backend field replace_confirmed and never replace_existing", () => {
    const body = buildSaveRequest([], true);
    assert.deepEqual(Object.keys(body).sort(), ["entries", "replace_confirmed"]);
    assert.equal(body.replace_confirmed, true);
    assert.equal(buildSaveRequest([], false).replace_confirmed, false);
  });
});
