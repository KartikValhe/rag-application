/**
 * Decides which publish dialog opens: the simple confirm or the flag review.
 *
 * Simple confirm ONLY when the version is known to be clean (flagged_chunk_count is exactly 0).
 * Review when it has flagged passages (> 0), has never been scanned (null / undefined), or when the
 * server already told us during this dialog that a review is required (forced).
 */
export function needsFlagReview(
  flaggedChunkCount: number | null | undefined,
  forcedByServer = false
): boolean {
  return forcedByServer || flaggedChunkCount !== 0;
}

export const PUBLISH_TICK_TEXT =
  "I have read the flagged passages and want to publish this version";

export const PUBLISH_REASON_TICK = "Tick the box to confirm you have read the notice.";
export const PUBLISH_REASON_FLAGS_LOADING = "Loading the flagged passages.";
export const PUBLISH_REASON_FLAGS_FAILED =
  "The flagged passages could not be loaded. Try again before publishing.";
export const PUBLISH_REASON_NOT_AVAILABLE = "This version can no longer be published.";

export function formatReplacesLine(publishedVersionNo: number): string {
  return `Version ${publishedVersionNo} stays visible to students until you publish this one; then it is replaced.`;
}
