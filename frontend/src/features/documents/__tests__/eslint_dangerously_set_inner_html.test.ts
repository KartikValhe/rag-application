import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ESLint } from "eslint";

test("ESLint forbids dangerouslySetInnerHTML in src", async () => {
  const currentDir = import.meta.dirname ?? path.dirname(fileURLToPath(import.meta.url));
  const frontendDir = path.resolve(currentDir, "../../../..");
  const eslint = new ESLint({ cwd: frontendDir });
  const fakeFilePath = path.resolve(frontendDir, "src/sample.tsx");

  // 1. Selector 1: JSX attribute
  const jsxCode = `const Comp = () => <div dangerouslySetInnerHTML={{ __html: "<p>test</p>" }} />;`;
  const jsxResults = await eslint.lintText(jsxCode, { filePath: fakeFilePath });
  assert.equal(jsxResults.length, 1);
  const jsxViolation = jsxResults[0].messages.find(
    (m) => m.ruleId === "no-restricted-syntax" && m.message.includes("dangerouslySetInnerHTML")
  );
  assert.ok(jsxViolation, "Expected ESLint error on JSX attribute dangerouslySetInnerHTML");
  assert.equal(jsxViolation.severity, 2, "Rule severity must be 'error' (2)");

  // 2. Selector 2: Plain identifier or object property
  const identifierCode = `const config = { dangerouslySetInnerHTML: "unsafe" };`;
  const idResults = await eslint.lintText(identifierCode, { filePath: fakeFilePath });
  assert.equal(idResults.length, 1);
  const idViolation = idResults[0].messages.find(
    (m) => m.ruleId === "no-restricted-syntax" && m.message.includes("dangerouslySetInnerHTML")
  );
  assert.ok(idViolation, "Expected ESLint error on Identifier dangerouslySetInnerHTML");
  assert.equal(idViolation.severity, 2, "Rule severity must be 'error' (2)");

  // 3. Clean code produces zero violations
  const cleanCode = `export const SafeComp = () => <div>Safe text</div>;`;
  const cleanResults = await eslint.lintText(cleanCode, { filePath: fakeFilePath });
  assert.equal(cleanResults.length, 1);
  const cleanErrors = cleanResults[0].messages.filter((m) => m.severity === 2);
  assert.equal(cleanErrors.length, 0, "Clean code must have 0 errors");
});
