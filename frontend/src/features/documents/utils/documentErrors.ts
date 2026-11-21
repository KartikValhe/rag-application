import { parseApiError } from "../../../api/errorMessages.ts";

export interface DocumentErrorInfo {
  message: string;
  field?: "title" | "file" | "classes" | "query";
  retryAfterSeconds?: number;
  action?: "reload" | "login" | "none";
}

const DOCUMENT_ERROR_MESSAGES: Record<string, string> = {
  // Auth and generic
  permission_denied: "You do not have permission to do this.",
  document_manage_requires_all_classes:
    "You can view this material, but you teach this subject in only some of its classes, so you cannot change it. Ask an administrator.",
  not_found: "We could not find this material. It may have been removed.",
  missing_token: "Your session has ended. Please sign in again.",
  invalid_token: "Your session has ended. Please sign in again.",
  scope_resolution_failed: "We could not check your access. Please try again.",
  internal_error: "Something went wrong on our side. Please try again.",

  // Request guard
  duplicate_header: "The upload was not valid. Please try again.",
  invalid_content_length: "The upload was not valid. Please try again.",
  length_required:
    "The upload could not be sent. Please try again with a different browser or network.",
  file_too_large:
    "This file is larger than your organisation allows. Make it smaller or split it.",
  too_many_parts: "The upload was not valid. Upload one file at a time.",
  unsupported_media_type: "The upload was not valid. Please try again.",
  too_many_concurrent_uploads:
    "Several uploads are running from your network.",
  service_unavailable:
    "Uploads, search or storage are temporarily unavailable. Please try again in a few minutes.",

  // File checks (422)
  unsupported_format:
    "Documents accept PDF, Word, PowerPoint, text and Markdown files. Spreadsheets are not accepted.",
  disallowed_file_type:
    "Documents accept PDF, Word, PowerPoint, text and Markdown files. Spreadsheets are not accepted.",
  file_type_mismatch: "The file extension does not match the file content.",
  invalid_encoding: "The text file is not UTF-8. Save it as UTF-8 and upload again.",
  empty_file: "The file is empty.",
  too_many_pages: "This PDF has more pages than your organisation allows.",
  too_many_slides: "This presentation has more slides than your organisation allows.",
  xml_too_complex: "This file looks damaged or is not a valid Office file.",
  invalid_xml: "This file looks damaged or is not a valid Office file.",
  xml_entity_expansion_forbidden: "This file looks damaged or is not a valid Office file.",
  corrupt_archive: "This file looks damaged or is not a valid Office file.",
  zip_bomb_detected: "This file looks damaged or is not a valid Office file.",
  zip_member_too_large: "This file looks damaged or is not a valid Office file.",
  invalid_openxml_format: "This file looks damaged or is not a valid Office file.",
  macro_enabled_file: "Files with macros are not allowed. Save a copy without macros.",
  embedded_content: "Files with embedded or linked content are not allowed.",
  external_relationship: "Files with embedded or linked content are not allowed.",
  encrypted_file:
    "Password-protected files are not supported. Remove the password and upload again.",
  pdf_active_content:
    "PDFs with scripts, launch actions or rich media are not allowed.",
  pdf_embedded_file:
    "This PDF has another file attached inside it. Export or print it again without attachments, or upload a DOCX version instead.",
  pdf_too_complex: "This PDF is too large or complex to process.",

  // Upload/version other
  invalid_title: "The title must not be empty (and at most 200 characters).",
  too_many_classes: "A material can be linked to at most 50 classes.",
  quota_documents_exceeded:
    "Your organisation has reached its document limit. Ask an administrator.",
  quota_storage_exceeded:
    "Your organisation has reached its storage limit. Ask an administrator.",
  quota_pages_exceeded:
    "Your organisation has reached its page limit. Ask an administrator.",
  duplicate_version:
    "This exact file is already an active version of this material.",
  document_processing_in_progress:
    "A version of this material is still being processed or waiting to be published. Publish or delete it first.",
  rate_limit_exceeded: "You have uploaded too many files this hour.",
  tenant_rate_limit_exceeded:
    "Your organisation has reached its hourly upload limit.",

  // List and status
  invalid_cursor: "The list could not continue. Reload the page.",
  invalid_status: "Unknown status filter. Reload the page.",

  // Retry
  version_not_retryable:
    "This file failed for a reason a retry cannot fix. Upload a corrected file as a new version.",
  retry_limit_reached:
    "This file has been retried too many times. Upload it again as a new version.",
  retry_rate_limit_exceeded: "You have retried too often.",
  retry_version_rate_limit_exceeded: "You have retried too often.",

  // Publish
  version_not_ready:
    "This version is not ready to publish (it may still be processing, or it was already published or changed). Reload the page.",
  publish_conflict:
    "Another version was published at the same time. Reload and check the result.",

  // Delete
  version_not_deletable: "Only drafts and failed versions can be deleted.",

  // Test search
  invalid_query: "Type a question with at least one word.",
  query_too_long:
    "The question is too long. Shorten it (up to 500 characters).",
  test_search_rate_limit_exceeded: "You have searched too often.",
  search_unavailable:
    "Search is temporarily unavailable. Please try again in a few minutes.",
  search_timeout: "The search took too long. Please try again.",
  search_busy: "Search is busy right now. Please try again in a moment.",
};

const FILE_FIELDS = new Set([
  "empty_file",
  "unsupported_format",
  "disallowed_file_type",
  "file_type_mismatch",
  "invalid_encoding",
  "too_many_pages",
  "too_many_slides",
  "xml_too_complex",
  "invalid_xml",
  "xml_entity_expansion_forbidden",
  "corrupt_archive",
  "zip_bomb_detected",
  "zip_member_too_large",
  "invalid_openxml_format",
  "macro_enabled_file",
  "embedded_content",
  "external_relationship",
  "encrypted_file",
  "pdf_active_content",
  "pdf_embedded_file",
  "pdf_too_complex",
  "file_too_large",
  "too_many_parts",
]);

const VERSION_FAILURES: Record<string, { message: string; retryable: boolean }> = {
  processing_failed: {
    message: "We could not process this file after several tries. Try again, or upload it again.",
    retryable: true,
  },
  worker_crash: {
    message: "Processing stopped unexpectedly. Try again.",
    retryable: true,
  },
  job_time_limit_exceeded: {
    message: "Processing took too long. Upload a smaller or simpler file.",
    retryable: false,
  },
  scanned_pdf: {
    message: "The PDF looks scanned: it has almost no selectable text. Upload a text PDF or a DOCX.",
    retryable: false,
  },
  unreadable_text: {
    message: "The text uses a font that cannot be read (garbled characters). Export the file again with a standard font.",
    retryable: false,
  },
  no_text_found: {
    message: "No text was found in the document.",
    retryable: false,
  },
  text_too_large: {
    message: "The document contains more text than can be processed. Split it into smaller files.",
    retryable: false,
  },
  too_many_pages: {
    message: "The file has more pages or slides than your organisation allows.",
    retryable: false,
  },
  too_many_slides: {
    message: "The file has more pages or slides than your organisation allows.",
    retryable: false,
  },
  corrupt_pdf: {
    message: "The PDF could not be read. Export it again.",
    retryable: false,
  },
  password_protected_pdf: {
    message: "The PDF is password protected. Remove the password and upload it again.",
    retryable: false,
  },
  pdf_too_complex: {
    message: "The PDF is too large or complex to process.",
    retryable: false,
  },
  pdf_embedded_file: {
    message:
      "This PDF has another file attached inside it. Export or print it again without attachments, or upload a DOCX version instead.",
    retryable: false,
  },
  pdf_active_content: {
    message: "PDFs with scripts, launch actions or rich media are not allowed.",
    retryable: false,
  },
  parse_timeout: {
    message: "Processing the document took too long.",
    retryable: false,
  },
  parse_failed: {
    message: "The document could not be processed. Try exporting it again or upload a different format.",
    retryable: false,
  },
  parse_output_invalid: {
    message: "The document could not be processed. Try exporting it again or upload a different format.",
    retryable: false,
  },
  parse_output_too_large: {
    message: "The document could not be processed. Try exporting it again or upload a different format.",
    retryable: false,
  },
  xml_too_complex: {
    message: "The document could not be processed. Try exporting it again or upload a different format.",
    retryable: false,
  },
  quota_pages_exceeded: {
    message: "Your organisation has reached its page limit. Ask an administrator.",
    retryable: false,
  },
};

const UPLOAD_VALIDATION_CODES = new Set([
  "unsupported_format",
  "disallowed_file_type",
  "file_type_mismatch",
  "invalid_encoding",
  "empty_file",
  "invalid_xml",
  "xml_entity_expansion_forbidden",
  "corrupt_archive",
  "zip_bomb_detected",
  "zip_member_too_large",
  "invalid_openxml_format",
  "macro_enabled_file",
  "embedded_content",
  "external_relationship",
  "encrypted_file",
]);

export function getDocumentError(
  err: unknown,
  context?: "upload" | "publish" | "search" | "retry" | "list" | "delete"
): DocumentErrorInfo {
  const parsed = parseApiError(err);
  const code = parsed.code ?? "";
  const status = parsed.status ?? 500;

  // Extract raw detail object if present
  const rawDetail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  const reason =
    typeof rawDetail === "object" && rawDetail !== null
      ? (rawDetail as { reason?: string }).reason
      : undefined;

  let message = "";
  let field: DocumentErrorInfo["field"] = undefined;
  let action: DocumentErrorInfo["action"] = "none";

  // Context-specific messages
  if (context === "upload") {
    if (code === "not_found" || status === 404) {
      message = "We could not find the class or subject. Choose one of your own classes.";
    }
  } else if (context === "search") {
    if (code === "service_unavailable") {
      message = "Search is temporarily unavailable. Please try again in a few minutes.";
    } else if (code === "not_found" || status === 404) {
      message = "The class or subject is not one of yours.";
    }
  } else if (context === "retry") {
    if (code === "duplicate_version") {
      message = "The same file is already an active version.";
    } else if (code === "document_processing_in_progress") {
      message = "Another version of this material is being processed. Try again when it is done.";
    }
  }

  // Handle flags_acknowledgement_required specially
  if (code === "flags_acknowledgement_required") {
    if (reason === "not_scanned") {
      message = "This file has not been checked yet and must be reviewed.";
    } else {
      message = "Some passages were flagged and must be reviewed.";
    }
  }

  // Lookup in document messages table if message not yet chosen
  if (!message) {
    if (code && DOCUMENT_ERROR_MESSAGES[code]) {
      message = DOCUMENT_ERROR_MESSAGES[code];
    } else if (parsed.isNetwork) {
      message = "Network error. Please check your connection and try again.";
    } else if (parsed.isSchemaMismatch) {
      message = "Data format error. Please refresh or contact support.";
    } else if (status === 422) {
      message = "Some of the values are not valid. Please check them and try again.";
    } else if (status === 401) {
      message = "Your session has ended. Please sign in again.";
    } else if (status === 403) {
      message = "You do not have permission to do this.";
    } else if (status === 404) {
      message = "We could not find this material. It may have been removed.";
    } else if (status === 429) {
      message = "Too many requests.";
    } else if (status >= 500) {
      message = "Something went wrong on our side. Please try again.";
    } else {
      message = "Something went wrong. Please try again.";
    }
  }

  // Map fields
  if (code === "invalid_title") {
    field = "title";
  } else if (code === "too_many_classes") {
    field = "classes";
  } else if (code === "invalid_query" || code === "query_too_long") {
    field = "query";
  } else if (FILE_FIELDS.has(code)) {
    field = "file";
  }

  // Map actions
  if (code === "missing_token" || code === "invalid_token" || status === 401) {
    action = "login";
  } else if (
    code === "invalid_cursor" ||
    code === "invalid_status" ||
    code === "version_not_ready" ||
    code === "publish_conflict"
  ) {
    action = "reload";
  }

  // Append wait text ONLY for HTTP 429
  if (status === 429) {
    const s = parsed.retryAfterSeconds;
    let waitText = "Please try again in a few seconds.";
    if (s !== null && s !== undefined && s > 0) {
      if (s < 60) {
        waitText = `Please try again in ${s} second${s === 1 ? "" : "s"}.`;
      } else {
        const m = Math.ceil(s / 60);
        waitText = `Please try again in ${m} minute${m === 1 ? "" : "s"}.`;
      }
    }
    const cleanMsg = message.endsWith(".") ? message : `${message}.`;
    message = `${cleanMsg} ${waitText}`;
  }

  return {
    message,
    field,
    action,
    retryAfterSeconds: parsed.retryAfterSeconds ?? undefined,
  };
}

export function getVersionFailure(
  errorCode: string | null | undefined
): { message: string; retryable: boolean } {
  if (!errorCode) {
    return {
      message: "This file could not be processed.",
      retryable: false,
    };
  }

  if (VERSION_FAILURES[errorCode]) {
    return VERSION_FAILURES[errorCode];
  }

  if (UPLOAD_VALIDATION_CODES.has(errorCode) && DOCUMENT_ERROR_MESSAGES[errorCode]) {
    return {
      message: DOCUMENT_ERROR_MESSAGES[errorCode],
      retryable: false,
    };
  }

  return {
    message: "This file could not be processed.",
    retryable: false,
  };
}
