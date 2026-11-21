import type { DocumentItem } from "../schemas/documentSchemas.ts";

export const TERMINAL_STATUSES = new Set([
  "ready",
  "published",
  "failed",
  "archived",
]);

export const QUEUED_TIMEOUT_SECONDS = 300; // 5 minutes
export const LONG_RUNNING_THRESHOLD_SECONDS = 900; // 15 minutes
export const HARD_STOP_SECONDS = 3900; // 65 minutes

/**
 * Computes the polling delay in milliseconds for in-flight version status.
 * Sequence: 2 s, 3 s, 5 s, capped at 10 s.
 * After a failed poll, wait doubles up to 30 s.
 */
export function nextPollDelay(
  attempt: number,
  errorOrCount: boolean | number = 0
): number {
  const safeAttempt = Math.max(0, attempt);
  const baseDelay =
    safeAttempt === 0
      ? 2000
      : safeAttempt === 1
        ? 3000
        : safeAttempt === 2
          ? 5000
          : 10000;

  if (!errorOrCount) {
    return baseDelay;
  }

  const errorCount = typeof errorOrCount === "boolean" ? 1 : errorOrCount;
  const backedOff = baseDelay * Math.pow(2, errorCount);
  return Math.min(backedOff, 30000);
}

/**
 * Checks whether a version status represents a terminal state.
 * Terminal states: ready, published, failed, archived.
 */
export function isTerminalStatus(status: string | null | undefined): boolean {
  if (!status) return false;
  return TERMINAL_STATUSES.has(status);
}

/**
 * Checks whether the course materials list should be polling.
 * True only when a visible document's latest version is queued or processing.
 * Returns false when hidden or when no items are in flight.
 */
export function shouldPollList(
  items: DocumentItem[] | undefined | null,
  isHidden = false
): boolean {
  if (isHidden || !items || items.length === 0) {
    return false;
  }

  return items.some((item) => {
    const status = item.latest_version?.status;
    return status === "queued" || status === "processing";
  });
}

export interface LongRunningStatusInfo {
  isHardStop: boolean;
  stageText: string;
  elapsedSeconds: number;
  hint?: string;
  isStuckQueued: boolean;
  isTakingLonger: boolean;
}

/**
 * Pure evaluation of stage text, elapsed seconds, stuck queued notice,
 * and the 65-minute hard stop for in-flight versions.
 */
export function getLongRunningStatus(
  status: string | null | undefined,
  stage: string | null | undefined,
  elapsedSeconds: number
): LongRunningStatusInfo {
  let stageText = "Processing";
  if (stage === "parsing") {
    stageText = "Reading file";
  } else if (stage === "chunking") {
    stageText = "Splitting text";
  } else if (stage === "scanning") {
    stageText = "Checking text";
  } else if (stage === "embedding" || stage === "indexing") {
    stageText = "Indexing";
  }

  const isHardStop = elapsedSeconds >= HARD_STOP_SECONDS;
  const isStuckQueued = status === "queued" && elapsedSeconds > QUEUED_TIMEOUT_SECONDS;
  const isTakingLonger = !isHardStop && elapsedSeconds > LONG_RUNNING_THRESHOLD_SECONDS;

  let hint: string | undefined = undefined;
  if (isStuckQueued) {
    hint =
      "Waiting for the processing service. If this does not change soon, contact your administrator.";
  } else if (isTakingLonger) {
    hint = "Taking longer than usual";
  }

  return {
    isHardStop,
    stageText,
    elapsedSeconds,
    hint,
    isStuckQueued,
    isTakingLonger,
  };
}
