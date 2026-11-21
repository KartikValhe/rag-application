import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const currentDir = import.meta.dirname ?? path.dirname(fileURLToPath(import.meta.url));
const srcDir = path.resolve(currentDir, "../../../");

test("student isolation: no file under features/student imports features/documents", () => {
  const studentDir = path.join(srcDir, "features/student");
  const files = fs.readdirSync(studentDir);
  for (const file of files) {
    const fullPath = path.join(studentDir, file);
    if (fs.statSync(fullPath).isFile()) {
      const content = fs.readFileSync(fullPath, "utf-8");
      assert.ok(
        !content.includes("features/documents"),
        `${file} must not import features/documents`
      );
      assert.ok(
        !content.includes("@/features/documents"),
        `${file} must not import @/features/documents`
      );
    }
  }
});

test("student isolation: sidebar config for student section has no materials item", () => {
  const sidebarPath = path.join(srcDir, "components/layout/Sidebar.tsx");
  const content = fs.readFileSync(sidebarPath, "utf-8");

  // Extract Student Portal section
  const studentSectionMatch = content.match(/title:\s*"Student Portal"[\s\S]*?items:\s*\[([\s\S]*?)\]/);
  assert.ok(studentSectionMatch, "Student Portal section must exist in Sidebar.tsx");
  const studentItems = studentSectionMatch[1];
  assert.ok(
    !studentItems.toLowerCase().includes("material"),
    "Student Portal items must not contain any materials entry"
  );
});

test("route guards: every /teacher/materials* and /admin/materials* route is wrapped in a guard", () => {
  const appPath = path.join(srcDir, "App.tsx");
  const content = fs.readFileSync(appPath, "utf-8");

  // Verify /teacher/materials exists and is inside TeacherRoute
  const teacherMaterialsMatch = content.match(/<Route\s+path="\/teacher\/materials"[\s\S]*?element=\{([\s\S]*?)\}/);
  assert.ok(teacherMaterialsMatch, "Route /teacher/materials must exist in App.tsx");
  assert.ok(
    teacherMaterialsMatch[1].includes("<TeacherRoute"),
    "/teacher/materials must be wrapped in TeacherRoute"
  );
  assert.ok(
    teacherMaterialsMatch[1].includes('permission="document:view"'),
    "/teacher/materials must require document:view"
  );

  // Verify /admin/materials exists and is inside AdminRoute
  const adminMaterialsMatch = content.match(/<Route\s+path="\/admin\/materials"[\s\S]*?element=\{([\s\S]*?)\}/);
  assert.ok(adminMaterialsMatch, "Route /admin/materials must exist in App.tsx");
  assert.ok(
    adminMaterialsMatch[1].includes("<AdminRoute"),
    "/admin/materials must be wrapped in AdminRoute"
  );
  assert.ok(
    adminMaterialsMatch[1].includes('permission="document:view"'),
    "/admin/materials must require document:view"
  );

  // Verify /teacher/materials/:documentId exists and is inside TeacherRoute
  const teacherDetailMatch = content.match(/<Route\s+path="\/teacher\/materials\/:documentId"[\s\S]*?element=\{([\s\S]*?)\}/);
  assert.ok(teacherDetailMatch, "Route /teacher/materials/:documentId must exist in App.tsx");
  assert.ok(
    teacherDetailMatch[1].includes("<TeacherRoute"),
    "/teacher/materials/:documentId must be wrapped in TeacherRoute"
  );
  assert.ok(
    teacherDetailMatch[1].includes('permission="document:view"'),
    "/teacher/materials/:documentId must require document:view"
  );

  // Verify /admin/materials/:documentId exists and is inside AdminRoute
  const adminDetailMatch = content.match(/<Route\s+path="\/admin\/materials\/:documentId"[\s\S]*?element=\{([\s\S]*?)\}/);
  assert.ok(adminDetailMatch, "Route /admin/materials/:documentId must exist in App.tsx");
  assert.ok(
    adminDetailMatch[1].includes("<AdminRoute"),
    "/admin/materials/:documentId must be wrapped in AdminRoute"
  );
  assert.ok(
    adminDetailMatch[1].includes('permission="document:view"'),
    "/admin/materials/:documentId must require document:view"
  );
});
