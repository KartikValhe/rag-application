// API drift guard (plan Q5). Reads docs/api/phase5.md as text (no network, no backend code) and
// fails, saying WHAT drifted, when the frontend's assumptions no longer match it.
import test from "node:test";
import assert from "node:assert/strict";
import { collectDocFacts, collectFrontendFacts, findDrift, readDoc } from "./apiDriftCheck.ts";

test("the frontend still matches docs/api/phase5.md (error codes, permissions, routes, fields, limits)", async () => {
  const problems = findDrift(collectDocFacts(readDoc()), await collectFrontendFacts());
  assert.equal(
    problems.length,
    0,
    `The frontend drifted from docs/api/phase5.md (${problems.length} problem${problems.length === 1 ? "" : "s"}):\n- ${problems.join("\n- ")}`
  );
});
