import test from "node:test";
import assert from "node:assert/strict";
import { getStatusChip } from "../utils/statusChip.ts";

const CASES: Array<{
  status: string;
  stage?: string | null;
  flaggedCount?: number | null;
  expectedText: string;
  expectedVariant: string;
}> = [
  { status: "queued", expectedText: "Waiting", expectedVariant: "info" },
  { status: "processing", stage: "parsing", expectedText: "Reading file", expectedVariant: "info" },
  { status: "processing", stage: "chunking", expectedText: "Splitting text", expectedVariant: "info" },
  { status: "processing", stage: "scanning", expectedText: "Checking text", expectedVariant: "info" },
  { status: "processing", stage: "embedding", expectedText: "Indexing", expectedVariant: "info" },
  { status: "processing", stage: "indexing", expectedText: "Indexing", expectedVariant: "info" },
  { status: "processing", stage: "unexpected_stage", expectedText: "Processing", expectedVariant: "info" },
  { status: "processing", stage: null, expectedText: "Processing", expectedVariant: "info" },
  { status: "ready", flaggedCount: 0, expectedText: "Draft, ready to publish", expectedVariant: "primary" },
  { status: "ready", flaggedCount: 5, expectedText: "Draft, needs review (5 passages)", expectedVariant: "warning" },
  { status: "ready", flaggedCount: 1, expectedText: "Draft, needs review (1 passage)", expectedVariant: "warning" },
  { status: "ready", flaggedCount: null, expectedText: "Draft, not checked yet", expectedVariant: "warning" },
  { status: "published", expectedText: "Published", expectedVariant: "success" },
  { status: "failed", expectedText: "Failed", expectedVariant: "danger" },
  { status: "archived", expectedText: "Older version", expectedVariant: "default" },
  { status: "something_future", expectedText: "Unknown", expectedVariant: "default" },
];

test("statusChip: table test covers every case in section b1", () => {
  for (const c of CASES) {
    const chip = getStatusChip(c.status, c.stage, c.flaggedCount);
    assert.equal(
      chip.label,
      c.expectedText,
      `Label mismatch for status=${c.status}, stage=${c.stage}, count=${c.flaggedCount}`
    );
    assert.equal(
      chip.variant,
      c.expectedVariant,
      `Variant mismatch for status=${c.status}, stage=${c.stage}, count=${c.flaggedCount}`
    );
  }
});
