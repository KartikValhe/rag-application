import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { getErrorFallback, FRIENDLY_ERROR_MESSAGE } from "../errorFallback.ts";

const RAW = "Cannot read properties of undefined (reading 'teacher_name')";

describe("ErrorBoundary fallback content", () => {
  it("outside development shows only the friendly message and a Reload button", () => {
    const fb = getErrorFallback(new TypeError(RAW), false);
    assert.equal(fb.message, FRIENDLY_ERROR_MESSAGE);
    assert.ok(!fb.message.includes("teacher_name"));
    assert.equal(fb.errorDetails, null);
    assert.deepEqual(fb.actions, ["reload"]);
  });

  it("in development also shows the raw error for debugging", () => {
    const fb = getErrorFallback(new TypeError(RAW), true);
    assert.equal(fb.message, FRIENDLY_ERROR_MESSAGE);
    assert.equal(fb.errorDetails, RAW);
    assert.deepEqual(fb.actions, ["dashboard", "reload"]);
  });

  it("handles a missing error object", () => {
    assert.equal(getErrorFallback(null, true).errorDetails, null);
  });
});
