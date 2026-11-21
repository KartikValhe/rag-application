import test from "node:test";
import assert from "node:assert/strict";
import { getDocumentError, getVersionFailure } from "../utils/documentErrors.ts";

const SECTION_D_CODES: Array<{ code: string; status: number; context?: "upload" | "publish" | "search" | "retry" | "list" | "delete" }> = [
  // Auth and generic
  { code: "permission_denied", status: 403 },
  { code: "document_manage_requires_all_classes", status: 403 },
  { code: "not_found", status: 404 },
  { code: "missing_token", status: 401 },
  { code: "invalid_token", status: 401 },
  { code: "scope_resolution_failed", status: 500 },
  { code: "internal_error", status: 500 },

  // Request guard
  { code: "duplicate_header", status: 400 },
  { code: "invalid_content_length", status: 400 },
  { code: "length_required", status: 411 },
  { code: "file_too_large", status: 413 },
  { code: "too_many_parts", status: 413 },
  { code: "unsupported_media_type", status: 415 },
  { code: "too_many_concurrent_uploads", status: 429 },
  { code: "service_unavailable", status: 503 },

  // File checks (422)
  { code: "unsupported_format", status: 422 },
  { code: "disallowed_file_type", status: 422 },
  { code: "file_type_mismatch", status: 422 },
  { code: "invalid_encoding", status: 422 },
  { code: "empty_file", status: 422 },
  { code: "too_many_pages", status: 422 },
  { code: "too_many_slides", status: 422 },
  { code: "xml_too_complex", status: 422 },
  { code: "invalid_xml", status: 422 },
  { code: "xml_entity_expansion_forbidden", status: 422 },
  { code: "corrupt_archive", status: 422 },
  { code: "zip_bomb_detected", status: 422 },
  { code: "zip_member_too_large", status: 422 },
  { code: "invalid_openxml_format", status: 422 },
  { code: "macro_enabled_file", status: 422 },
  { code: "embedded_content", status: 422 },
  { code: "external_relationship", status: 422 },
  { code: "encrypted_file", status: 422 },
  { code: "pdf_active_content", status: 422 },
  { code: "pdf_embedded_file", status: 422 },
  { code: "pdf_too_complex", status: 422 },

  // Upload/version other
  { code: "invalid_title", status: 422 },
  { code: "too_many_classes", status: 422 },
  { code: "quota_documents_exceeded", status: 422 },
  { code: "quota_storage_exceeded", status: 422 },
  { code: "quota_pages_exceeded", status: 422 },
  { code: "duplicate_version", status: 409 },
  { code: "document_processing_in_progress", status: 409 },
  { code: "rate_limit_exceeded", status: 429 },
  { code: "tenant_rate_limit_exceeded", status: 429 },

  // List and status
  { code: "invalid_cursor", status: 422 },
  { code: "invalid_status", status: 422 },

  // Retry
  { code: "version_not_retryable", status: 409 },
  { code: "retry_limit_reached", status: 409 },
  { code: "retry_rate_limit_exceeded", status: 429 },
  { code: "retry_version_rate_limit_exceeded", status: 429 },

  // Publish
  { code: "version_not_ready", status: 409 },
  { code: "publish_conflict", status: 409 },

  // Delete
  { code: "version_not_deletable", status: 409 },

  // Test search
  { code: "invalid_query", status: 422 },
  { code: "query_too_long", status: 422 },
  { code: "test_search_rate_limit_exceeded", status: 429 },
  { code: "search_unavailable", status: 503 },
  { code: "search_timeout", status: 503 },
  { code: "search_busy", status: 503 },
];

test("documentErrors: table-driven check over every section (d) code", () => {
  for (const item of SECTION_D_CODES) {
    const errorObj = {
      response: {
        status: item.status,
        data: {
          detail: {
            code: item.code,
            message: `Raw server message containing secret.pdf or internal path /var/log/doc-${item.code}`,
          },
        },
      },
    };

    const res = getDocumentError(errorObj, item.context);

    // Message must be non-empty
    assert.ok(res.message.length > 0, `Message for ${item.code} must not be empty`);

    // Must never contain raw error code
    assert.ok(!res.message.includes(item.code), `Message for ${item.code} must not contain raw code: "${res.message}"`);

    // Must never contain raw server message or file name
    assert.ok(!res.message.includes("secret.pdf"), `Message for ${item.code} must not contain file name: "${res.message}"`);
    assert.ok(!res.message.includes("/var/log"), `Message for ${item.code} must not contain internal path: "${res.message}"`);

    // Must never contain timetable words ("CSV", "2 MB", "timetable")
    assert.ok(!res.message.includes("CSV"), `Message for ${item.code} must not contain 'CSV': "${res.message}"`);
    assert.ok(!res.message.includes("2 MB"), `Message for ${item.code} must not contain '2 MB': "${res.message}"`);
    assert.ok(!res.message.toLowerCase().includes("timetable"), `Message for ${item.code} must not contain 'timetable': "${res.message}"`);
  }
});

test("documentErrors: Item 3 - 429 wait text formatting (seconds and minutes) and full stop", () => {
  const codes429 = [
    "too_many_concurrent_uploads",
    "rate_limit_exceeded",
    "tenant_rate_limit_exceeded",
    "retry_rate_limit_exceeded",
    "test_search_rate_limit_exceeded",
  ];

  for (const code of codes429) {
    const err = {
      response: {
        status: 429,
        headers: { "retry-after": "5" },
        data: { detail: { code } },
      },
    };
    const res = getDocumentError(err);
    assert.ok(
      res.message.includes("Please try again in 5 seconds."),
      `Code ${code} must include properly punctuated wait text: "${res.message}"`
    );
    assert.ok(
      !res.message.includes(".."),
      `Code ${code} must not have double full stops: "${res.message}"`
    );
  }

  // Check 59s -> 59 seconds
  const res59 = getDocumentError({
    response: {
      status: 429,
      headers: { "retry-after": "59" },
      data: { detail: { code: "rate_limit_exceeded" } },
    },
  });
  assert.ok(res59.message.includes("Please try again in 59 seconds."));

  // Check 60s -> 1 minute
  const res60 = getDocumentError({
    response: {
      status: 429,
      headers: { "retry-after": "60" },
      data: { detail: { code: "rate_limit_exceeded" } },
    },
  });
  assert.ok(res60.message.includes("Please try again in 1 minute."));

  // Check 61s -> 2 minutes (rounded up)
  const res61 = getDocumentError({
    response: {
      status: 429,
      data: { detail: { code: "rate_limit_exceeded", retry_after_seconds: 61 } },
    },
  });
  assert.ok(res61.message.includes("Please try again in 2 minutes."));

  // Check 3600s -> 60 minutes
  const res3600 = getDocumentError({
    response: {
      status: 429,
      data: { detail: { code: "rate_limit_exceeded", retry_after_seconds: 3600 } },
    },
  });
  assert.ok(res3600.message.includes("Please try again in 60 minutes."));

  // 503 with Retry-After header must NOT get wait text or second "try again" sentence
  const res503 = getDocumentError({
    response: {
      status: 503,
      headers: { "retry-after": "30" },
      data: { detail: { code: "service_unavailable" } },
    },
  });
  assert.equal(
    res503.message,
    "Uploads, search or storage are temporarily unavailable. Please try again in a few minutes."
  );
  assert.ok(!res503.message.includes("30 seconds"));
});

test("documentErrors: Item 4 - Context-specific wording overrides and quotas ending with 'Ask an administrator.'", () => {
  // (a) upload context, 404
  const resUpload404 = getDocumentError(
    { response: { status: 404, data: { detail: { code: "not_found" } } } },
    "upload"
  );
  assert.equal(
    resUpload404.message,
    "We could not find the class or subject. Choose one of your own classes."
  );

  // (b) retry context, duplicate_version
  const resRetryDup = getDocumentError(
    { response: { status: 409, data: { detail: { code: "duplicate_version" } } } },
    "retry"
  );
  assert.equal(resRetryDup.message, "The same file is already an active version.");

  // retry context, document_processing_in_progress
  const resRetryInProg = getDocumentError(
    { response: { status: 409, data: { detail: { code: "document_processing_in_progress" } } } },
    "retry"
  );
  assert.equal(resRetryInProg.message, "Another version of this material is being processed. Try again when it is done.");

  // search context, service_unavailable & 404
  const resSearch503 = getDocumentError(
    { response: { status: 503, data: { detail: { code: "service_unavailable" } } } },
    "search"
  );
  assert.equal(resSearch503.message, "Search is temporarily unavailable. Please try again in a few minutes.");

  const resSearch404 = getDocumentError(
    { response: { status: 404, data: { detail: { code: "not_found" } } } },
    "search"
  );
  assert.equal(resSearch404.message, "The class or subject is not one of yours.");

  // (c) quota codes all end with "Ask an administrator."
  const resQuotaDocs = getDocumentError({
    response: { status: 422, data: { detail: { code: "quota_documents_exceeded" } } },
  });
  assert.ok(resQuotaDocs.message.endsWith("Ask an administrator."));

  const resQuotaStorage = getDocumentError({
    response: { status: 422, data: { detail: { code: "quota_storage_exceeded" } } },
  });
  assert.ok(resQuotaStorage.message.endsWith("Ask an administrator."));

  const resQuotaPages = getDocumentError({
    response: { status: 422, data: { detail: { code: "quota_pages_exceeded" } } },
  });
  assert.ok(resQuotaPages.message.endsWith("Ask an administrator."));
});

test("documentErrors: flags_acknowledgement_required wording for flagged vs not_scanned", () => {
  const flaggedErr = {
    response: {
      status: 409,
      data: {
        detail: {
          code: "flags_acknowledgement_required",
          reason: "flagged",
        },
      },
    },
  };
  const flaggedRes = getDocumentError(flaggedErr);
  assert.ok(flaggedRes.message.includes("flagged"), `Expected flagged message: "${flaggedRes.message}"`);

  const notScannedErr = {
    response: {
      status: 409,
      data: {
        detail: {
          code: "flags_acknowledgement_required",
          reason: "not_scanned",
        },
      },
    },
  };
  const notScannedRes = getDocumentError(notScannedErr);
  assert.ok(notScannedRes.message.includes("not been checked yet"), `Expected not-scanned message: "${notScannedRes.message}"`);
});

test("documentErrors: Item 5 - getVersionFailure covers EVERY row of table and fallback for non-table codes", () => {
  const EXPECTED_VERSION_FAILURES: Record<string, { message: string; retryable: boolean }> = {
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
      message: "This PDF has another file attached inside it. Export or print it again without attachments, or upload a DOCX version instead.",
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
    unsupported_format: {
      message: "Documents accept PDF, Word, PowerPoint, text and Markdown files. Spreadsheets are not accepted.",
      retryable: false,
    },
    disallowed_file_type: {
      message: "Documents accept PDF, Word, PowerPoint, text and Markdown files. Spreadsheets are not accepted.",
      retryable: false,
    },
    file_type_mismatch: {
      message: "The file extension does not match the file content.",
      retryable: false,
    },
    invalid_encoding: {
      message: "The text file is not UTF-8. Save it as UTF-8 and upload again.",
      retryable: false,
    },
    empty_file: {
      message: "The file is empty.",
      retryable: false,
    },
    invalid_xml: {
      message: "This file looks damaged or is not a valid Office file.",
      retryable: false,
    },
    xml_entity_expansion_forbidden: {
      message: "This file looks damaged or is not a valid Office file.",
      retryable: false,
    },
    corrupt_archive: {
      message: "This file looks damaged or is not a valid Office file.",
      retryable: false,
    },
    zip_bomb_detected: {
      message: "This file looks damaged or is not a valid Office file.",
      retryable: false,
    },
    zip_member_too_large: {
      message: "This file looks damaged or is not a valid Office file.",
      retryable: false,
    },
    invalid_openxml_format: {
      message: "This file looks damaged or is not a valid Office file.",
      retryable: false,
    },
    macro_enabled_file: {
      message: "Files with macros are not allowed. Save a copy without macros.",
      retryable: false,
    },
    embedded_content: {
      message: "Files with embedded or linked content are not allowed.",
      retryable: false,
    },
    external_relationship: {
      message: "Files with embedded or linked content are not allowed.",
      retryable: false,
    },
    encrypted_file: {
      message: "Password-protected files are not supported. Remove the password and upload again.",
      retryable: false,
    },
  };

  // Test every single expected row
  for (const [code, expected] of Object.entries(EXPECTED_VERSION_FAILURES)) {
    const actual = getVersionFailure(code);
    assert.equal(actual.retryable, expected.retryable, `retryable mismatch for ${code}`);
    assert.equal(actual.message, expected.message, `message mismatch for ${code}`);
  }

  // Non-version failure codes MUST return fallback "This file could not be processed."
  const disallowedCodes = ["permission_denied", "not_found", "rate_limit_exceeded", "invalid_cursor", "unknown_xyz"];
  for (const code of disallowedCodes) {
    const actual = getVersionFailure(code);
    assert.equal(actual.retryable, false);
    assert.equal(actual.message, "This file could not be processed.", `Code ${code} must return fallback`);
  }
});
