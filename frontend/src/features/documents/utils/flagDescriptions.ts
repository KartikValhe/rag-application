export const FLAG_CODE_DESCRIPTIONS: Record<string, string> = {
  instruction_override: "Tells an AI to ignore its instructions",
  role_markup: "Looks like chat-system markup",
  role_play: "Tells an AI to act as something else",
  prompt_extraction: "Asks an AI to reveal its instructions",
  model_directive: "Gives orders to an AI",
  tool_call: "Looks like a tool or function call",
  link_directive: "Links or images that could send data out",
  encoded_blob: "A long encoded block of text",
};

export const UNKNOWN_FLAG_CODE_DESCRIPTION = "Flagged for review";

export const TRIPWIRE_NOTICE =
  "This check is a tripwire. Text that is not flagged is not guaranteed safe.";

export const NO_LONGER_MATCHES_TEXT = "No longer matches the current rules";

export function getFlagCodeDescription(code: string): string {
  return FLAG_CODE_DESCRIPTIONS[code] ?? UNKNOWN_FLAG_CODE_DESCRIPTION;
}

export function formatFlaggedCountHeading(count: number | null | undefined): string {
  if (count === null || count === undefined) {
    return "This file has not been checked yet. You can still publish after you confirm you have read this notice.";
  }
  if (count === 0) {
    return "Nothing was flagged";
  }
  if (count === 1) {
    return "1 passage was flagged for review";
  }
  return `${count} passages were flagged for review`;
}

export function formatPageRange(start?: number | null, end?: number | null): string {
  if (start == null && end == null) return "-";
  if (start != null && (end == null || end === start)) {
    return `Page ${start}`;
  }
  if (start != null && end != null) {
    return `Pages ${start}-${end}`;
  }
  return `Page ${end}`;
}

export function formatMatchSource(source: string): string {
  if (source === "heading_path" || source === "heading") {
    return "in the heading";
  }
  return "in the text";
}
