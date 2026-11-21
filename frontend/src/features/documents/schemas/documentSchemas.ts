import { z } from "zod";
import { SCHEMA_MISMATCH_NAME } from "../../../api/errorMessages.ts";

export const NamedRefSchema = z.object({
  id: z.string(),
  name: z.string(),
});

export const DocumentVersionSchema = z.object({
  id: z.string(),
  document_id: z.string(),
  version_no: z.number().int(),
  status: z.string(),
  stage: z.string().nullable().optional(),
  error_code: z.string().nullable().optional(),
  file_kind: z.string(),
  original_filename: z.string(),
  size_bytes: z.number().int(),
  page_count: z.number().int().nullable().optional(),
  chunk_count: z.number().int().nullable().optional(),
  flagged_chunk_count: z.number().int().nullable().optional(),
  created_at: z.string(),
  published_at: z.string().nullable().optional(),
  retryable: z.boolean().nullable().optional(),
  warnings: z.array(z.string()).default([]),
});

export const DocumentSchema = z.object({
  id: z.string(),
  title: z.string(),
  description: z.string().nullable().optional(),
  subject: NamedRefSchema,
  classes: z.array(NamedRefSchema),
  created_by: z.string(),
  created_at: z.string(),
  updated_at: z.string(),
  latest_version: DocumentVersionSchema.nullable().optional(),
  published_version: DocumentVersionSchema.nullable().optional(),
  can_manage: z.boolean(),
  blocked_reason: z.string().nullable().optional(),
});

export const DocumentListSchema = z.object({
  items: z.array(DocumentSchema),
  next_cursor: z.string().nullable().optional(),
});

export const DocumentStatusSchema = z.object({
  document_id: z.string(),
  version_id: z.string(),
  version_no: z.number().int(),
  status: z.string(),
  stage: z.string().nullable().optional(),
  error_code: z.string().nullable().optional(),
  retryable: z.boolean(),
  updated_at: z.string(),
});

export const FlagMatchSchema = z.object({
  code: z.string(),
  source: z.enum(["heading_path", "content"]).or(z.string()),
  excerpt: z.string(),
});

export const FlaggedChunkSchema = z.object({
  chunk_id: z.string(),
  ordinal: z.number().int(),
  page_start: z.number().int().nullable().optional(),
  page_end: z.number().int().nullable().optional(),
  heading_path: z.string().nullable().optional(),
  injection_flags: z.array(z.string()),
  excluded: z.boolean(),
  excerpt: z.string(),
  matches: z.array(FlagMatchSchema).default([]),
});

export const VersionFlagsSchema = z.object({
  document_id: z.string(),
  version_id: z.string(),
  version_no: z.number().int(),
  version_status: z.string(),
  flagged_chunk_count: z.number().int().nullable().optional(),
  flags_acknowledged: z.boolean(),
  title_flagged: z.boolean(),
  filename_flagged: z.boolean(),
  items: z.array(FlaggedChunkSchema),
  next_after_ordinal: z.number().int().nullable().optional(),
});

export const TestSearchHitSchema = z.object({
  rank: z.number().int(),
  chunk_id: z.string(),
  document_id: z.string(),
  document_title: z.string(),
  version_id: z.string(),
  version_no: z.number().int(),
  version_status: z.string(),
  page_start: z.number().int().nullable().optional(),
  page_end: z.number().int().nullable().optional(),
  heading_path: z.string().nullable().optional(),
  content: z.string(),
  vector_rank: z.number().int().nullable().optional(),
  vector_similarity: z.number().nullable().optional(),
  keyword_rank: z.number().int().nullable().optional(),
  keyword_matched_terms: z.number().int().nullable().optional(),
  fused_score: z.number(),
  flagged: z.boolean(),
  injection_flags: z.array(z.string()),
  title_flagged: z.boolean(),
  filename_flagged: z.boolean(),
});

export const TestSearchResponseSchema = z.object({
  sufficient: z.boolean(),
  reason: z.string(),
  message: z.string().nullable().optional(),
  threshold: z.number(),
  threshold_calibrated: z.boolean(),
  keyword_coverage_threshold: z.number().nullable().optional(),
  model: z.string(),
  candidate_count: z.number().int(),
  hits: z.array(TestSearchHitSchema),
});

export const DocumentLimitsSchema = z.object({
  max_file_bytes: z.number().int(),
  max_pdf_pages: z.number().int(),
  max_pptx_slides: z.number().int(),
  max_documents: z.number().int(),
  max_storage_bytes: z.number().int(),
  max_pages: z.number().int(),
  uploads_per_user_per_hour: z.number().int(),
  uploads_per_tenant_per_hour: z.number().int(),
});

export const DocumentUsageSchema = z.object({
  documents: z.number().int(),
  storage_bytes: z.number().int(),
  pages: z.number().int(),
  limits: DocumentLimitsSchema,
});

export const DocumentUploadLimitsSchema = z.object({
  max_file_bytes: z.number().int(),
  max_pdf_pages: z.number().int(),
  max_pptx_slides: z.number().int(),
  uploads_per_user_per_hour: z.number().int(),
  uploads_per_tenant_per_hour: z.number().int(),
  accepted_extensions: z.array(z.string()),
});

export type NamedRefItem = z.infer<typeof NamedRefSchema>;
export type DocumentVersionItem = z.infer<typeof DocumentVersionSchema>;
export type DocumentItem = z.infer<typeof DocumentSchema>;
export type DocumentListItem = z.infer<typeof DocumentListSchema>;
export type DocumentStatusItem = z.infer<typeof DocumentStatusSchema>;
export type FlagMatchItem = z.infer<typeof FlagMatchSchema>;
export type FlaggedChunkItem = z.infer<typeof FlaggedChunkSchema>;
export type VersionFlagsItem = z.infer<typeof VersionFlagsSchema>;
export type TestSearchHitItem = z.infer<typeof TestSearchHitSchema>;
export type TestSearchResponseItem = z.infer<typeof TestSearchResponseSchema>;
export type DocumentLimitsItem = z.infer<typeof DocumentLimitsSchema>;
export type DocumentUsageItem = z.infer<typeof DocumentUsageSchema>;
export type DocumentUploadLimitsItem = z.infer<typeof DocumentUploadLimitsSchema>;

/**
 * Validates data from an API response against a Zod schema.
 * On schema mismatch:
 * - Logs full details in development only (import.meta.env.DEV)
 * - Throws a friendly error message suitable for end-user display with name SCHEMA_MISMATCH_NAME.
 */
export function validateResponse<T>(
  schema: z.ZodType<T, any, any>,
  data: unknown,
  endpointName: string
): T {
  const result = schema.safeParse(data);
  if (!result.success) {
    const isDev =
      (typeof import.meta !== "undefined" &&
        typeof import.meta.env !== "undefined" &&
        Boolean(import.meta.env?.DEV)) ||
      (typeof process !== "undefined" && process.env?.NODE_ENV !== "production");

    if (isDev) {
      console.error(
        `[API Schema Validation Failed] ${endpointName}:`,
        result.error.format(),
        "\nPayload received:",
        data
      );
    }
    const mismatch = new Error(
      `Data format error while loading ${endpointName}. Please refresh or contact support.`
    );
    mismatch.name = SCHEMA_MISMATCH_NAME;
    throw mismatch;
  }
  return result.data;
}
