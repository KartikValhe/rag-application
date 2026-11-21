import test from "node:test";
import assert from "node:assert/strict";
import {
  isAdminUser,
  isTeacherUser,
  isTeacherOnlyUser,
  getMaterialsBasePath,
} from "../roleUtils.ts";

test("roleUtils: isAdminUser checks isAdmin flag or tenant_admin role", () => {
  assert.equal(isAdminUser({ isAdmin: true }), true);
  assert.equal(isAdminUser({ roles: ["tenant_admin"] }), true);
  assert.equal(isAdminUser({ roles: ["teacher"] }), false);
  assert.equal(isAdminUser(null), false);
  assert.equal(isAdminUser(undefined), false);
});

test("roleUtils: isTeacherUser checks teacher role", () => {
  assert.equal(isTeacherUser({ roles: ["teacher"] }), true);
  assert.equal(isTeacherUser({ roles: ["student"] }), false);
  assert.equal(isTeacherUser(null), false);
});

test("roleUtils: isTeacherOnlyUser is true only for teachers who are not admins", () => {
  assert.equal(isTeacherOnlyUser({ roles: ["teacher"], isAdmin: false }), true);
  assert.equal(isTeacherOnlyUser({ roles: ["teacher", "tenant_admin"] }), false);
  assert.equal(isTeacherOnlyUser({ roles: ["teacher"], isAdmin: true }), false);
  assert.equal(isTeacherOnlyUser({ roles: ["tenant_admin"] }), false);
  assert.equal(isTeacherOnlyUser(null), false);
});

test("roleUtils: getMaterialsBasePath returns /admin/materials for admin routes, /teacher/materials otherwise", () => {
  assert.equal(getMaterialsBasePath("/admin/materials"), "/admin/materials");
  assert.equal(getMaterialsBasePath("/admin/materials/doc-1"), "/admin/materials");
  assert.equal(getMaterialsBasePath("/teacher/materials"), "/teacher/materials");
  assert.equal(getMaterialsBasePath("/teacher/materials/doc-2"), "/teacher/materials");
});
