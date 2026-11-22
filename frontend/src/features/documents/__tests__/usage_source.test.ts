import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createDocumentsApi } from "../api/documentsApi.ts";

const currentDir = import.meta.dirname ?? path.dirname(fileURLToPath(import.meta.url));
const documentsDir = path.resolve(currentDir, "..");
const srcDir = path.resolve(currentDir, "../../../");

function sourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name !== "__tests__") out.push(...sourceFiles(full));
    } else if (/\.tsx?$/.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

test("usage route: /admin/materials/usage is an AdminRoute that needs settings:manage AND document:view", () => {
  const app = fs.readFileSync(path.join(srcDir, "App.tsx"), "utf8");
  const match = app.match(/<Route\s+path="\/admin\/materials\/usage"[\s\S]*?\/>\s*(?=<Route|<\/Routes>|\{)/);
  assert.ok(match, "Route /admin/materials/usage must exist in App.tsx");
  const block = match[0];
  assert.ok(block.includes("<AdminRoute"), "usage route must use AdminRoute");
  assert.ok(block.includes('permission="settings:manage"'), "usage route must require settings:manage");
  assert.ok(block.includes('permission="document:view"'), "usage route must require document:view");
  assert.ok(block.includes("<MaterialsUsagePage"), "usage route must render MaterialsUsagePage");
});

test("usage route: there is no teacher or student usage route", () => {
  const app = fs.readFileSync(path.join(srcDir, "App.tsx"), "utf8");
  assert.ok(!/path="\/teacher\/[^"]*usage/.test(app), "no teacher usage route");
  assert.ok(!/path="\/student\/[^"]*usage/.test(app), "no student usage route");
});

test("usage menu: the sidebar gets no extra usage entry", () => {
  const sidebar = fs.readFileSync(path.join(srcDir, "components/layout/Sidebar.tsx"), "utf8");
  assert.ok(!/materials\/usage/.test(sidebar), "Sidebar must not link to the usage page");
  assert.ok(!/label:\s*"[^"]*Usage/i.test(sidebar), "Sidebar must not have a Usage item");
});

test("usage: the API function is called only from a hook (never from a page or component)", () => {
  const pattern = /\b(?:api|documentsApi|props\.api)\??\.usage\s*\(/;
  assert.ok(pattern.test("api.usage()"));
  assert.ok(pattern.test("props.api?.usage()"));
  const offenders: string[] = [];
  let hookCalls = 0;
  for (const file of sourceFiles(documentsDir)) {
    const rel = path.normalize(path.relative(documentsDir, file));
    const text = fs.readFileSync(file, "utf8");
    if (rel.startsWith("hooks")) {
      if (pattern.test(text)) hookCalls += 1;
      continue;
    }
    if (rel.startsWith("api")) continue;
    if (pattern.test(text)) offenders.push(rel);
  }
  assert.deepEqual(offenders, []);
  assert.ok(hookCalls >= 1, "a hook must call api.usage()");
});

test("usage: the usage hook does not poll and does not refetch on its own", () => {
  const hook = fs.readFileSync(path.join(documentsDir, "hooks/useDocumentUsage.ts"), "utf8");
  assert.ok(!/refetchInterval/.test(hook), "no refetchInterval");
  assert.ok(!/setInterval/.test(hook), "no setInterval");
  assert.ok(/refetchOnWindowFocus:\s*false/.test(hook), "no refetch on window focus");
  assert.ok(/refetchOnReconnect:\s*false/.test(hook), "no refetch on reconnect");
});

test("usage: the exported API object has usage and it is not a mutation by name", () => {
  const api = createDocumentsApi({} as never);
  assert.ok(Object.keys(api).includes("usage"));
});

test("usage page: no inline styles, no hex colours, no raw HTML", () => {
  for (const rel of ["pages/MaterialsUsagePage.tsx", "utils/usageView.ts", "hooks/useDocumentUsage.ts"]) {
    const text = fs.readFileSync(path.join(documentsDir, rel), "utf8");
    assert.ok(!/\bstyle=/.test(text), `${rel}: inline style`);
    assert.ok(!/#[0-9a-fA-F]{3,8}\b/.test(text), `${rel}: hex colour`);
    assert.ok(!/dangerouslySetInnerHTML|innerHTML/.test(text), `${rel}: raw HTML`);
  }
});
