import test from "node:test";
import assert from "node:assert/strict";
import {
  DocumentVersionSchema,
  DocumentSchema,
  DocumentListSchema,
  DocumentStatusSchema,
  VersionFlagsSchema,
  FlaggedChunkSchema,
  TestSearchResponseSchema,
  TestSearchHitSchema,
  DocumentUsageSchema,
  DocumentLimitsSchema,
  DocumentUploadLimitsSchema,
  validateResponse,
} from "../schemas/documentSchemas.ts";
import { SCHEMA_MISMATCH_NAME } from "../../../api/errorMessages.ts";

test("documentSchemas: full valid payloads", () => {
  const versionPayload = {
    id: "v-1",
    document_id: "doc-1",
    version_no: 1,
    status: "ready",
    stage: null,
    error_code: null,
    file_kind: "pdf",
    original_filename: "lecture.pdf",
    size_bytes: 1024,
    page_count: 5,
    chunk_count: 10,
    flagged_chunk_count: 0,
    created_at: "2026-11-20T10:00:00Z",
    published_at: null,
    retryable: null,
    warnings: [],
  };
  const parsedVersion = DocumentVersionSchema.parse(versionPayload);
  assert.equal(parsedVersion.id, "v-1");
  assert.equal(parsedVersion.retryable, null);

  const docPayload = {
    id: "doc-1",
    title: "Chapter 1",
    description: "Introductory notes",
    subject: { id: "sub-1", name: "Math" },
    classes: [{ id: "cls-1", name: "Class 10A" }],
    created_by: "usr-1",
    created_at: "2026-11-20T10:00:00Z",
    updated_at: "2026-11-20T10:00:00Z",
    latest_version: versionPayload,
    published_version: null,
    can_manage: true,
    blocked_reason: null,
  };
  const parsedDoc = DocumentSchema.parse(docPayload);
  assert.equal(parsedDoc.title, "Chapter 1");

  const listPayload = {
    items: [docPayload],
    next_cursor: "cursor-123",
  };
  const parsedList = DocumentListSchema.parse(listPayload);
  assert.equal(parsedList.items.length, 1);
  assert.equal(parsedList.next_cursor, "cursor-123");

  const statusPayload = {
    document_id: "doc-1",
    version_id: "v-1",
    version_no: 1,
    status: "processing",
    stage: "parsing",
    error_code: null,
    retryable: true,
    updated_at: "2026-11-20T10:01:00Z",
  };
  const parsedStatus = DocumentStatusSchema.parse(statusPayload);
  assert.equal(parsedStatus.status, "processing");

  const flagsPayload = {
    document_id: "doc-1",
    version_id: "v-1",
    version_no: 1,
    version_status: "ready",
    flagged_chunk_count: 1,
    flags_acknowledged: false,
    title_flagged: false,
    filename_flagged: true,
    items: [
      {
        chunk_id: "chk-1",
        ordinal: 0,
        page_start: 1,
        page_end: 2,
        heading_path: "Intro > Overview",
        injection_flags: ["instruction_override"],
        excluded: false,
        excerpt: "Ignore previous instructions",
        matches: [
          {
            code: "instruction_override",
            source: "content",
            excerpt: "Ignore previous instructions",
          },
        ],
      },
    ],
    next_after_ordinal: null,
  };
  const parsedFlags = VersionFlagsSchema.parse(flagsPayload);
  assert.equal(parsedFlags.items.length, 1);
  assert.equal(parsedFlags.items[0].matches[0].source, "content");

  const testSearchPayload = {
    sufficient: true,
    reason: "similarity",
    message: null,
    threshold: 0.84,
    threshold_calibrated: true,
    keyword_coverage_threshold: null,
    model: "text-embedding-3-small",
    candidate_count: 42,
    hits: [
      {
        rank: 1,
        chunk_id: "chk-1",
        document_id: "doc-1",
        document_title: "Chapter 1",
        version_id: "v-1",
        version_no: 1,
        version_status: "published",
        page_start: 1,
        page_end: 2,
        heading_path: null,
        content: "Here is the textbook passage.",
        vector_rank: 1,
        vector_similarity: 0.912,
        keyword_rank: null,
        keyword_matched_terms: null,
        fused_score: 0.912,
        flagged: false,
        injection_flags: [],
        title_flagged: false,
        filename_flagged: false,
      },
    ],
  };
  const parsedSearch = TestSearchResponseSchema.parse(testSearchPayload);
  assert.equal(parsedSearch.hits[0].keyword_rank, null);

  const limitsPayload = {
    max_file_bytes: 52428800,
    max_pdf_pages: 500,
    max_pptx_slides: 200,
    max_documents: 1000,
    max_storage_bytes: 10737418240,
    max_pages: 50000,
    uploads_per_user_per_hour: 20,
    uploads_per_tenant_per_hour: 100,
  };
  assert.equal(DocumentLimitsSchema.parse(limitsPayload).max_file_bytes, 52428800);

  const usagePayload = {
    documents: 50,
    storage_bytes: 104857600,
    pages: 250,
    limits: limitsPayload,
  };
  assert.equal(DocumentUsageSchema.parse(usagePayload).documents, 50);

  const uploadLimitsPayload = {
    max_file_bytes: 52428800,
    max_pdf_pages: 500,
    max_pptx_slides: 200,
    uploads_per_user_per_hour: 20,
    uploads_per_tenant_per_hour: 100,
    accepted_extensions: ["docx", "md", "pdf", "pptx", "txt"],
  };
  assert.equal(DocumentUploadLimitsSchema.parse(uploadLimitsPayload).accepted_extensions.length, 5);
});

test("documentSchemas: every optional key present with null for each response", () => {
  // 1. DocumentVersionSchema with all nullables explicitly null
  const nullVersion = {
    id: "v-null",
    document_id: "doc-null",
    version_no: 1,
    status: "failed",
    stage: null,
    error_code: null,
    file_kind: "pdf",
    original_filename: "file.pdf",
    size_bytes: 100,
    page_count: null,
    chunk_count: null,
    flagged_chunk_count: null,
    created_at: "2026-11-20T10:00:00Z",
    published_at: null,
    retryable: null,
    warnings: [],
  };
  const parsedVersion = DocumentVersionSchema.parse(nullVersion);
  assert.equal(parsedVersion.stage, null);
  assert.equal(parsedVersion.error_code, null);
  assert.equal(parsedVersion.page_count, null);
  assert.equal(parsedVersion.chunk_count, null);
  assert.equal(parsedVersion.flagged_chunk_count, null);
  assert.equal(parsedVersion.published_at, null);
  assert.equal(parsedVersion.retryable, null);

  // 2. DocumentSchema with all nullables explicitly null
  const nullDoc = {
    id: "doc-null",
    title: "Untitled",
    description: null,
    subject: { id: "sub-1", name: "Math" },
    classes: [],
    created_by: "usr-1",
    created_at: "2026-11-20T10:00:00Z",
    updated_at: "2026-11-20T10:00:00Z",
    latest_version: null,
    published_version: null,
    can_manage: true,
    blocked_reason: null,
  };
  const parsedDoc = DocumentSchema.parse(nullDoc);
  assert.equal(parsedDoc.description, null);
  assert.equal(parsedDoc.latest_version, null);
  assert.equal(parsedDoc.published_version, null);
  assert.equal(parsedDoc.blocked_reason, null);

  // 3. DocumentListSchema with next_cursor explicitly null
  const nullList = {
    items: [nullDoc],
    next_cursor: null,
  };
  const parsedList = DocumentListSchema.parse(nullList);
  assert.equal(parsedList.next_cursor, null);

  // 4. DocumentStatusSchema with nullables explicitly null
  const nullStatus = {
    document_id: "doc-null",
    version_id: "v-null",
    version_no: 1,
    status: "ready",
    stage: null,
    error_code: null,
    retryable: false,
    updated_at: "2026-11-20T10:00:00Z",
  };
  const parsedStatus = DocumentStatusSchema.parse(nullStatus);
  assert.equal(parsedStatus.stage, null);
  assert.equal(parsedStatus.error_code, null);

  // 5. FlaggedChunkSchema with nullables explicitly null
  const nullChunk = {
    chunk_id: "chk-null",
    ordinal: 0,
    page_start: null,
    page_end: null,
    heading_path: null,
    injection_flags: [],
    excluded: false,
    excerpt: "Safe excerpt",
    matches: [],
  };
  const parsedChunk = FlaggedChunkSchema.parse(nullChunk);
  assert.equal(parsedChunk.page_start, null);
  assert.equal(parsedChunk.page_end, null);
  assert.equal(parsedChunk.heading_path, null);

  // 6. VersionFlagsSchema with nullables explicitly null
  const nullFlags = {
    document_id: "doc-null",
    version_id: "v-null",
    version_no: 1,
    version_status: "ready",
    flagged_chunk_count: null,
    flags_acknowledged: false,
    title_flagged: false,
    filename_flagged: false,
    items: [nullChunk],
    next_after_ordinal: null,
  };
  const parsedFlags = VersionFlagsSchema.parse(nullFlags);
  assert.equal(parsedFlags.flagged_chunk_count, null);
  assert.equal(parsedFlags.next_after_ordinal, null);

  // 7. TestSearchHitSchema with nullables explicitly null
  const nullHit = {
    rank: 1,
    chunk_id: "chk-null",
    document_id: "doc-null",
    document_title: "Title",
    version_id: "v-null",
    version_no: 1,
    version_status: "published",
    page_start: null,
    page_end: null,
    heading_path: null,
    content: "Content text",
    vector_rank: null,
    vector_similarity: null,
    keyword_rank: null,
    keyword_matched_terms: null,
    fused_score: 0.5,
    flagged: false,
    injection_flags: [],
    title_flagged: false,
    filename_flagged: false,
  };
  const parsedHit = TestSearchHitSchema.parse(nullHit);
  assert.equal(parsedHit.page_start, null);
  assert.equal(parsedHit.page_end, null);
  assert.equal(parsedHit.heading_path, null);
  assert.equal(parsedHit.vector_rank, null);
  assert.equal(parsedHit.vector_similarity, null);
  assert.equal(parsedHit.keyword_rank, null);
  assert.equal(parsedHit.keyword_matched_terms, null);

  // 8. TestSearchResponseSchema with nullables explicitly null
  const nullSearch = {
    sufficient: false,
    reason: "below_threshold",
    message: null,
    threshold: 0.84,
    threshold_calibrated: false,
    keyword_coverage_threshold: null,
    model: "embedding-model",
    candidate_count: 0,
    hits: [nullHit],
  };
  const parsedSearch = TestSearchResponseSchema.parse(nullSearch);
  assert.equal(parsedSearch.message, null);
  assert.equal(parsedSearch.keyword_coverage_threshold, null);
});

test("documentSchemas: lenient with unknown extra keys", () => {
  const payloadWithExtras = {
    id: "v-2",
    document_id: "doc-2",
    version_no: 2,
    status: "unknown_future_status",
    stage: null,
    error_code: null,
    file_kind: "pdf",
    original_filename: "doc.pdf",
    size_bytes: 2048,
    page_count: null,
    chunk_count: null,
    flagged_chunk_count: null,
    created_at: "2026-11-20T10:00:00Z",
    published_at: null,
    retryable: true,
    warnings: [],
    future_feature_flag: "active",
    another_untracked_field: 12345,
  };
  const parsed = DocumentVersionSchema.parse(payloadWithExtras);
  assert.equal(parsed.status, "unknown_future_status");
  assert.equal((parsed as Record<string, unknown>).future_feature_flag, undefined);
});

test("documentSchemas: accepts unknown status string without throwing", () => {
  const statusPayload = {
    document_id: "doc-1",
    version_id: "v-1",
    version_no: 1,
    status: "something_unexpected_new",
    stage: null,
    error_code: null,
    retryable: false,
    updated_at: "2026-11-20T10:00:00Z",
  };
  const parsed = DocumentStatusSchema.parse(statusPayload);
  assert.equal(parsed.status, "something_unexpected_new");
});

test("documentSchemas: validateResponse throws SchemaMismatchError on missing required key", () => {
  const invalidPayload = {
    id: "v-1",
    // missing version_no and status
    file_kind: "pdf",
  };

  assert.throws(
    () => {
      validateResponse(DocumentVersionSchema, invalidPayload, "test endpoint");
    },
    (err: unknown) => {
      const error = err as Error;
      return error.name === SCHEMA_MISMATCH_NAME && error.message.includes("test endpoint");
    }
  );
});

test("documentSchemas: VersionFlagsSchema accepts matches and unknown extra keys", () => {
  const flagsPayload = {
    document_id: "doc-1",
    version_id: "v-1",
    version_no: 1,
    version_status: "ready",
    flagged_chunk_count: 1,
    flags_acknowledged: false,
    title_flagged: false,
    filename_flagged: false,
    unknown_top_field: "extra",
    items: [
      {
        chunk_id: "chk-1",
        ordinal: 0,
        page_start: 1,
        page_end: 2,
        heading_path: "Intro",
        injection_flags: ["instruction_override"],
        excluded: false,
        excerpt: "Ignore previous instructions",
        unknown_chunk_field: 42,
        matches: [
          {
            code: "instruction_override",
            source: "content",
            excerpt: "Ignore previous instructions",
            unknown_match_field: true,
          },
        ],
      },
    ],
    next_after_ordinal: null,
  };
  const parsed = VersionFlagsSchema.parse(flagsPayload);
  assert.equal(parsed.items.length, 1);
  assert.equal(parsed.items[0].matches.length, 1);
  assert.equal(parsed.items[0].matches[0].code, "instruction_override");
  assert.equal(parsed.items[0].matches[0].source, "content");
  assert.equal(parsed.items[0].matches[0].excerpt, "Ignore previous instructions");
});

test("known limitation (5f-5 leftover C): the version response carries no updated or processing-started timestamp", () => {
  // The 65-minute polling stop can only start from created_at after a page reload. 5g-1 added updated_at, but it
  // moves with every column change (status, stage, counts), so it is NOT a processing start. If the backend ever
  // adds a real start timestamp, this test fails: then base the stop on it and delete this test.
  const keys = Object.keys(DocumentVersionSchema.shape);
  const timestamps = keys.filter((k) => /(_at|_started|started_)/.test(k)).sort();
  assert.deepEqual(timestamps, ["created_at", "published_at", "updated_at"]);
});
