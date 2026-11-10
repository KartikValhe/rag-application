/**
 * scripts/__tests__/check_styles.test.js
 * ──────────────────────────────────────
 * Unit tests verifying that check-styles.js correctly flags:
 * 1. Components containing inline style attributes (style={{...}}).
 * 2. Code mutating element.style (DOM styles).
 * 3. Components with hardcoded hex colors.
 * 4. Legitimate theme/branding files in the exact allow-list pass.
 */

import { test } from "node:test";
import assert from "node:assert";
import { scanSourceText, EXACT_ALLOWED_HEX_FILES } from "../check-styles.js";

test("Style Guardrail: Clean component passes with zero violations", () => {
  const cleanComponent = `
    import React from "react";
    export const Button = () => (
      <button className="bg-primary hover:bg-primary-hover text-primary-foreground min-h-[44px] px-4 py-2 rounded-md">
        Click Me
      </button>
    );
  `;
  const violations = scanSourceText(cleanComponent, "components/Button.tsx");
  assert.strictEqual(violations.length, 0);
});

test("Style Guardrail: Component with style={{...}} attribute fails", () => {
  const badComponent = `
    import React from "react";
    export const Header = () => (
      <div style={{ backgroundColor: "blue", minHeight: 44 }}>
        <h1>Title</h1>
      </div>
    );
  `;
  const violations = scanSourceText(badComponent, "components/Header.tsx");
  assert.ok(violations.length > 0, "Expected violations for style={{...}}");
  assert.ok(
    violations.some((v) => v.rule.includes("NO inline styles")),
    "Expected violation rule to mention NO inline styles"
  );
});

test("Style Guardrail: Component with style={stylesObj} fails", () => {
  const badComponent = `
    import React from "react";
    export const Box = ({ dynamicStyles }) => (
      <div style={dynamicStyles}>
        Content
      </div>
    );
  `;
  const violations = scanSourceText(badComponent, "components/Box.tsx");
  assert.ok(violations.length > 0, "Expected violation for style={...}");
  assert.ok(violations.some((v) => v.rule.includes("NO inline styles")));
});

test("Style Guardrail: Direct DOM element.style mutation fails", () => {
  const badHook = `
    import { useEffect } from "react";
    export function useBadScrollLock() {
      useEffect(() => {
        document.body.style.overflow = "hidden";
        return () => {
          document.body.style.overflow = "auto";
        };
      }, []);
    }
  `;
  const violations = scanSourceText(badHook, "hooks/useBadScrollLock.ts");
  assert.ok(violations.length >= 2, "Expected violations for document.body.style.overflow");
  assert.ok(
    violations.some((v) => v.rule.includes("NO direct DOM style mutation")),
    "Expected violation rule to mention direct DOM style mutation"
  );
});

test("Style Guardrail: Component containing hardcoded hex colors fails", () => {
  const badColorComponent = `
    import React from "react";
    export const Card = () => (
      <div className="bg-[#1e293b] text-[#ffffff] border border-[#334155]">
        Card Content
      </div>
    );
  `;
  const violations = scanSourceText(badColorComponent, "features/cards/Card.tsx");
  assert.ok(violations.length >= 1, "Expected violations for hardcoded hex colors");
  assert.ok(
    violations.some((v) => v.rule.includes("NO hardcoded hex colors")),
    "Expected violation rule to mention NO hardcoded hex colors"
  );
});

test("Style Guardrail: Allow-listed theme utilities are permitted to define hex colors", () => {
  for (const allowedFile of EXACT_ALLOWED_HEX_FILES) {
    const codeWithHex = `
      export const PALETTE = {
        primary: "#2563eb",
        surface: "#0f172a",
      };
    `;
    const violations = scanSourceText(codeWithHex, allowedFile);
    assert.strictEqual(
      violations.length,
      0,
      `Expected ${allowedFile} to be permitted to contain hex colors`
    );
  }
});

