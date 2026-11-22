import type { TestSearchHitItem, TestSearchResponseItem } from "../schemas/documentSchemas.ts";
import { formatPageRange, getFlagCodeDescription } from "./flagDescriptions.ts";
import { getDocumentError, getSearchInputMessage } from "./documentErrors.ts";
import { SearchInputError } from "../api/documentsApi.ts";

export { SEARCH_MAX_QUERY_CHARS } from "../api/documentsApi.ts";

/** Result count the select starts on (the API default). */
export const DEFAULT_SEARCH_K = 6;
export const SEARCH_K_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] as const;

/** Passages are cut to this many lines or characters until the reader asks for more. */
const PREVIEW_MAX_LINES = 6;
const PREVIEW_MAX_CHARS = 600;

export interface SearchBanner {
  variant: "success" | "warning" | "info";
  text: string;
}

export interface TechnicalDetail {
  label: string;
  value: string;
}

export interface SearchHitView {
  key: string;
  rank: number;
  title: string;
  documentId: string;
  versionLabel: string;
  statusLabel: "Draft" | "Published" | null;
  isDraft: boolean;
  pageText: string | null;
  heading: string | null;
  similarityText: string;
  thresholdText: string | null;
  keywordText: string | null;
  technical: TechnicalDetail[];
  flagLabels: string[];
  titleFlagged: boolean;
  filenameFlagged: boolean;
  content: string;
}

export interface SearchView {
  banner: SearchBanner;
  assistantWouldNotUse: boolean;
  thresholdText: string;
  candidateText: string;
  calibration: { calibrated: boolean; text: string };
  announcement: string;
  hits: SearchHitView[];
}

export const ASSISTANT_WOULD_NOT_USE_NOTE =
  "The student assistant would not use these passages.";

/** Similarity with three decimals; a missing value is a dash, never 0. */
export function formatSimilarity(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "-";
  return value.toFixed(3);
}

function pluralPassages(count: number): string {
  return count === 1 ? "1 passage found" : `${count} passages found`;
}

export function getSearchBanner(
  response: Pick<TestSearchResponseItem, "sufficient" | "reason" | "message">,
  options: { includeDrafts?: boolean } = {}
): SearchBanner {
  const { sufficient, reason, message } = response;
  if (sufficient && (reason === "similarity" || reason === "keyword_coverage")) {
    return { variant: "success", text: "Enough matching material was found." };
  }
  switch (reason) {
    case "below_threshold":
      return {
        variant: "warning",
        text: "No passage was found that is similar enough to answer this question. The student assistant would answer 'not enough information'.",
      };
    case "no_candidates":
      return {
        variant: "warning",
        text: "No passage matched this question in the material you can search.",
      };
    case "empty_scope":
      return {
        variant: "info",
        text: options.includeDrafts
          ? "There is no material to search in this scope."
          : "There is no published material to search in this scope. Tick Include drafts to also search versions that are ready but not published.",
      };
    default:
      if (sufficient) return { variant: "success", text: "Enough matching material was found." };
      return {
        variant: "info",
        text: message && message.trim() !== "" ? message : "The search finished, but it could not tell if the material is enough.",
      };
  }
}

/** Cuts a long passage to a short preview. The text is only cut, never parsed or changed. */
export function collapsePassage(
  text: string,
  maxLines = PREVIEW_MAX_LINES,
  maxChars = PREVIEW_MAX_CHARS
): { preview: string; isTruncated: boolean } {
  const lines = text.split("\n");
  if (lines.length <= maxLines && text.length <= maxChars) {
    return { preview: text, isTruncated: false };
  }
  const preview = lines.slice(0, maxLines).join("\n").slice(0, maxChars);
  return { preview: `${preview}…`, isTruncated: true };
}

function statusLabelFor(status: string): "Draft" | "Published" | null {
  if (status === "ready") return "Draft";
  if (status === "published") return "Published";
  return null;
}

function buildHitView(hit: TestSearchHitItem, threshold: number): SearchHitView {
  const similarity = hit.vector_similarity;
  const hasSimilarity = similarity !== null && similarity !== undefined && !Number.isNaN(similarity);
  const flagLabels = Array.from(new Set(hit.injection_flags.map(getFlagCodeDescription)));
  if (hit.flagged && flagLabels.length === 0) flagLabels.push(getFlagCodeDescription(""));
  const hasKeyword = hit.keyword_rank !== null && hit.keyword_rank !== undefined;
  const hasPage = hit.page_start != null || hit.page_end != null;
  const statusLabel = statusLabelFor(hit.version_status);

  return {
    key: hit.chunk_id,
    rank: hit.rank,
    title: hit.document_title,
    documentId: hit.document_id,
    versionLabel: `Version ${hit.version_no}`,
    statusLabel,
    isDraft: statusLabel === "Draft",
    pageText: hasPage ? formatPageRange(hit.page_start, hit.page_end) : null,
    heading: hit.heading_path && hit.heading_path !== "" ? hit.heading_path : null,
    similarityText: formatSimilarity(similarity),
    thresholdText: hasSimilarity
      ? (similarity as number) >= threshold
        ? "above the threshold"
        : "below the threshold"
      : null,
    keywordText: hasKeyword
      ? `Keyword rank ${hit.keyword_rank}${
          hit.keyword_matched_terms != null ? `, ${hit.keyword_matched_terms} matching terms` : ""
        }`
      : null,
    technical: [
      { label: "Vector rank", value: hit.vector_rank != null ? String(hit.vector_rank) : "-" },
      { label: "Fused score", value: Number.isFinite(hit.fused_score) ? hit.fused_score.toFixed(4) : "-" },
    ],
    flagLabels,
    titleFlagged: hit.title_flagged,
    filenameFlagged: hit.filename_flagged,
    content: hit.content,
  };
}

/** Turns the API response into plain values for the panel. All decisions about wording live here. */
export function buildSearchView(
  response: TestSearchResponseItem,
  options: { includeDrafts?: boolean } = {}
): SearchView {
  const banner = getSearchBanner(response, options);
  const hits = [...response.hits]
    .sort((a, b) => a.rank - b.rank)
    .map((h) => buildHitView(h, response.threshold));

  const calibrated = response.threshold_calibrated === true;
  return {
    banner,
    assistantWouldNotUse: !response.sufficient,
    thresholdText: `Threshold ${response.threshold}`,
    candidateText: `Searched ${response.candidate_count} passages.`,
    calibration: {
      calibrated,
      text: calibrated
        ? "Calibrated for this model"
        : `Not calibrated for the current search model (${response.model}). Similarity numbers may mislead until retrieval is recalibrated.`,
    },
    announcement: hits.length > 0 ? pluralPassages(hits.length) : banner.text,
    hits,
  };
}

/** Error text for a failed search, only from the error table. `field: "query"` means: show it beside the box. */
export function describeSearchError(error: unknown): { message: string; field?: "query" } {
  if (error instanceof SearchInputError) {
    return { message: getSearchInputMessage(error.code), field: "query" };
  }
  const info = getDocumentError(error, "search");
  return { message: info.message, field: info.field === "query" ? "query" : undefined };
}
