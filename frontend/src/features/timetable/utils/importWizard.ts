/**
 * Pure logic for the timetable import wizard (no React, no network).
 * Kept free of path aliases so node --test can run it directly.
 */

export const MAX_IMPORT_BYTES = 2 * 1024 * 1024;
export const MAX_IMPORT_ENTRIES = 200;
export const ALLOWED_EXTENSIONS = ["csv", "xlsx", "docx", "pdf"] as const;
export const IMPORT_TIMEOUT_MS = 60_000;

export interface PreviewRowInput {
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
}

export interface WizardRow {
  key: string;
  day_of_week: number | null;
  start_time: string;
  end_time: string;
  subject_id: string | null;
  teacher_id: string | null;
  /** True once the user picked a teacher (or "no teacher") on purpose. */
  teacherTouched: boolean;
  room: string;
  note: string;
  rawSubject: string;
  rawTeacher: string;
  periodLabel: string | null;
  needsPeriodTime: boolean;
}

export interface PeriodTimes {
  [label: string]: { start: string; end: string };
}

export interface ValidationContext {
  subjectIds: ReadonlySet<string>;
  teacherIds: ReadonlySet<string>;
}

export type IssueCode =
  | "weekday"
  | "time"
  | "time_order"
  | "period_time"
  | "subject"
  | "teacher";

// ---------- file checks (feedback only; the server is the real gate) ----------

export function validateClientFile(name: string, size: number): string | null {
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot + 1).toLowerCase() : "";
  if (!(ALLOWED_EXTENSIONS as readonly string[]).includes(ext)) {
    return "Please choose a CSV, XLSX, DOCX or PDF file.";
  }
  if (size <= 0) return "The file is empty. Please choose a file with content.";
  if (size > MAX_IMPORT_BYTES) return "This file is too large. The maximum size is 2 MB.";
  return null;
}

const UNSAFE_NAME_CHARS = /[\u0000-\u001f\u007f‎‏‪-‮⁦-⁩]/g;

/** Display-only filename: no path, no control/bidi characters, no markup chars, truncated. */
export function sanitizeDisplayFilename(name: string, max = 40): string {
  const base = name.split(/[\\/]/).pop() ?? "";
  const cleaned = base.replace(UNSAFE_NAME_CHARS, "").replace(/[<>"'`]/g, "").trim();
  if (!cleaned) return "uploaded file";
  if (cleaned.length <= max) return cleaned;
  const dot = cleaned.lastIndexOf(".");
  const ext = dot > 0 && cleaned.length - dot <= 6 ? cleaned.slice(dot) : "";
  return cleaned.slice(0, Math.max(1, max - ext.length - 1)) + "…" + ext;
}

export function truncateText(value: string | null | undefined, max = 80): string {
  const v = value ?? "";
  return v.length > max ? v.slice(0, max - 1) + "…" : v;
}

// ---------- times ----------

/** Returns "HH:MM" for "9:00", "09:00" or "09:00:00"; null when not a valid time. */
export function normalizeTime(value: string | null | undefined): string | null {
  if (!value) return null;
  const m = /^(\d{1,2}):(\d{2})(?::(\d{2}))?$/.exec(value.trim());
  if (!m) return null;
  const h = Number(m[1]);
  const min = Number(m[2]);
  if (h > 23 || min > 59) return null;
  return `${String(h).padStart(2, "0")}:${String(min).padStart(2, "0")}`;
}

export function isPeriodTimeComplete(t: { start: string; end: string } | undefined): boolean {
  if (!t) return false;
  const s = normalizeTime(t.start);
  const e = normalizeTime(t.end);
  return s !== null && e !== null && e > s;
}

/** Times a row will be saved with: its own, or its period's entered times. */
export function effectiveTimes(
  row: WizardRow,
  periodTimes: PeriodTimes
): { start: string | null; end: string | null } {
  if (row.needsPeriodTime && row.periodLabel) {
    const p = periodTimes[row.periodLabel];
    return { start: normalizeTime(p?.start), end: normalizeTime(p?.end) };
  }
  return { start: normalizeTime(row.start_time), end: normalizeTime(row.end_time) };
}

// ---------- building rows from the preview ----------

export function toWizardRows(rows: PreviewRowInput[]): WizardRow[] {
  return rows.map((r, i) => {
    const raw = r.raw_data ?? {};
    const needs = Boolean(r.requires_period_time) && Boolean(r.period_label);
    return {
      key: `r${i}`,
      day_of_week: typeof r.day_of_week === "number" ? r.day_of_week : null,
      start_time: normalizeTime(r.start_time) ?? "",
      end_time: normalizeTime(r.end_time) ?? "",
      subject_id: r.subject_id ?? null,
      teacher_id: r.teacher_id ?? null,
      teacherTouched: false,
      room: r.room ?? "",
      note: r.note ?? "",
      rawSubject: raw.subject ?? "",
      rawTeacher: raw.teacher ?? "",
      periodLabel: r.period_label ?? null,
      needsPeriodTime: needs,
    };
  });
}

// ---------- validation ----------

export function rowIssues(
  row: WizardRow,
  periodTimes: PeriodTimes,
  ctx: ValidationContext
): IssueCode[] {
  const issues: IssueCode[] = [];
  if (row.day_of_week === null || row.day_of_week < 0 || row.day_of_week > 6) {
    issues.push("weekday");
  }
  if (row.needsPeriodTime) {
    const p = row.periodLabel ? periodTimes[row.periodLabel] : undefined;
    if (!isPeriodTimeComplete(p)) issues.push("period_time");
  } else {
    const { start, end } = effectiveTimes(row, periodTimes);
    if (!start || !end) issues.push("time");
    else if (end <= start) issues.push("time_order");
  }
  if (!row.subject_id || !ctx.subjectIds.has(row.subject_id)) issues.push("subject");
  if (row.teacher_id) {
    if (!ctx.teacherIds.has(row.teacher_id)) issues.push("teacher");
  } else if (row.rawTeacher.trim() && !row.teacherTouched) {
    // A teacher name was in the file but could not be matched: map it or clear it.
    issues.push("teacher");
  }
  return issues;
}

export interface ImportSummary {
  total: number;
  valid: number;
  needsAttention: number;
}

export function summarize(
  rows: WizardRow[],
  periodTimes: PeriodTimes,
  ctx: ValidationContext
): ImportSummary {
  let valid = 0;
  for (const r of rows) if (rowIssues(r, periodTimes, ctx).length === 0) valid += 1;
  return { total: rows.length, valid, needsAttention: rows.length - valid };
}

export function canSaveDraft(
  rows: WizardRow[],
  periodTimes: PeriodTimes,
  ctx: ValidationContext
): boolean {
  if (rows.length === 0 || rows.length > MAX_IMPORT_ENTRIES) return false;
  return summarize(rows, periodTimes, ctx).needsAttention === 0;
}

// ---------- bulk mapping ----------

export type MapKind = "subject" | "teacher";

export interface UnknownName {
  name: string;
  count: number;
}

const norm = (s: string) => s.trim().toLowerCase();

export function unknownNames(
  rows: WizardRow[],
  kind: MapKind,
  ctx: ValidationContext
): UnknownName[] {
  const counts = new Map<string, UnknownName>();
  for (const r of rows) {
    const unresolved =
      kind === "subject"
        ? !r.subject_id || !ctx.subjectIds.has(r.subject_id)
        : !r.teacher_id && !r.teacherTouched;
    const raw = kind === "subject" ? r.rawSubject : r.rawTeacher;
    if (!unresolved || !raw.trim()) continue;
    const k = norm(raw);
    const cur = counts.get(k);
    if (cur) cur.count += 1;
    else counts.set(k, { name: raw.trim(), count: 1 });
  }
  return [...counts.values()];
}

/** Applies one mapping to every still-unresolved row that has the same raw name. */
export function bulkMap(
  rows: WizardRow[],
  kind: MapKind,
  rawName: string,
  targetId: string,
  ctx: ValidationContext
): WizardRow[] {
  const key = norm(rawName);
  if (!key || !targetId) return rows;
  const allowed = kind === "subject" ? ctx.subjectIds : ctx.teacherIds;
  if (!allowed.has(targetId)) return rows;
  return rows.map((r) => {
    if (kind === "subject") {
      const unresolved = !r.subject_id || !ctx.subjectIds.has(r.subject_id);
      return unresolved && norm(r.rawSubject) === key ? { ...r, subject_id: targetId } : r;
    }
    const unresolved = !r.teacher_id && !r.teacherTouched;
    return unresolved && norm(r.rawTeacher) === key
      ? { ...r, teacher_id: targetId, teacherTouched: true }
      : r;
  });
}

// ---------- save payload ----------

export interface SaveEntryPayload {
  day_of_week: number;
  start_time: string;
  end_time: string;
  subject_id: string;
  teacher_id: string | null;
  room: string | null;
  note: string | null;
}

/** Request body for save-imported-draft. The backend field is replace_confirmed. */
export function buildSaveRequest(
  entries: SaveEntryPayload[],
  replace: boolean
): { replace_confirmed: boolean; entries: SaveEntryPayload[] } {
  return { replace_confirmed: replace, entries };
}

export function buildSaveEntries(rows: WizardRow[], periodTimes: PeriodTimes): SaveEntryPayload[] {
  const out: SaveEntryPayload[] = [];
  for (const r of rows) {
    const { start, end } = effectiveTimes(r, periodTimes);
    if (r.day_of_week === null || !start || !end || !r.subject_id) continue;
    out.push({
      day_of_week: r.day_of_week,
      start_time: start,
      end_time: end,
      subject_id: r.subject_id,
      teacher_id: r.teacher_id,
      room: r.room.trim() || null,
      note: r.note.trim() || null,
    });
  }
  return out;
}

// ---------- save / replace-draft flow ----------

export type SaveFlowState =
  | { phase: "idle" }
  | { phase: "saving"; replace: boolean }
  | { phase: "confirm_replace"; oldCount: number | null; newCount: number }
  | { phase: "saved" }
  | { phase: "error"; message: string };

export type SaveFlowAction =
  | { type: "save" }
  | { type: "conflict"; oldCount: number | null; newCount: number }
  | { type: "confirm_replace" }
  | { type: "cancel_replace" }
  | { type: "success" }
  | { type: "failure"; message: string }
  | { type: "reset" };

export const initialSaveFlow: SaveFlowState = { phase: "idle" };

export function saveFlowReducer(state: SaveFlowState, action: SaveFlowAction): SaveFlowState {
  switch (action.type) {
    case "save":
      return state.phase === "idle" || state.phase === "error"
        ? { phase: "saving", replace: false }
        : state;
    case "conflict":
      return state.phase === "saving"
        ? { phase: "confirm_replace", oldCount: action.oldCount, newCount: action.newCount }
        : state;
    case "confirm_replace":
      return state.phase === "confirm_replace" ? { phase: "saving", replace: true } : state;
    case "cancel_replace":
      return state.phase === "confirm_replace" ? { phase: "idle" } : state;
    case "success":
      return state.phase === "saving" ? { phase: "saved" } : state;
    case "failure":
      return state.phase === "saving" ? { phase: "error", message: action.message } : state;
    case "reset":
      return initialSaveFlow;
    default:
      return state;
  }
}

/** True when the error is the axios client-side timeout (no HTTP response). */
export function isClientTimeout(err: unknown): boolean {
  const e = (err ?? {}) as { code?: string; response?: unknown };
  return !e.response && e.code === "ECONNABORTED";
}
