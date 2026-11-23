/** Plain-text helpers for version rows. Nothing here can return "Invalid Date", NaN or raw ISO text. */

/**
 * "Nov 21, 2026, 11:30 AM" style text for a server timestamp, or null when there is nothing usable
 * (missing, null, empty, not a string, not a real date). A caller shows nothing for null.
 */
export function formatChangedAt(value: unknown): string | null {
  if (typeof value !== "string" || value.trim() === "") return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  try {
    return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  } catch {
    return null;
  }
}
