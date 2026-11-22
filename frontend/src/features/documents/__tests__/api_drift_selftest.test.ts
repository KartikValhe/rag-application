// Proves the drift guard can fail: each case changes ONE name in an in-memory copy of the doc
// and checks the guard names exactly what drifted. The real doc is never written.
import test from "node:test";
import assert from "node:assert/strict";
import { collectDocFacts, collectFrontendFacts, findDrift, readDoc, type FrontendFacts } from "./apiDriftCheck.ts";

const realDoc = readDoc();
let facts: FrontendFacts;

async function drift(mutate: (doc: string) => string): Promise<string[]> {
  facts ??= await collectFrontendFacts();
  return findDrift(collectDocFacts(mutate(realDoc)), facts);
}

function replaceOnce(doc: string, from: string, to: string): string {
  assert.ok(doc.includes(from), `self-test setup: the doc no longer contains ${JSON.stringify(from)}`);
  return doc.replace(from, to);
}

test("self-test: the unchanged doc has no drift", async () => {
  assert.deepEqual(await drift((d) => d), []);
});

test("self-test: a renamed error code in the doc is reported as unmapped", async () => {
  const problems = await drift((d) => replaceOnce(d, "| `duplicate_version` | 409 |", "| `duplicate_versionn` | 409 |"));
  assert.ok(problems.some((p) => p.includes('ERROR CODE') && p.includes('"duplicate_versionn"')), problems.join("\n"));
});

test("self-test: a brand new error code in the doc is reported", async () => {
  const problems = await drift((d) => replaceOnce(d, "| `invalid_title` | 422 |", "| `invalid_title` | 422 |\n| `brand_new_code` | 409 | x | y |\n| `invalid_title` | 422 |"));
  assert.ok(problems.some((p) => p.includes('"brand_new_code"')), problems.join("\n"));
});

test("self-test: an allow-listed code that left the doc is reported as stale", async () => {
  const problems = await drift((d) => replaceOnce(d, "| `subject_has_documents` | 409 |", "| `subject_has_docs` | 409 |"));
  assert.ok(problems.some((p) => p.includes("subject_has_documents") && p.includes("no longer lists it")), problems.join("\n"));
});

test("self-test: a renamed worker or parser failure code is reported", async () => {
  const problems = await drift((d) => replaceOnce(d, "| `scanned_pdf` |", "| `scanned_pdf_v2` |"));
  assert.ok(problems.some((p) => p.includes("VERSION FAILURE CODE") && p.includes('"scanned_pdf_v2"')), problems.join("\n"));
});

test("self-test: a renamed injection flag code is reported both ways", async () => {
  const problems = await drift((d) => replaceOnce(d, "| `role_play` |", "| `role_playing` |"));
  assert.ok(problems.some((p) => p.includes('"role_playing"') && p.includes("flagDescriptions.ts has no text")), problems.join("\n"));
  assert.ok(problems.some((p) => p.includes('"role_play"') && p.includes("no longer lists it")), problems.join("\n"));
});

test("self-test: a renamed permission is reported", async () => {
  const problems = await drift((d) => replaceOnce(d, "| `POST /api/v1/documents` | `document:upload` |", "| `POST /api/v1/documents` | `document:create` |"));
  assert.ok(problems.some((p) => p.includes("PERMISSION") && p.includes('"document:create"')), problems.join("\n"));
});

test("self-test: a renamed route path is reported", async () => {
  const problems = await drift((d) => replaceOnce(d, "`POST /api/v1/documents/test-search`", "`POST /api/v1/documents/search`"));
  assert.ok(problems.some((p) => p.includes("ROUTE") && p.includes("POST /documents/test-search")), problems.join("\n"));
});

test("self-test: a changed HTTP method is reported", async () => {
  const problems = await drift((d) => replaceOnce(d, "| `GET .../versions/{version_id}/status` |", "| `POST .../versions/{version_id}/status` |"));
  assert.ok(problems.some((p) => p.includes("ROUTE") && p.includes("GET /documents/{}/versions/{}/status")), problems.join("\n"));
});

test("self-test: a renamed response field is reported with the model name", async () => {
  const problems = await drift((d) => replaceOnce(d, "flagged_chunk_count: integer | null, flags_acknowledged", "flagged_count: integer | null, flags_acknowledged"));
  assert.ok(problems.some((p) => p.includes("VersionFlagsResponse") && p.includes('"flagged_chunk_count"')), problems.join("\n"));
});

test("self-test: a renamed limit field is reported", async () => {
  const problems = await drift((d) => replaceOnce(d, "`DocumentLimits`: `{max_file_bytes,", "`DocumentLimits`: `{max_file_size,"));
  assert.ok(problems.some((p) => p.includes("DocumentLimits") && p.includes('"max_file_bytes"')), problems.join("\n"));
});

test("self-test: a renamed request field is reported", async () => {
  const problems = await drift((d) => replaceOnce(d, "`TestSearchRequest` (5d-2): `{query: string,", "`TestSearchRequest` (5d-2): `{question: string,"));
  assert.ok(problems.some((p) => p.includes("REQUEST FIELD") && p.includes('"query"')), problems.join("\n"));
});

test("self-test: a changed numeric limit is reported", async () => {
  const problems = await drift((d) => replaceOnce(d, "`RETRIEVAL_MAX_QUERY_CHARS` (500)", "`RETRIEVAL_MAX_QUERY_CHARS` (400)"));
  assert.ok(problems.some((p) => p.includes("LIMIT") && p.includes("400")), problems.join("\n"));
});

test("self-test: a doc that lost its tables does not pass silently", async () => {
  const problems = await drift((d) => d.replace(/^\|/gm, "!"));
  assert.ok(problems.some((p) => p.startsWith("DOC PARSE")), problems.join("\n"));
});
