import test from "node:test";
import assert from "node:assert/strict";
import { ESLint } from "eslint";

test("ESLint forbids dangerouslySetInnerHTML in src", async () => {
  const eslint = new ESLint();
  const code = `const Comp = () => <div dangerouslySetInnerHTML={{ __html: "<p>test</p>" }} />;`;
  const results = await eslint.lintText(code, { filePath: "src/sample.tsx" });
  assert.equal(results.length, 1);
  const messages = results[0].messages;
  const violation = messages.find((m) => m.ruleId === "no-restricted-syntax" && m.message.includes("dangerouslySetInnerHTML"));
  assert.ok(violation, "Expected ESLint error on dangerouslySetInnerHTML");
});
