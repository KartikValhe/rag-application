/**
 * Maps an API error to a friendly, fixed message. Raw server text is never shown.
 * Status and code values follow docs/api/phase4.md.
 */

export const SCHEMA_MISMATCH_NAME = "SchemaMismatchError";

const FALLBACK = "Something went wrong. Please try again.";

const CODE_MESSAGES: Record<string, string> = {
  file_too_large: "This file is too large. The maximum size is 2 MB.",
  too_many_parts: "The upload is not valid. Please upload a single file.",
  parse_timeout: "Reading this file took too long. Try a smaller or simpler file.",
  empty_file: "The file is empty. Please choose a file with content.",
  disallowed_file_type: "This file type is not allowed. Use CSV, XLSX, DOCX or a text PDF.",
  invalid_openxml_format: "This file looks damaged or is not a valid Office file.",
  file_type_mismatch: "The file extension does not match its content.",
  macro_enabled_file: "Files with macros are not allowed. Save a copy without macros.",
  embedded_content: "Files with embedded content are not allowed.",
  external_relationship: "Files that link to external content are not allowed.",
  invalid_xml: "This file looks damaged and could not be read.",
  xml_entity_expansion_forbidden: "This file contains content that is not allowed.",
  corrupt_archive: "This file looks damaged and could not be read.",
  encrypted_file: "Password-protected files are not supported. Remove the password and retry.",
  zip_bomb_detected: "This file is not valid and could not be read.",
  zip_member_too_large: "This file contains content that is too large.",
  pdf_active_content: "PDFs with scripts or attachments are not allowed.",
  pdf_too_complex: "This PDF is too large or complex. Try a simpler file.",
  scanned_pdf: "This PDF has no readable text (it looks scanned). Use a text PDF or a CSV.",
  password_protected_pdf: "Password-protected PDFs are not supported.",
  corrupt_pdf: "This PDF looks damaged and could not be read.",
  nul_byte_in_csv: "The CSV file contains invalid characters.",
  invalid_encoding: "The file text encoding is not supported. Save it as UTF-8.",
  empty_workbook: "The spreadsheet has no data.",
  unsupported_format: "This file format cannot be imported. Use CSV, XLSX, DOCX or a text PDF.",
  parse_failed: "We could not read this file. Please check it and try again.",
  parse_output_too_large: "This file contains too much data to import.",
  worker_crash: "We could not read this file. Please try again or use a different file.",
  rate_limit_exceeded: "You have made too many import attempts.",
  tenant_rate_limit_exceeded: "Your organization has made too many import attempts.",
  too_many_concurrent_parses: "The importer is busy right now. Please try again shortly.",
  tenant_concurrent_limit_exceeded:
    "Your organization already has imports running. Please wait and try again.",
  service_unavailable: "The service is temporarily unavailable. Please try again later.",
  entries_cap_exceeded: "A timetable can have at most 200 sessions.",
  subject_not_linked: "That subject is not linked to this class.",
  teacher_not_assigned: "That teacher is not assigned to this subject in this class.",
  invalid_time_range: "End time must be later than start time.",
  invalid_timezone: "Please choose a valid timezone.",
  no_draft:
    "There is no draft timetable for this class. Close this window and choose Start editing first.",
  no_published_timetable: "This class has no published timetable.",
  draft_already_exists: "A draft already exists for this class.",
  timetable_draft_exists:
    "This class already has a draft. Publish or delete the draft first, then try again.",
  not_published: "This class has no published timetable.",
  timetable_conflicts_detected: "The timetable has scheduling conflicts.",
  permission_denied: "You do not have permission to do this.",
};

interface ErrorShape {
  name?: string;
  response?: {
    status?: number;
    headers?: Record<string, unknown>;
    data?: {
      code?: unknown;
      detail?: unknown;
    };
  };
  code?: string;
  request?: unknown;
}

export interface ParsedApiError {
  status: number | null;
  code: string | null;
  retryAfterSeconds: number | null;
  isNetwork: boolean;
  isSchemaMismatch: boolean;
}

function toSeconds(value: unknown): number | null {
  const n = typeof value === "string" ? Number(value) : value;
  return typeof n === "number" && Number.isFinite(n) && n > 0 ? Math.ceil(n) : null;
}

export function parseApiError(err: unknown): ParsedApiError {
  const e = (err ?? {}) as ErrorShape;
  const res = e.response;
  const rawDetail = res?.data?.detail;
  const detail =
    typeof rawDetail === "object" && rawDetail !== null
      ? (rawDetail as { code?: unknown; retry_after_seconds?: unknown })
      : undefined;
  const rawCode = detail?.code ?? res?.data?.code;
  const headers = res?.headers ?? {};
  const header = headers["retry-after"] ?? headers["Retry-After"];
  return {
    status: typeof res?.status === "number" ? res.status : null,
    code: typeof rawCode === "string" ? rawCode : null,
    retryAfterSeconds: toSeconds(header) ?? toSeconds(detail?.retry_after_seconds),
    isNetwork: !res && (e.code === "ERR_NETWORK" || e.code === "ECONNABORTED" || e.request !== undefined),
    isSchemaMismatch: e.name === SCHEMA_MISMATCH_NAME,
  };
}

function waitText(seconds: number | null): string {
  if (seconds === null) return " Please wait a moment and try again.";
  if (seconds < 60) return ` Please try again in ${seconds} second${seconds === 1 ? "" : "s"}.`;
  const m = Math.ceil(seconds / 60);
  return ` Please try again in ${m} minute${m === 1 ? "" : "s"}.`;
}

export function getFriendlyError(err: unknown, fallback: string = FALLBACK): string {
  const p = parseApiError(err);

  if (p.isSchemaMismatch) {
    return "We received data in an unexpected format. Please refresh the page or contact support.";
  }
  if (p.isNetwork) {
    return "Cannot reach the server. Check your connection and try again.";
  }
  if (p.status === null) return fallback;

  if (p.status === 429) {
    const base = (p.code && CODE_MESSAGES[p.code]) || "Too many requests.";
    return base + waitText(p.retryAfterSeconds);
  }
  const byCode = p.code ? CODE_MESSAGES[p.code] : undefined;
  if (byCode) return byCode;

  switch (p.status) {
    case 408:
      return CODE_MESSAGES["parse_timeout"] as string;
    case 413:
      return CODE_MESSAGES["file_too_large"] as string;
    case 422:
      return "Some of the values are not valid. Please check them and try again.";
    case 503:
      return CODE_MESSAGES["service_unavailable"] as string;
    case 403:
      return "You do not have permission to do this.";
    case 404:
      return "We could not find what you were looking for.";
    case 409:
      return "This change conflicts with the current data. Please refresh and try again.";
    default:
      return fallback;
  }
}

/** True for a 403 response. */
export function isForbidden(err: unknown): boolean {
  return parseApiError(err).status === 403;
}
