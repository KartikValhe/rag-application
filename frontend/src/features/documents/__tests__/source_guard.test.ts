import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createDocumentsApi } from "../api/documentsApi.ts";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const documentsDir = path.resolve(__dirname, "..");

const NON_MUTATING_METHODS = new Set([
  "list",
  "get",
  "limits",
  "status",
  "flags",
  "search",
]);

/**
 * Pure function to find policy and API usage violations in source code.
 */
export function findViolations(source: string, fileName = "snippet"): string[] {
  const violations: string[] = [];

  // 1. Direct can("x") or can ("x") calls
  if (/\bcan\s*\(/.test(source)) {
    violations.push(`${fileName}: calls can() directly instead of using documentPolicy`);
  }

  // 2. Direct hasRole("x") calls
  if (/\bhasRole\s*\(/.test(source)) {
    violations.push(`${fileName}: calls hasRole() directly`);
  }

  // 3. .roles.includes(
  if (/\.roles\.includes\s*\(/.test(source)) {
    violations.push(`${fileName}: calls .roles.includes() directly`);
  }

  // 4. .permissions.includes(
  if (/\.permissions\.includes\s*\(/.test(source)) {
    violations.push(`${fileName}: calls .permissions.includes() directly`);
  }

  // 5. Any import of buildUploadFormData
  if (/\bimport\b[^;]*\bbuildUploadFormData\b/.test(source)) {
    violations.push(`${fileName}: imports buildUploadFormData directly`);
  }

  // 6. Mutating API calls
  // Derive mutating method names from exported createDocumentsApi (everything except list, get, limits, status, flags, search)
  const sampleApi = createDocumentsApi({} as any);
  const derivedMutating = Object.keys(sampleApi).filter((k) => !NON_MUTATING_METHODS.has(k));
  const mutatingMethods = Array.from(
    new Set([...derivedMutating, "upload", "publish", "retry", "delete", "deleteVersion", "deleteDocument"])
  );

  for (const method of mutatingMethods) {
    if (method === "delete") {
      // .delete-style mutating calls of the documents API
      const pattern = /\b(?:api|documentsApi|props\.api)\??\.delete\w*\s*\(|\.(?:deleteDocument|deleteVersion)\s*\(/g;
      if (pattern.test(source)) {
        violations.push(`${fileName}: directly calls documentsApi.${method}()`);
      }
    } else if (method === "publish" || method === "retry") {
      // .publish( or .retry( or api.publish( etc.
      const pattern = new RegExp(`(?:\\b(?:api|documentsApi|props\\.api)\\??\\.${method}\\s*\\(|\\.${method}\\s*\\()`, "g");
      if (pattern.test(source)) {
        violations.push(`${fileName}: directly calls ${method}()`);
      }
    } else {
      const pattern = new RegExp(`\\b(?:api|documentsApi|props\\.api)\\??\\.${method}\\s*\\(`, "g");
      if (pattern.test(source)) {
        violations.push(`${fileName}: directly calls documentsApi.${method}()`);
      }
    }
  }

  return violations;
}

function getAllSourceFiles(dir: string): string[] {
  const results: string[] = [];
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "__tests__" && entry.name !== "node_modules") {
        results.push(...getAllSourceFiles(fullPath));
      }
    } else if (
      entry.isFile() &&
      (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx"))
    ) {
      results.push(fullPath);
    }
  }
  return results;
}

const EXCLUDED_FILES = new Set([
  path.normalize("api/documentsApi.ts"),
  path.normalize("api/documentsClient.ts"),
  path.normalize("hooks/useDocumentMutations.ts"),
]);

test("findViolations: flags all required injected bad snippets", () => {
  const badSnippets = [
    `can("document:upload")`,
    `can ("x")`,
    `hasRole("teacher")`,
    `user.roles.includes("tenant_admin")`,
    `user.permissions.includes("document:upload")`,
    `documentsApi.upload(formData)`,
    `props.api?.upload(form)`,
    `api.upload(form)`,
    `.publish("doc-1")`,
    `.retry("doc-1")`,
    `api.delete("doc-1")`,
    `props.api?.delete("doc-1")`,
    `documentsApi.delete("doc-1")`,
    `api.deleteVersion("v-1")`,
    `import { buildUploadFormData } from "../api/documentsApi";`,
  ];

  for (const snippet of badSnippets) {
    const violations = findViolations(snippet);
    assert.ok(
      violations.length > 0,
      `Expected snippet to be flagged as violation: ${snippet}`
    );
  }
});

test("findViolations: passes clean snippets without flagging", () => {
  const cleanSnippets = [
    `next.delete("class");`,
    `next.delete("subject");`,
    `delete copy.title;`,
    `const can = policy.can;`,
    `canView ? true : false;`,
    `api.list({ limit: 20 });`,
    `api.get(docId);`,
    `api.limits();`,
    `api.status(docId, vid);`,
    `props.api?.list();`,
    `props.api?.get("123");`,
    `const isRetryable = true;`,
    `const pub = doc.published_version;`,
    `if (status === "ready") { return true; }`,
  ];

  for (const snippet of cleanSnippets) {
    const violations = findViolations(snippet);
    assert.deepEqual(
      violations,
      [],
      `Expected clean snippet NOT to be flagged: ${snippet}`
    );
  }
});

test("Source guard: scan EVERY .ts and .tsx file under features/documents (except documentsApi.ts, documentsClient.ts, useDocumentMutations.ts)", () => {
  const allFiles = getAllSourceFiles(documentsDir);
  assert.ok(allFiles.length > 0, "Found no .ts or .tsx files under features/documents");

  const violations: string[] = [];
  const scannedFiles: string[] = [];

  for (const file of allFiles) {
    const relPath = path.normalize(path.relative(documentsDir, file));
    if (EXCLUDED_FILES.has(relPath)) {
      continue;
    }
    scannedFiles.push(relPath);
    const content = fs.readFileSync(file, "utf8");
    const fileViolations = findViolations(content, relPath);
    violations.push(...fileViolations);
  }

  // Ensure hooks and utils are included in scannedFiles
  assert.ok(
    scannedFiles.some((f) => f.includes("hooks")),
    "Hooks must be included in scanned files"
  );
  assert.ok(
    scannedFiles.some((f) => f.includes("utils")),
    "Utils must be included in scanned files"
  );
  assert.ok(
    scannedFiles.some((f) => f.includes("pages")),
    "Pages must be included in scanned files"
  );
  assert.ok(
    scannedFiles.some((f) => f.includes("components")),
    "Components must be included in scanned files"
  );

  assert.deepEqual(violations, []);
});

test("Source guard: every mutating method of the exported API object is flagged when a component calls it", () => {
  const api = createDocumentsApi({} as any);
  const mutating = Object.keys(api).filter((k) => !NON_MUTATING_METHODS.has(k));
  assert.ok(mutating.includes("upload"));
  assert.ok(mutating.includes("retryVersion"), "retryVersion must be treated as mutating");
  assert.ok(mutating.includes("deleteVersion"), "deleteVersion must be treated as mutating");
  for (const method of mutating) {
    for (const form of [`api.${method}(x)`, `props.api?.${method}(x)`, `documentsApi.${method}(x)`]) {
      assert.ok(findViolations(form).length > 0, `${form} must be flagged in a component`);
    }
  }
});
