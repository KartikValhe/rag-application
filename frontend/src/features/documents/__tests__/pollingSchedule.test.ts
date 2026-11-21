import test from "node:test";
import assert from "node:assert/strict";
import {
  nextPollDelay,
  isTerminalStatus,
  shouldPollList,
  getLongRunningStatus,
} from "../utils/pollingSchedule.ts";
import type { DocumentItem } from "../schemas/documentSchemas.ts";

function createMockItem(status?: string): DocumentItem {
  return {
    id: "doc-1",
    title: "Algorithms",
    description: null,
    subject: { id: "s1", name: "CS" },
    classes: [{ id: "c1", name: "10A" }],
    created_by: "u1",
    created_at: "2026-11-20T10:00:00Z",
    updated_at: "2026-11-20T10:00:00Z",
    latest_version: status
      ? {
          id: "v1",
          document_id: "doc-1",
          version_no: 1,
          status,
          stage: null,
          error_code: null,
          file_kind: "pdf",
          original_filename: "alg.pdf",
          size_bytes: 1000,
          created_at: "2026-11-20T10:00:00Z",
          warnings: [],
        }
      : null,
    published_version: null,
    can_manage: true,
    blocked_reason: null,
  };
}

test("nextPollDelay: sequence 2s, 3s, 5s, capped at 10s", () => {
  assert.equal(nextPollDelay(0), 2000);
  assert.equal(nextPollDelay(1), 3000);
  assert.equal(nextPollDelay(2), 5000);
  assert.equal(nextPollDelay(3), 10000);
  assert.equal(nextPollDelay(4), 10000);
  assert.equal(nextPollDelay(100), 10000);
  assert.equal(nextPollDelay(-1), 2000);
});

test("nextPollDelay: backoff after failed poll doubles wait capped at 30s", () => {
  // 1 failure doubles base delay
  assert.equal(nextPollDelay(0, true), 4000);
  assert.equal(nextPollDelay(1, true), 6000);
  assert.equal(nextPollDelay(2, true), 10000);
  assert.equal(nextPollDelay(3, true), 20000);

  // multiple consecutive errors double further up to 30s
  assert.equal(nextPollDelay(0, 1), 4000);
  assert.equal(nextPollDelay(0, 2), 8000);
  assert.equal(nextPollDelay(0, 3), 16000);
  assert.equal(nextPollDelay(0, 4), 30000); // 32s -> capped at 30s
  assert.equal(nextPollDelay(3, 2), 30000); // 10s * 4 = 40s -> capped at 30s
  assert.equal(nextPollDelay(3, 10), 30000);
});

test("isTerminalStatus: ready, published, failed, archived are terminal", () => {
  assert.equal(isTerminalStatus("ready"), true);
  assert.equal(isTerminalStatus("published"), true);
  assert.equal(isTerminalStatus("failed"), true);
  assert.equal(isTerminalStatus("archived"), true);

  assert.equal(isTerminalStatus("queued"), false);
  assert.equal(isTerminalStatus("processing"), false);
  assert.equal(isTerminalStatus("unknown"), false);
  assert.equal(isTerminalStatus(""), false);
  assert.equal(isTerminalStatus(null), false);
  assert.equal(isTerminalStatus(undefined), false);
});

test("shouldPollList: true only when a visible latest version is queued or processing", () => {
  assert.equal(shouldPollList([]), false);
  assert.equal(shouldPollList([createMockItem()]), false);
  assert.equal(shouldPollList([createMockItem("ready")]), false);
  assert.equal(shouldPollList([createMockItem("published")]), false);
  assert.equal(shouldPollList([createMockItem("failed")]), false);
  assert.equal(shouldPollList([createMockItem("archived")]), false);

  assert.equal(shouldPollList([createMockItem("queued")]), true);
  assert.equal(shouldPollList([createMockItem("processing")]), true);
  assert.equal(
    shouldPollList([createMockItem("ready"), createMockItem("processing")]),
    true
  );
});

test("shouldPollList: no poll when hidden flag is passed in", () => {
  assert.equal(shouldPollList([createMockItem("queued")], true), false);
  assert.equal(shouldPollList([createMockItem("processing")], true), false);
  assert.equal(
    shouldPollList([createMockItem("ready"), createMockItem("processing")], true),
    false
  );
  assert.equal(
    shouldPollList([createMockItem("ready"), createMockItem("processing")], false),
    true
  );
});

test("long-running texts: stage text with elapsed time", () => {
  const info = getLongRunningStatus("processing", "parsing", 45);
  assert.equal(info.stageText, "Reading file");
  assert.equal(info.elapsedSeconds, 45);
  assert.equal(info.isHardStop, false);
  assert.equal(info.isStuckQueued, false);
  assert.equal(info.isTakingLonger, false);
});

test("long-running texts: stuck queued message after 5 minutes (300s)", () => {
  const normalQueued = getLongRunningStatus("queued", null, 120);
  assert.equal(normalQueued.isStuckQueued, false);
  assert.equal(normalQueued.hint, undefined);

  const stuckQueued = getLongRunningStatus("queued", null, 301);
  assert.equal(stuckQueued.isStuckQueued, true);
  assert.equal(
    stuckQueued.hint,
    "Waiting for the processing service. If this does not change soon, contact your administrator."
  );
});

test("long-running texts: taking longer than usual after 15 minutes (900s)", () => {
  const under15 = getLongRunningStatus("processing", "chunking", 600);
  assert.equal(under15.isTakingLonger, false);

  const over15 = getLongRunningStatus("processing", "chunking", 905);
  assert.equal(over15.isTakingLonger, true);
  assert.equal(over15.hint, "Taking longer than usual");
});

test("long-running texts: hard stop at 65 minutes (3900s) with check again", () => {
  const at64 = getLongRunningStatus("processing", "indexing", 3899);
  assert.equal(at64.isHardStop, false);

  const at65 = getLongRunningStatus("processing", "indexing", 3900);
  assert.equal(at65.isHardStop, true);
});
