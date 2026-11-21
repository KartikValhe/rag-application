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

test("documentErrors: 429 wait time from Retry-After header and retry_after_seconds", () => {
  const errWithHeader = {
    response: {
      status: 429,
      headers: { "retry-after": "45" },
      data: {
        detail: {
          code: "rate_limit_exceeded",
        },
      },
    },
  };
  const resHeader = getDocumentError(errWithHeader);
  assert.ok(resHeader.message.includes("45 seconds"), `Expected wait text in message: "${resHeader.message}"`);
  assert.equal(resHeader.retryAfterSeconds, 45);

  const errWithPayload = {
    response: {
      status: 429,
      data: {
        detail: {
          code: "test_search_rate_limit_exceeded",
          retry_after_seconds: 30,
        },
      },
    },
  };
  const resPayload = getDocumentError(errWithPayload);
  assert.ok(resPayload.message.includes("30 seconds"), `Expected wait text in message: "${resPayload.message}"`);
  assert.equal(resPayload.retryAfterSeconds, 30);
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

test("documentErrors: getVersionFailure returns correct message and retryable flag", () => {
  const transient = getVersionFailure("processing_failed");
  assert.equal(transient.retryable, true);
  assert.ok(transient.message.includes("Try again"));

  const crash = getVersionFailure("worker_crash");
  assert.equal(crash.retryable, true);

  const scanned = getVersionFailure("scanned_pdf");
  assert.equal(scanned.retryable, false);
  assert.ok(!scanned.message.includes("CSV"), "scanned_pdf must not mention CSV");

  const unknown = getVersionFailure("some_new_failure_code");
  assert.equal(unknown.retryable, false);
  assert.equal(unknown.message, "This file could not be processed.");

  const embedded = getVersionFailure("pdf_embedded_file");
  assert.equal(
    embedded.message,
    "This PDF has another file attached inside it. Export or print it again without attachments, or upload a DOCX version instead."
  );
});
