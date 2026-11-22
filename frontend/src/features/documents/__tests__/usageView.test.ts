import test from "node:test";
import assert from "node:assert/strict";
import { formatBytes, formatCount, getMeter, NOT_AVAILABLE } from "../utils/usageView.ts";

const KB = 1024;
const MB = KB * 1024;
const GB = MB * 1024;
const TB = GB * 1024;

test("formatBytes: zero, small, KB, MB, GB, TB", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(1), "1 B");
  assert.equal(formatBytes(1023), "1,023 B");
  assert.equal(formatBytes(KB), "1.0 KB");
  assert.equal(formatBytes(1536), "1.5 KB");
  assert.equal(formatBytes(MB), "1.0 MB");
  assert.equal(formatBytes(50 * MB), "50.0 MB");
  assert.equal(formatBytes(GB), "1.0 GB");
  assert.equal(formatBytes(TB), "1.0 TB");
});

test("formatBytes: a value that rounds up to 1024 moves to the next unit", () => {
  assert.equal(formatBytes(MB - 1), "1.0 MB");
  assert.equal(formatBytes(GB - 1), "1.0 GB");
});

test("formatBytes: very large values stay finite text and never show NaN or Infinity", () => {
  for (const big of [1e15, 1e18, Number.MAX_SAFE_INTEGER, 1e300]) {
    const text = formatBytes(big);
    assert.ok(/TB$/.test(text), text);
    assert.ok(!/NaN|Infinity|e\+/.test(text), text);
  }
});

test("formatBytes and formatCount: missing, null, NaN, infinite and negative values give the plain not-available text", () => {
  for (const bad of [null, undefined, NaN, Infinity, -Infinity, -1, "12" as unknown as number]) {
    assert.equal(formatBytes(bad), NOT_AVAILABLE);
    assert.equal(formatCount(bad), NOT_AVAILABLE);
  }
  assert.equal(NOT_AVAILABLE, "Not available");
});

test("formatCount: zero, thousands separators and very large values", () => {
  assert.equal(formatCount(0), "0");
  assert.equal(formatCount(999), "999");
  assert.equal(formatCount(1000), "1,000");
  assert.equal(formatCount(1234567), "1,234,567");
  assert.equal(formatCount(Number.MAX_SAFE_INTEGER), "9,007,199,254,740,991");
  assert.ok(!/NaN|Infinity|e\+/.test(formatCount(1e21)));
});

test("getMeter: normal count shows 'used of max (percent%)' and the within-limit words", () => {
  const m = getMeter(120, 200, "count");
  assert.equal(m.text, "120 of 200 (60%)");
  assert.equal(m.percent, 60);
  assert.equal(m.level, "ok");
  assert.equal(m.levelText, "Within limit");
  assert.equal(m.progress?.value, 120);
  assert.equal(m.progress?.max, 200);
});

test("getMeter: bytes use human sizes", () => {
  const m = getMeter(5 * MB, 10 * MB, "bytes");
  assert.equal(m.text, "5.0 MB of 10.0 MB (50%)");
  assert.equal(m.percent, 50);
});

test("getMeter: 79% is ok, 80% warns in words, 99% still warns, 100% and over say the limit is reached", () => {
  assert.equal(getMeter(79, 100, "count").level, "ok");
  const eighty = getMeter(80, 100, "count");
  assert.equal(eighty.level, "warning");
  assert.equal(eighty.levelText, "Nearly full");
  assert.equal(getMeter(99, 100, "count").level, "warning");
  // 199 of 200 is 99.5%: shown as 99% (rounded down), never as 100%
  const almost = getMeter(199, 200, "count");
  assert.equal(almost.percent, 99);
  assert.equal(almost.level, "warning");
  const full = getMeter(100, 100, "count");
  assert.equal(full.level, "full");
  assert.equal(full.levelText, "Limit reached");
  const over = getMeter(250, 200, "count");
  assert.equal(over.level, "full");
  assert.equal(over.percent, 125);
  assert.equal(over.progress?.value, 200, "the bar never goes past its maximum");
  assert.equal(over.progress?.max, 200);
});

test("getMeter: the level words are never empty for a known maximum (colour is not the only signal)", () => {
  for (const used of [0, 50, 80, 99, 100, 500]) {
    assert.ok(getMeter(used, 100, "count").levelText.length > 0, `used ${used}`);
  }
});

test("getMeter: a zero maximum shows the plain number, no percentage, no bar, and never divides by zero", () => {
  const m = getMeter(5, 0, "count");
  assert.equal(m.text, "5");
  assert.equal(m.percent, null);
  assert.equal(m.progress, null);
  assert.equal(m.level, "unknown");
  assert.ok(!/NaN|Infinity|%/.test(m.text));
});

test("getMeter: a missing, null, NaN or negative maximum shows the plain number with no percentage", () => {
  for (const max of [null, undefined, NaN, -5, Infinity]) {
    const m = getMeter(7, max as number | null | undefined, "count");
    assert.equal(m.text, "7");
    assert.equal(m.percent, null);
    assert.equal(m.progress, null);
    assert.equal(m.level, "unknown");
  }
  const bytes = getMeter(2 * KB, null, "bytes");
  assert.equal(bytes.text, "2.0 KB");
});

test("getMeter: a missing used value shows the not-available text and no percentage", () => {
  for (const used of [null, undefined, NaN, -1]) {
    const m = getMeter(used as number | null | undefined, 200, "count");
    assert.equal(m.text, NOT_AVAILABLE);
    assert.equal(m.percent, null);
    assert.equal(m.progress, null);
    assert.equal(m.level, "unknown");
  }
});

test("getMeter: zero used of a real maximum is 0%", () => {
  const m = getMeter(0, 200, "count");
  assert.equal(m.text, "0 of 200 (0%)");
  assert.equal(m.percent, 0);
  assert.equal(m.level, "ok");
});

test("getMeter: very large values keep exact percentages and finite text", () => {
  const m = getMeter(Number.MAX_SAFE_INTEGER / 2, Number.MAX_SAFE_INTEGER, "count");
  assert.equal(m.percent, 50);
  assert.ok(!/NaN|Infinity|e\+/.test(m.text));
  const huge = getMeter(1e300, 1e300, "count");
  assert.equal(huge.percent, 100);
  assert.equal(huge.level, "full");
});
