/**
 * frontend/src/features/branding/__tests__/theme_engine.test.ts
 * ─────────────────────────────────────────────────────────────
 * Unit tests for Phase 3b Theme Engine:
 * - Color manipulation & WCAG AA contrast calculations
 * - Light and Dark mode dynamic token derivation
 * - System font and border radius allow-list enforcement
 * - CSS rule generation
 * - LocalStorage versioned caching and resilience
 */

import test from "node:test";
import assert from "node:assert/strict";

import {
  isValidHex,
  hexToRgb,
  rgbToHex,
  calculateRelativeLuminance,
  calculateContrastRatio,
  mixColors,
  getAccessibleForeground,
  computePalette,
  generateThemeCss,
  RADIUS_ALLOWLIST,
  FONT_FAMILY_ALLOWLIST,
} from "../utils/tokens.ts";

import {
  getBrandingStorageKey,
  getStoredBranding,
  saveStoredBranding,
  clearStoredBranding,
} from "../utils/storage.ts";

import type { DesignTokens, PublicBranding } from "../types.ts";
import { DEFAULT_TOKENS } from "../types.ts";

test("isValidHex: validates 6-character hex strings correctly", () => {
  assert.equal(isValidHex("#2563eb"), true);
  assert.equal(isValidHex("#FFFFFF"), true);
  assert.equal(isValidHex("#000000"), true);
  assert.equal(isValidHex("2563eb"), false, "Missing leading # should be false");
  assert.equal(isValidHex("#fff"), false, "3-digit hex should be false");
  assert.equal(isValidHex("#gggggg"), false, "Non-hex characters should be false");
  assert.equal(isValidHex(""), false);
  assert.equal(isValidHex(null), false);
  assert.equal(isValidHex(undefined), false);
});

test("hexToRgb & rgbToHex: accurately converts RGB channels", () => {
  const blue = hexToRgb("#2563eb");
  assert.deepEqual(blue, { r: 37, g: 99, b: 235 });

  const hexAgain = rgbToHex(37, 99, 235);
  assert.equal(hexAgain.toLowerCase(), "#2563eb");

  assert.equal(hexToRgb("invalid"), null);
});

test("mixColors: mixes two hex colors by weight", () => {
  const midGrey = mixColors("#000000", "#ffffff", 0.5);
  assert.equal(midGrey, "#808080");

  const fullTarget = mixColors("#123456", "#ffffff", 1.0);
  assert.equal(fullTarget, "#ffffff");
});

test("calculateRelativeLuminance: white is 1.0, black is 0.0", () => {
  const whiteLum = calculateRelativeLuminance("#ffffff");
  const blackLum = calculateRelativeLuminance("#000000");

  assert.ok(Math.abs(whiteLum - 1.0) < 0.001);
  assert.ok(Math.abs(blackLum - 0.0) < 0.001);
});

test("calculateContrastRatio: white on black is 21:1", () => {
  const ratio = calculateContrastRatio("#ffffff", "#000000");
  assert.ok(Math.abs(ratio - 21.0) < 0.1, `Expected ~21:1, got ${ratio}`);
});

test("getAccessibleForeground: guarantees >= 4.5:1 WCAG AA text contrast", () => {
  // Deep blue: white text has high contrast (> 4.5)
  const fgForBlue = getAccessibleForeground("#1e40af", false);
  const ratioWhite = calculateContrastRatio("#1e40af", fgForBlue);
  assert.ok(ratioWhite >= 4.5, `Contrast on #1e40af must be >= 4.5, got ${ratioWhite}`);
  assert.equal(fgForBlue, "#ffffff");

  // Pale yellow/light background: dark text must be chosen
  const fgForYellow = getAccessibleForeground("#fef08a", false);
  const ratioDark = calculateContrastRatio("#fef08a", fgForYellow);
  assert.ok(ratioDark >= 4.5, `Contrast on #fef08a must be >= 4.5, got ${ratioDark}`);
  assert.equal(fgForYellow, "#0f172a");
});

test("computePalette: derives light and dark variants cleanly", () => {
  const tokens: DesignTokens = {
    primary: "#2563eb",
    secondary: "#475569",
    accent: "#06b6d4",
    radius: "md",
    font_family: "system-ui",
  };

  const light = computePalette(tokens, false);
  assert.equal(light.primary, "#2563eb");
  assert.notEqual(light.primaryHover, light.primary, "Hover variant should differ from primary");
  assert.notEqual(light.primaryActive, light.primaryHover, "Active variant should differ from hover");
  assert.ok(light.primaryLight.startsWith("#"), "Light surface tint must be a valid hex");
  assert.ok(["#ffffff", "#0f172a"].includes(light.primaryForeground), "Foreground must be high-contrast");

  const dark = computePalette(tokens, true);
  assert.ok(dark.primary.startsWith("#"));
  assert.notEqual(dark.primaryHover, dark.primary, "Dark hover should differ");
  assert.ok(["#ffffff", "#090d16"].includes(dark.primaryForeground));
});

test("computePalette: dark mode adapts dark primary for readability on dark surface", () => {
  // A very dark primary color (navy #0f172a)
  const darkNavyTokens: DesignTokens = {
    primary: "#1e3a8a",
    secondary: "#475569",
    accent: "#06b6d4",
    radius: "md",
    font_family: "system-ui",
  };

  const dark = computePalette(darkNavyTokens, true);
  // Dark mode primary must have good contrast against dark background #090d16
  const contrastAgainstDark = calculateContrastRatio(dark.primary, "#090d16");
  assert.ok(
    contrastAgainstDark >= 3.0,
    `Dark mode primary should contrast at least 3.0:1 against dark surface, got ${contrastAgainstDark}`
  );
});

test("generateThemeCss: outputs valid CSS variables with :root and .dark blocks", () => {
  const tokens: DesignTokens = {
    primary: "#059669", // Emerald
    secondary: "#475569",
    accent: "#06b6d4",
    radius: "lg",
    font_family: "sans",
  };

  const css = generateThemeCss(tokens);
  assert.ok(css.includes(":root {"), "Must contain :root block");
  assert.ok(css.includes(".dark {"), "Must contain .dark block");
  assert.ok(css.includes("--color-primary: #059669;"), "Light mode primary must match token");
  assert.ok(css.includes("--radius-lg: 0.75rem;"), "Must set radii according to allow-list");
  assert.ok(css.includes(FONT_FAMILY_ALLOWLIST.sans), "Must include system font stack");
});

test("generateThemeCss: safely falls back if unknown radius or font_family is supplied", () => {
  const invalidTokens = {
    primary: "#2563eb",
    secondary: "#475569",
    accent: "#06b6d4",
    radius: "evil_radius" as any,
    font_family: "comic_sans" as any,
  };

  const css = generateThemeCss(invalidTokens);
  // Must fall back to "md" radius and "system-ui" font
  assert.ok(css.includes(`--radius-md: ${RADIUS_ALLOWLIST.md.md};`));
  assert.ok(css.includes(FONT_FAMILY_ALLOWLIST["system-ui"]));
});

test("LocalStorage storage helpers: save, load, and versioning", () => {
  // Mock window.localStorage in Node test environment
  const store: Record<string, string> = {};
  const mockLocalStorage = {
    getItem: (key: string) => store[key] || null,
    setItem: (key: string, value: string) => {
      store[key] = value;
    },
    removeItem: (key: string) => {
      delete store[key];
    },
  };

  (globalThis as any).window = { localStorage: mockLocalStorage };

  const testBranding: PublicBranding = {
    display_name: "Test Academy",
    tagline: "Excellence in Testing",
    footer_text: "© 2026 Test Academy",
    support_email: "support@test.edu",
    tokens: DEFAULT_TOKENS,
    logo_url: "https://example.com/logo.png",
    favicon_url: "https://example.com/favicon.ico",
    version: 3,
  };

  const slug = "test-tenant";
  assert.equal(getBrandingStorageKey("Test-Tenant"), "app_branding_test-tenant");
  saveStoredBranding(slug, testBranding, 'W/"test-3"');

  const loaded = getStoredBranding(slug);
  assert.ok(loaded !== null, "Stored branding should be found");
  assert.equal(loaded?.version, 3);
  assert.equal(loaded?.etag, 'W/"test-3"');
  assert.equal(loaded?.branding.display_name, "Test Academy");
  assert.ok(loaded?.css?.includes("--color-primary"), "Precomputed CSS must be stored in cache");

  clearStoredBranding(slug);
  assert.equal(getStoredBranding(slug), null, "Should return null after clearing");

  // Resilience: when getItem throws, getStoredBranding returns null without crashing
  mockLocalStorage.getItem = () => {
    throw new Error("Private browsing quota exceeded");
  };
  assert.equal(getStoredBranding(slug), null, "Must safely catch storage exceptions");
});
