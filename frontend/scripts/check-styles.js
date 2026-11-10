#!/usr/bin/env node

/**
 * scripts/check-styles.js
 * ───────────────────────
 * Style guardrail linter:
 * 1. Guarantees ZERO inline styles (style={{ or style={).
 * 2. Guarantees ZERO direct DOM element.style mutations (e.g. style.overflow).
 * 3. Restricts hex colors exclusively to an exact allow-list of theme/branding utilities.
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const SRC_DIR = path.resolve(__dirname, "../src");

/**
 * Exact relative file paths (within src/) that legitimately define hex colors
 * for theme variables, branding defaults, color algorithms, and test assertions.
 * No whole folders or UI component files are allowed.
 */
export const EXACT_ALLOWED_HEX_FILES = new Set([
  "styles/theme.css",
  "features/branding/types.ts",
  "features/branding/schemas/brandingSchema.ts",
  "features/branding/utils/tokens.ts",
  "features/branding/__tests__/theme_engine.test.ts",
]);

/**
 * Scans a single file's text for style violations.
 * @param {string} content - Source code content
 * @param {string} relPath - Relative file path from src/
 * @returns {Array<{file: string, line: number, rule: string, text: string}>}
 */
export function scanSourceText(content, relPath) {
  const violations = [];
  const lines = content.split(/\r?\n/);
  const normalizedRelPath = relPath.replace(/\\/g, "/");
  const isAllowedForHex = EXACT_ALLOWED_HEX_FILES.has(normalizedRelPath);

  lines.forEach((line, index) => {
    const lineNum = index + 1;
    const trimmed = line.trim();

    // Skip comment lines
    if (trimmed.startsWith("//") || trimmed.startsWith("/*") || trimmed.startsWith("*")) {
      return;
    }

    // 1. Inline style attribute check
    if (/style\s*=\s*\{\{/i.test(line) || /style\s*=\s*\{[a-zA-Z]/i.test(line)) {
      violations.push({
        file: normalizedRelPath,
        line: lineNum,
        rule: "NO inline styles (style={{...}}). Use Tailwind or theme.css classes instead.",
        text: trimmed,
      });
    }

    // 2. Direct DOM element.style mutation check
    if (/\.style\.[a-zA-Z]+\s*=/i.test(line)) {
      violations.push({
        file: normalizedRelPath,
        line: lineNum,
        rule: "NO direct DOM style mutation (.style.prop = ...). Use CSS classes instead.",
        text: trimmed,
      });
    }

    // 3. Disallowed hardcoded hex colors
    if (!isAllowedForHex) {
      const hexMatches = line.match(/#[0-9a-fA-F]{3,8}\b/g);
      if (hexMatches) {
        // Exclude regex definitions
        if (!line.includes("RegExp") && !line.includes("/^#") && !line.includes(".regex")) {
          violations.push({
            file: normalizedRelPath,
            line: lineNum,
            rule: `NO hardcoded hex colors (${hexMatches.join(", ")}). Use semantic theme tokens.`,
            text: trimmed,
          });
        }
      }
    }
  });

  return violations;
}

/**
 * Recursively scans directory for TypeScript / TSX / CSS files.
 * @param {string} dir
 * @param {string} baseDir
 * @returns {Array<{file: string, line: number, rule: string, text: string}>}
 */
export function scanDirectory(dir, baseDir = dir) {
  let violations = [];
  const entries = fs.readdirSync(dir, { withFileTypes: true });

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    const relPath = path.relative(baseDir, fullPath).replace(/\\/g, "/");

    if (entry.isDirectory()) {
      violations = violations.concat(scanDirectory(fullPath, baseDir));
    } else if (
      entry.isFile() &&
      (entry.name.endsWith(".tsx") || entry.name.endsWith(".ts") || entry.name.endsWith(".css"))
    ) {
      const content = fs.readFileSync(fullPath, "utf-8");
      violations = violations.concat(scanSourceText(content, relPath));
    }
  }

  return violations;
}

// Execute as CLI if run directly
const isMain = process.argv[1] && (
  process.argv[1].endsWith("check-styles.js") ||
  process.argv[1].endsWith("check-styles.ts")
);

if (isMain) {
  console.log("Scanning src/ for styling rule violations (restricted exact allow-list)...");
  const violations = scanDirectory(SRC_DIR, SRC_DIR);

  if (violations.length > 0) {
    console.error(`\n❌ Found ${violations.length} style rule violation(s):\n`);
    for (const v of violations) {
      console.error(`  ${v.file}:${v.line} [${v.rule}]`);
      console.error(`    ${v.text}\n`);
    }
    process.exit(1);
  } else {
    console.log("✅ Style check passed: 0 inline styles, 0 direct DOM style mutations, 0 disallowed hex colors.");
    process.exit(0);
  }
}
