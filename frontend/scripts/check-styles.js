#!/usr/bin/env node

/**
 * check-styles.js
 * ────────────────
 * Style guardrail script:
 * 1. Checks that NO components use inline styles (style={{ or style={).
 * 2. Checks that NO code performs direct DOM element.style mutations (e.g. style.overflow).
 * 3. Checks that NO UI code uses hard-coded hex colors outside designated branding token utilities.
 */

import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const srcDir = path.resolve(__dirname, "../src");

let violations = [];

const allowedHexFiles = [
  "features/branding/utils/colorUtils.ts",
  "features/branding/utils/tokens.ts",
  "features/branding/schemas/brandingSchema.ts",
  "features/branding/types.ts",
  "features/branding/__tests__/theme_engine.test.ts",
  "features/branding/components/BrandingEditorPage.tsx", // Allow input placeholders/defaults for the color picker
  "styles/theme.css",
];

function scanDirectory(dir) {
  const entries = fs.readdirSync(dir, { withFileTypes: true });

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    const relPath = path.relative(srcDir, fullPath).replace(/\\/g, "/");

    if (entry.isDirectory()) {
      scanDirectory(fullPath);
    } else if (entry.isFile() && (entry.name.endsWith(".tsx") || entry.name.endsWith(".ts"))) {
      checkFile(fullPath, relPath);
    }
  }
}

function checkFile(filePath, relPath) {
  const content = fs.readFileSync(filePath, "utf-8");
  const lines = content.split(/\r?\n/);

  const isAllowedForHex = allowedHexFiles.some((allowed) => relPath.endsWith(allowed));

  lines.forEach((line, index) => {
    const lineNum = index + 1;
    const trimmed = line.trim();

    // Ignore comments
    if (trimmed.startsWith("//") || trimmed.startsWith("/*") || trimmed.startsWith("*")) {
      return;
    }

    // 1. Inline style check
    if (/style\s*=\s*\{\{/i.test(line) || /style\s*=\s*\{[a-zA-Z]/i.test(line)) {
      violations.push({
        file: relPath,
        line: lineNum,
        rule: "NO inline styles (style={{...}})",
        text: trimmed,
      });
    }

    // 2. Direct DOM style mutation check
    if (/\.style\.[a-zA-Z]+\s*=/i.test(line)) {
      violations.push({
        file: relPath,
        line: lineNum,
        rule: "NO direct DOM style mutation (.style.prop = ...). Use CSS classes instead.",
        text: trimmed,
      });
    }

    // 3. Hardcoded hex colors in standard UI code
    if (!isAllowedForHex) {
      const hexMatch = line.match(/#[0-9a-fA-F]{3,8}\b/g);
      if (hexMatch) {
        // Exclude TypeScript hex regex definitions if any
        if (!line.includes("RegExp") && !line.includes("/^#") && !line.includes(".regex")) {
          violations.push({
            file: relPath,
            line: lineNum,
            rule: `NO hardcoded hex colors (${hexMatch.join(", ")}). Use semantic theme tokens.`,
            text: trimmed,
          });
        }
      }
    }
  });
}

console.log("Scanning src/ for styling rule violations...");
scanDirectory(srcDir);

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

