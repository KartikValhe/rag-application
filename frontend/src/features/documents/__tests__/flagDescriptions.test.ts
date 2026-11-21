import test from "node:test";
import assert from "node:assert/strict";
import {
  getFlagCodeDescription,
  formatFlaggedCountHeading,
  formatPageRange,
  formatMatchSource,
  TRIPWIRE_NOTICE,
  NO_LONGER_MATCHES_TEXT,
  FLAG_CODE_DESCRIPTIONS,
} from "../utils/flagDescriptions.ts";

const CODE_CASES: Array<{ code: string; expectedDescription: string }> = [
  { code: "instruction_override", expectedDescription: "Tells an AI to ignore its instructions" },
  { code: "role_markup", expectedDescription: "Looks like chat-system markup" },
  { code: "role_play", expectedDescription: "Tells an AI to act as something else" },
  { code: "prompt_extraction", expectedDescription: "Asks an AI to reveal its instructions" },
  { code: "model_directive", expectedDescription: "Gives orders to an AI" },
  { code: "tool_call", expectedDescription: "Looks like a tool or function call" },
  { code: "link_directive", expectedDescription: "Links or images that could send data out" },
  { code: "encoded_blob", expectedDescription: "A long encoded block of text" },
  { code: "unknown_injection_type", expectedDescription: "Flagged for review" },
  { code: "custom_future_rule", expectedDescription: "Flagged for review" },
];

test("flagDescriptions: table-driven test covers every code in the description table plus unknown code", () => {
  for (const { code, expectedDescription } of CODE_CASES) {
    const desc = getFlagCodeDescription(code);
    assert.equal(desc, expectedDescription, `Mismatch for code: ${code}`);
  }
  assert.equal(Object.keys(FLAG_CODE_DESCRIPTIONS).length, 8);
});

test("flagDescriptions: null, 0, and >0 count texts", () => {
  // null count
  const nullText = formatFlaggedCountHeading(null);
  assert.equal(
    nullText,
    "This file has not been checked yet. You can still publish after you confirm you have read this notice."
  );

  // undefined count
  const undefinedText = formatFlaggedCountHeading(undefined);
  assert.equal(
    undefinedText,
    "This file has not been checked yet. You can still publish after you confirm you have read this notice."
  );

  // 0 count
  const zeroText = formatFlaggedCountHeading(0);
  assert.equal(zeroText, "Nothing was flagged");

  // >0 count
  const singleText = formatFlaggedCountHeading(1);
  assert.equal(singleText, "1 passage was flagged for review");

  const pluralText = formatFlaggedCountHeading(5);
  assert.equal(pluralText, "5 passages were flagged for review");
});

test("flagDescriptions: page range formatting (single page and range)", () => {
  assert.equal(formatPageRange(3, 3), "Page 3");
  assert.equal(formatPageRange(3, null), "Page 3");
  assert.equal(formatPageRange(3, 4), "Pages 3-4");
  assert.equal(formatPageRange(1, 10), "Pages 1-10");
  assert.equal(formatPageRange(null, null), "-");
});

test("flagDescriptions: match source formatting (heading vs text)", () => {
  assert.equal(formatMatchSource("heading_path"), "in the heading");
  assert.equal(formatMatchSource("heading"), "in the heading");
  assert.equal(formatMatchSource("content"), "in the text");
  assert.equal(formatMatchSource("unknown"), "in the text");
});

test("flagDescriptions: tripwire line and no longer matches text constants", () => {
  assert.equal(TRIPWIRE_NOTICE, "This check is a tripwire. Text that is not flagged is not guaranteed safe.");
  assert.equal(NO_LONGER_MATCHES_TEXT, "No longer matches the current rules");
});
