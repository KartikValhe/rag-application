import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { getFriendlyError, parseApiError } from "../../../api/errorMessages.ts";

const http = (
  status: number,
  code?: string,
  extra: Record<string, unknown> = {},
  headers: Record<string, unknown> = {}
) => ({
  response: {
    status,
    headers,
    data: { detail: { code, message: "RAW SERVER TEXT /etc/passwd", ...extra } },
  },
});

describe("getFriendlyError", () => {
  const codes: Array<[number, string]> = [
    [413, "file_too_large"],
    [413, "too_many_parts"],
    [408, "parse_timeout"],
    [422, "empty_file"],
    [422, "disallowed_file_type"],
    [422, "invalid_openxml_format"],
    [422, "file_type_mismatch"],
    [422, "macro_enabled_file"],
    [422, "embedded_content"],
    [422, "external_relationship"],
    [422, "invalid_xml"],
    [422, "xml_entity_expansion_forbidden"],
    [422, "corrupt_archive"],
    [422, "encrypted_file"],
    [422, "zip_bomb_detected"],
    [422, "zip_member_too_large"],
    [422, "pdf_active_content"],
    [422, "pdf_too_complex"],
    [422, "scanned_pdf"],
    [422, "password_protected_pdf"],
    [422, "corrupt_pdf"],
    [422, "nul_byte_in_csv"],
    [422, "invalid_encoding"],
    [422, "empty_workbook"],
    [422, "unsupported_format"],
    [422, "parse_failed"],
    [422, "parse_output_too_large"],
    [422, "worker_crash"],
    [429, "rate_limit_exceeded"],
    [429, "tenant_rate_limit_exceeded"],
    [429, "too_many_concurrent_parses"],
    [429, "tenant_concurrent_limit_exceeded"],
    [503, "service_unavailable"],
    [422, "entries_cap_exceeded"],
    [422, "subject_not_linked"],
    [422, "teacher_not_assigned"],
    [422, "invalid_time_range"],
    [422, "invalid_timezone"],
    [403, "permission_denied"],
    [404, "not_found"],
    [409, "timetable_draft_exists"],
    [404, "not_published"],
  ];

  for (const [status, code] of codes) {
    it(`maps ${status} ${code} to a fixed friendly message`, () => {
      const msg = getFriendlyError(http(status, code));
      assert.ok(msg.length > 10);
      assert.ok(!msg.includes("RAW SERVER TEXT"));
      assert.ok(!msg.includes(code));
    });
  }

  it("tells the user how to resolve a 409 timetable_draft_exists", () => {
    const msg = getFriendlyError(http(409, "timetable_draft_exists"));
    assert.match(msg, /publish or delete the draft/i);
  });

  it("gives distinct messages for distinct 422 codes", () => {
    assert.notEqual(
      getFriendlyError(http(422, "scanned_pdf")),
      getFriendlyError(http(422, "macro_enabled_file"))
    );
  });

  it("falls back per status when the code is unknown", () => {
    assert.match(getFriendlyError(http(413, "x")), /too large/i);
    assert.match(getFriendlyError(http(408, "x")), /too long/i);
    assert.match(getFriendlyError(http(422, "x")), /not valid/i);
    assert.match(getFriendlyError(http(503, "x")), /unavailable/i);
    assert.match(getFriendlyError(http(403, "x")), /permission/i);
    assert.match(getFriendlyError(http(404, "x")), /could not find/i);
    assert.equal(getFriendlyError(http(500, "x"), "Fallback text"), "Fallback text");
  });

  it("uses the Retry-After header on 429", () => {
    const msg = getFriendlyError(http(429, "rate_limit_exceeded", {}, { "retry-after": "45" }));
    assert.match(msg, /45 seconds/);
  });

  it("converts long waits to minutes and reads retry_after_seconds from the body", () => {
    const msg = getFriendlyError(http(429, "rate_limit_exceeded", { retry_after_seconds: 600 }));
    assert.match(msg, /10 minutes/);
  });

  it("handles 429 without any retry hint", () => {
    assert.match(getFriendlyError(http(429)), /wait a moment/);
  });

  it("handles network errors", () => {
    const msg = getFriendlyError({ code: "ERR_NETWORK", message: "Network Error", request: {} });
    assert.match(msg, /Cannot reach the server/);
  });

  it("handles schema mismatch errors without leaking details", () => {
    const err = new Error("Data format error while loading x");
    err.name = "SchemaMismatchError";
    assert.match(getFriendlyError(err), /unexpected format/);
  });

  it("returns the fallback for unknown shapes", () => {
    assert.equal(getFriendlyError(null, "Fallback"), "Fallback");
    assert.equal(getFriendlyError(new Error("boom"), "Fallback"), "Fallback");
  });

  it("parses a top-level body code", () => {
    const p = parseApiError({ response: { status: 404, data: { code: "not_found" } } });
    assert.equal(p.code, "not_found");
  });
});
