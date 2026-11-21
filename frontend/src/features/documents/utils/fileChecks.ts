/**
 * Pure client-side file validation and filename sanitization for Course Materials.
 */

export const ALLOWED_EXTENSIONS = [".pdf", ".docx", ".pptx", ".txt", ".md"] as const;
export const SPREADSHEET_EXTENSIONS = [".xlsx", ".xls", ".csv", ".ods"] as const;
export const DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024; // 50 MB platform ceiling

export const UNSAFE_NAME_CHARS = /[\u0000-\u001f\u007f\u200e\u200f\u202a-\u202e\u2066-\u2069]/g;

/**
 * Validates a file before upload.
 * Returns null if valid, or a plain user-facing error message if invalid.
 */
export function validateDocumentFile(
  file: { name: string; size: number },
  maxBytes: number = DEFAULT_MAX_FILE_BYTES,
  acceptedExtensions?: readonly string[]
): string | null {
  const name = file.name.trim();
  const dotIndex = name.lastIndexOf(".");
  const ext = dotIndex >= 0 ? name.slice(dotIndex).toLowerCase() : "";

  if (SPREADSHEET_EXTENSIONS.includes(ext as (typeof SPREADSHEET_EXTENSIONS)[number])) {
    return "Spreadsheets are not accepted here; export it as PDF or DOCX";
  }

  const allowed: readonly string[] =
    acceptedExtensions && acceptedExtensions.length > 0
      ? acceptedExtensions.map((e) => (e.startsWith(".") ? e.toLowerCase() : `.${e.toLowerCase()}`))
      : ALLOWED_EXTENSIONS;

  if (!allowed.includes(ext)) {
    return "Documents accept PDF, Word, PowerPoint, text and Markdown files. Spreadsheets and CSV are not accepted.";
  }

  if (file.size <= 0) {
    return "The file is empty.";
  }

  if (file.size > maxBytes) {
    return "This file is larger than your organisation allows. Make it smaller or split it.";
  }

  return null;
}

/**
 * Sanitizes a filename for safe visual display:
 * - Strips directory paths (Windows & POSIX)
 * - Removes control and bidi characters
 * - Truncates if too long, preserving extension
 */
export function sanitizeDisplayFilename(name: string, max = 50): string {
  const base = name.split(/[\\/]/).pop() ?? "";
  const cleaned = base.replace(UNSAFE_NAME_CHARS, "").trim();
  if (!cleaned) return "uploaded file";
  if (cleaned.length <= max) return cleaned;
  const dot = cleaned.lastIndexOf(".");
  const ext = dot > 0 && cleaned.length - dot <= 6 ? cleaned.slice(dot) : "";
  return cleaned.slice(0, Math.max(1, max - ext.length - 1)) + "…" + ext;
}
