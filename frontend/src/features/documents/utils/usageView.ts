/** Plain-text formatting for the admin usage page. Nothing here can return NaN, Infinity or "undefined". */

export const NOT_AVAILABLE = "Not available";

export type MeterLevel = "unknown" | "ok" | "warning" | "full";

export interface Meter {
  /** The line shown to people, for example "120 of 200 (60%)", or just "120" when there is no usable maximum. */
  text: string;
  /** Whole percent, rounded down (199 of 200 is 99%, never 100%). Null when it cannot be calculated. */
  percent: number | null;
  level: MeterLevel;
  /** The level in words, so colour is never the only signal. Empty only when the level is unknown. */
  levelText: string;
  /** Values for a native <progress>; null when there is no percentage to draw. */
  progress: { value: number; max: number } | null;
}

const WARNING_PERCENT = 80;
const FULL_PERCENT = 100;
const UNITS = ["KB", "MB", "GB", "TB"] as const;

function isCount(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0;
}

function groupDigits(value: number, fractionDigits: number): string {
  return value.toLocaleString("en-US", {
    minimumFractionDigits: fractionDigits,
    maximumFractionDigits: fractionDigits,
  });
}

/** A whole count with thousands separators. */
export function formatCount(value: number | null | undefined): string {
  if (!isCount(value)) return NOT_AVAILABLE;
  return groupDigits(Math.floor(value), 0);
}

/** A size in B, KB, MB, GB or TB (binary units, one decimal). */
export function formatBytes(value: number | null | undefined): string {
  if (!isCount(value)) return NOT_AVAILABLE;
  if (value < 1024) return `${groupDigits(Math.floor(value), 0)} B`;
  let unit = 0;
  let scaled = value / 1024;
  // A value like 1023.96 KB rounds to 1024.0, so it is shown in the next unit instead
  while (unit < UNITS.length - 1 && Number(scaled.toFixed(1)) >= 1024) {
    scaled /= 1024;
    unit += 1;
  }
  return `${groupDigits(scaled, 1)} ${UNITS[unit]}`;
}

export function getMeter(
  used: number | null | undefined,
  max: number | null | undefined,
  kind: "count" | "bytes"
): Meter {
  const format = kind === "bytes" ? formatBytes : formatCount;
  if (!isCount(used)) {
    return { text: NOT_AVAILABLE, percent: null, level: "unknown", levelText: "", progress: null };
  }
  if (!isCount(max) || max === 0) {
    return { text: format(used), percent: null, level: "unknown", levelText: "", progress: null };
  }
  const percent = Math.floor((used / max) * 100);
  const level: MeterLevel =
    percent >= FULL_PERCENT ? "full" : percent >= WARNING_PERCENT ? "warning" : "ok";
  const levelText =
    level === "full" ? "Limit reached" : level === "warning" ? "Nearly full" : "Within limit";
  return {
    text: `${format(used)} of ${format(max)} (${groupDigits(percent, 0)}%)`,
    percent,
    level,
    levelText,
    progress: { value: Math.min(used, max), max },
  };
}
