import test from "node:test";
import assert from "node:assert/strict";
import { getDocumentActions, getFailedVersionHint, type DocumentActionId, type DocumentPolicyAction } from "../utils/documentPolicy.ts";
import type { DocumentItem, DocumentVersionItem } from "../schemas/documentSchemas.ts";

function createMockDoc(overrides: Partial<DocumentItem> = {}): DocumentItem {
  return {
    id: "doc-1",
    title: "Document 1",
    description: null,
    subject: { id: "s1", name: "Math" },
    classes: [{ id: "c1", name: "Class 10" }],
    created_by: "u1",
    created_at: "2026-11-20T10:00:00Z",
    updated_at: "2026-11-20T10:00:00Z",
    latest_version: null,
    published_version: null,
    can_manage: true,
    blocked_reason: null,
    ...overrides,
  };
}

function createMockVersion(overrides: Partial<DocumentVersionItem> = {}): DocumentVersionItem {
  return {
    id: "v-1",
    document_id: "doc-1",
    version_no: 1,
    status: "ready",
    stage: null,
    error_code: null,
    file_kind: "pdf",
    original_filename: "doc.pdf",
    size_bytes: 1024,
    page_count: 5,
    chunk_count: 10,
    flagged_chunk_count: 0,
    created_at: "2026-11-20T10:00:00Z",
    published_at: null,
    retryable: null,
    warnings: [],
    ...overrides,
  };
}

function getAction(actions: DocumentPolicyAction[], id: DocumentActionId): DocumentPolicyAction {
  const action = actions.find((a) => a.id === id);
  assert.ok(action, `Action ${id} not found`);
  return action;
}

test("documentPolicy: tenant admin has full access", () => {
  const can = (_perm: string) => true;
  const readyVersion = createMockVersion({ status: "ready" });
  const doc = createMockDoc({ latest_version: readyVersion });

  const actions = getDocumentActions({ can, document: doc, version: readyVersion });
  assert.equal(getAction(actions, "view").visible, true);
  assert.equal(getAction(actions, "upload").visible, true);
  assert.equal(getAction(actions, "publish").visible, true);
  assert.equal(getAction(actions, "publish").enabled, true);
  assert.equal(getAction(actions, "delete_version").visible, true);
  assert.equal(getAction(actions, "delete_version").enabled, true);
  assert.equal(getAction(actions, "delete_document").visible, true);
  assert.equal(getAction(actions, "delete_document").enabled, true);
  assert.equal(getAction(actions, "test_search").visible, true);
  assert.equal(getAction(actions, "usage").visible, true);
});

test("documentPolicy: admin via wildcard permission has full access", () => {
  // Real usePermission logic for admin: user has isAdmin: true or '*' permission
  const permissionsSet = new Set(["*"]);
  const can = (perm: string) => permissionsSet.has("*") || permissionsSet.has(perm);

  const readyVersion = createMockVersion({ status: "ready" });
  const doc = createMockDoc({ latest_version: readyVersion });
  const actions = getDocumentActions({ can, document: doc, version: readyVersion });

  assert.equal(getAction(actions, "view").visible, true);
  assert.equal(getAction(actions, "upload").visible, true);
  assert.equal(getAction(actions, "publish").enabled, true);
  assert.equal(getAction(actions, "usage").visible, true);
});

test("documentPolicy: teacher with can_manage true", () => {
  const teacherPerms = new Set(["document:upload", "document:delete", "document:view", "document:publish"]);
  const can = (perm: string) => teacherPerms.has(perm);

  const readyVersion = createMockVersion({ status: "ready" });
  const doc = createMockDoc({ latest_version: readyVersion, can_manage: true });

  const actions = getDocumentActions({ can, document: doc, version: readyVersion });
  assert.equal(getAction(actions, "view").visible, true);
  assert.equal(getAction(actions, "upload").visible, true);
  assert.equal(getAction(actions, "publish").visible, true);
  assert.equal(getAction(actions, "publish").enabled, true);
  assert.equal(getAction(actions, "delete_document").visible, true);
  assert.equal(getAction(actions, "delete_document").enabled, true);
  assert.equal(getAction(actions, "usage").visible, false, "Usage hidden for teachers");

  // New version should be disabled while latest version is ready
  assert.equal(getAction(actions, "new_version").visible, true);
  assert.equal(getAction(actions, "new_version").enabled, false);
  assert.equal(getAction(actions, "new_version").reason, "Publish or delete the draft first");
});

test("documentPolicy: teacher with can_manage false shows lock reason", () => {
  const teacherPerms = new Set(["document:upload", "document:delete", "document:view", "document:publish"]);
  const can = (perm: string) => teacherPerms.has(perm);

  const readyVersion = createMockVersion({ status: "ready" });
  const doc = createMockDoc({
    latest_version: readyVersion,
    can_manage: false,
    blocked_reason: "document_manage_requires_all_classes",
  });

  const actions = getDocumentActions({ can, document: doc, version: readyVersion });
  const lockReason = "You teach this subject in only some of the linked classes, so you can only view it";

  assert.equal(getAction(actions, "view").visible, true);
  assert.equal(getAction(actions, "publish").visible, true);
  assert.equal(getAction(actions, "publish").enabled, false);
  assert.equal(getAction(actions, "publish").reason, lockReason);

  assert.equal(getAction(actions, "delete_version").visible, true);
  assert.equal(getAction(actions, "delete_version").enabled, false);
  assert.equal(getAction(actions, "delete_version").reason, lockReason);

  assert.equal(getAction(actions, "delete_document").visible, true);
  assert.equal(getAction(actions, "delete_document").enabled, false);
  assert.equal(getAction(actions, "delete_document").reason, lockReason);

  assert.equal(getAction(actions, "new_version").visible, true);
  assert.equal(getAction(actions, "new_version").enabled, false);
  assert.equal(getAction(actions, "new_version").reason, lockReason);
});

test("documentPolicy: custom role with only document:view", () => {
  const can = (perm: string) => perm === "document:view";
  const readyVersion = createMockVersion({ status: "ready" });
  const doc = createMockDoc({ latest_version: readyVersion });

  const actions = getDocumentActions({ can, document: doc, version: readyVersion });
  assert.equal(getAction(actions, "view").visible, true);
  assert.equal(getAction(actions, "test_search").visible, true);

  assert.equal(getAction(actions, "upload").visible, false);
  assert.equal(getAction(actions, "new_version").visible, false);
  assert.equal(getAction(actions, "publish").visible, false);
  assert.equal(getAction(actions, "delete_version").visible, false);
  assert.equal(getAction(actions, "delete_document").visible, false);
  assert.equal(getAction(actions, "retry").visible, false);
  assert.equal(getAction(actions, "usage").visible, false);
});

test("documentPolicy: student has zero visible actions", () => {
  const can = (_perm: string) => false;
  const readyVersion = createMockVersion({ status: "ready" });
  const doc = createMockDoc({ latest_version: readyVersion });

  const actions = getDocumentActions({ can, document: doc, version: readyVersion });
  for (const a of actions) {
    assert.equal(a.visible, false, `Action ${a.id} must be hidden from student`);
  }
});

test("documentPolicy: signed-out user has zero visible actions", () => {
  const can = (_perm: string) => false;
  const doc = createMockDoc({ latest_version: createMockVersion({ status: "published" }) });

  const actions = getDocumentActions({ can, document: doc });
  for (const a of actions) {
    assert.equal(a.visible, false, `Action ${a.id} must be hidden for signed-out user`);
  }
});

test("documentPolicy: BLOCKING item 1 - published v2 passed as version while latest is ready v3 -> New version is disabled", () => {
  const can = (_perm: string) => true;
  const publishedV2 = createMockVersion({ version_no: 2, status: "published" });
  const readyV3 = createMockVersion({ version_no: 3, status: "ready" });
  const doc = createMockDoc({ latest_version: readyV3, published_version: publishedV2, can_manage: true });

  // When viewing row for published v2, New version must be decided from latest_version (ready v3)
  const actions = getDocumentActions({ can, document: doc, version: publishedV2 });
  const newVersionAction = getAction(actions, "new_version");
  assert.equal(newVersionAction.visible, true);
  assert.equal(newVersionAction.enabled, false);
  assert.equal(newVersionAction.reason, "Publish or delete the draft first");
});

test("documentPolicy: New version visibility when no document or no latest_version", () => {
  const can = (_perm: string) => true;

  // No document passed
  const actionsNoDoc = getDocumentActions({ can });
  assert.equal(getAction(actionsNoDoc, "new_version").visible, false);

  // Document with latest_version = null
  const actionsNoLatest = getDocumentActions({ can, document: createMockDoc({ latest_version: null }) });
  assert.equal(getAction(actionsNoLatest, "new_version").visible, false);
});

test("documentPolicy: New version status cases with latest queued, processing, ready, published, failed, archived", () => {
  const can = (_perm: string) => true;

  // queued -> disabled
  const actionsQueued = getDocumentActions({
    can,
    document: createMockDoc({ latest_version: createMockVersion({ status: "queued" }) }),
  });
  assert.equal(getAction(actionsQueued, "new_version").visible, true);
  assert.equal(getAction(actionsQueued, "new_version").enabled, false);
  assert.equal(getAction(actionsQueued, "new_version").reason, "Publish or delete the draft first");

  // processing -> disabled
  const actionsProcessing = getDocumentActions({
    can,
    document: createMockDoc({ latest_version: createMockVersion({ status: "processing" }) }),
  });
  assert.equal(getAction(actionsProcessing, "new_version").visible, true);
  assert.equal(getAction(actionsProcessing, "new_version").enabled, false);
  assert.equal(getAction(actionsProcessing, "new_version").reason, "Publish or delete the draft first");

  // ready -> disabled
  const actionsReady = getDocumentActions({
    can,
    document: createMockDoc({ latest_version: createMockVersion({ status: "ready" }) }),
  });
  assert.equal(getAction(actionsReady, "new_version").visible, true);
  assert.equal(getAction(actionsReady, "new_version").enabled, false);
  assert.equal(getAction(actionsReady, "new_version").reason, "Publish or delete the draft first");

  // published -> enabled
  const actionsPublished = getDocumentActions({
    can,
    document: createMockDoc({ latest_version: createMockVersion({ status: "published" }) }),
  });
  assert.equal(getAction(actionsPublished, "new_version").visible, true);
  assert.equal(getAction(actionsPublished, "new_version").enabled, true);
  assert.equal(getAction(actionsPublished, "new_version").reason, undefined);

  // failed -> enabled
  const actionsFailed = getDocumentActions({
    can,
    document: createMockDoc({ latest_version: createMockVersion({ status: "failed" }) }),
  });
  assert.equal(getAction(actionsFailed, "new_version").visible, true);
  assert.equal(getAction(actionsFailed, "new_version").enabled, true);
  assert.equal(getAction(actionsFailed, "new_version").reason, undefined);

  // archived -> enabled
  const actionsArchived = getDocumentActions({
    can,
    document: createMockDoc({ latest_version: createMockVersion({ status: "archived" }) }),
  });
  assert.equal(getAction(actionsArchived, "new_version").visible, true);
  assert.equal(getAction(actionsArchived, "new_version").enabled, true);
  assert.equal(getAction(actionsArchived, "new_version").reason, undefined);
});

test("documentPolicy: Publish with flags 0, >0 and null", () => {
  const can = (_perm: string) => true;

  // flags = 0
  const actionsZero = getDocumentActions({
    can,
    version: createMockVersion({ status: "ready", flagged_chunk_count: 0 }),
  });
  assert.equal(getAction(actionsZero, "publish").visible, true);
  assert.equal(getAction(actionsZero, "publish").enabled, true);

  // flags > 0
  const actionsPositive = getDocumentActions({
    can,
    version: createMockVersion({ status: "ready", flagged_chunk_count: 3 }),
  });
  assert.equal(getAction(actionsPositive, "publish").visible, true);
  assert.equal(getAction(actionsPositive, "publish").enabled, true);

  // flags = null
  const actionsNull = getDocumentActions({
    can,
    version: createMockVersion({ status: "ready", flagged_chunk_count: null }),
  });
  assert.equal(getAction(actionsNull, "publish").visible, true);
  assert.equal(getAction(actionsNull, "publish").enabled, true);
});

test("documentPolicy: archived version actions", () => {
  const can = (_perm: string) => true;
  const archivedVersion = createMockVersion({ status: "archived" });
  const actions = getDocumentActions({ can, version: archivedVersion });

  assert.equal(getAction(actions, "retry").visible, false);
  assert.equal(getAction(actions, "publish").visible, false);
  assert.equal(getAction(actions, "delete_version").visible, false);
});

test("documentPolicy: retry with can_manage false", () => {
  const can = (_perm: string) => true;
  const failedRetryable = createMockVersion({ status: "failed", retryable: true });
  const doc = createMockDoc({ latest_version: failedRetryable, can_manage: false });

  const actions = getDocumentActions({ can, document: doc, version: failedRetryable });
  const retryAction = getAction(actions, "retry");
  assert.equal(retryAction.visible, true);
  assert.equal(retryAction.enabled, false);
  assert.equal(retryAction.reason, "You teach this subject in only some of the linked classes, so you can only view it");
});

test("documentPolicy: delete version with can_manage false for ready and for failed", () => {
  const can = (_perm: string) => true;
  const lockReason = "You teach this subject in only some of the linked classes, so you can only view it";

  // Ready with can_manage false
  const readyVersion = createMockVersion({ status: "ready" });
  const docReady = createMockDoc({ latest_version: readyVersion, can_manage: false });
  const actionsReady = getDocumentActions({ can, document: docReady, version: readyVersion });
  const delReady = getAction(actionsReady, "delete_version");
  assert.equal(delReady.visible, true);
  assert.equal(delReady.enabled, false);
  assert.equal(delReady.reason, lockReason);

  // Failed with can_manage false
  const failedVersion = createMockVersion({ status: "failed" });
  const docFailed = createMockDoc({ latest_version: failedVersion, can_manage: false });
  const actionsFailed = getDocumentActions({ can, document: docFailed, version: failedVersion });
  const delFailed = getAction(actionsFailed, "delete_version");
  assert.equal(delFailed.visible, true);
  assert.equal(delFailed.enabled, false);
  assert.equal(delFailed.reason, lockReason);
});

const LOCK = "You teach this subject in only some of the linked classes, so you can only view it";

test("documentPolicy matrix: retry is visible only for failed + retryable === true", () => {
  const can = (_perm: string) => true;
  const cases: Array<{ status: string; retryable: boolean | null; visible: boolean }> = [
    { status: "failed", retryable: true, visible: true },
    { status: "failed", retryable: false, visible: false },
    { status: "failed", retryable: null, visible: false },
    { status: "ready", retryable: null, visible: false },
    { status: "queued", retryable: null, visible: false },
    { status: "processing", retryable: null, visible: false },
    { status: "published", retryable: null, visible: false },
    { status: "archived", retryable: null, visible: false },
  ];
  assert.ok(cases.length > 0);
  for (const c of cases) {
    const v = createMockVersion({ status: c.status, retryable: c.retryable });
    const doc = createMockDoc({ latest_version: v });
    const retry = getAction(getDocumentActions({ can, document: doc, version: v }), "retry");
    assert.equal(retry.visible, c.visible, `${c.status}/${String(c.retryable)}`);
    assert.equal(retry.enabled, c.visible);
    assert.equal(retry.reason, undefined);
  }
});

test("documentPolicy matrix: retry and delete need document:upload; view-only and student get none", () => {
  const v = createMockVersion({ status: "failed", retryable: true });
  const doc = createMockDoc({ latest_version: v });
  const viewOnly = (p: string) => p === "document:view";
  const student = (_p: string) => false;
  for (const can of [viewOnly, student]) {
    const actions = getDocumentActions({ can, document: doc, version: v });
    assert.equal(getAction(actions, "retry").visible, false);
    assert.equal(getAction(actions, "delete_version").visible, false);
  }
});

test("documentPolicy matrix: delete version only for ready and failed, never queued, processing, published, archived", () => {
  const can = (_perm: string) => true;
  const cases: Array<[string, boolean]> = [
    ["ready", true],
    ["failed", true],
    ["queued", false],
    ["processing", false],
    ["published", false],
    ["archived", false],
  ];
  for (const [status, visible] of cases) {
    const v = createMockVersion({ status });
    const doc = createMockDoc({ latest_version: v });
    const del = getAction(getDocumentActions({ can, document: doc, version: v }), "delete_version");
    assert.equal(del.visible, visible, status);
    assert.equal(del.enabled, visible, status);
  }
});

test("documentPolicy matrix: a published row is judged by its own status, not the latest version", () => {
  const can = (_perm: string) => true;
  const published = createMockVersion({ id: "v-2", version_no: 2, status: "published" });
  const latestReady = createMockVersion({ id: "v-3", version_no: 3, status: "ready" });
  const doc = createMockDoc({ latest_version: latestReady, published_version: published });
  const forPublished = getDocumentActions({ can, document: doc, version: published });
  assert.equal(getAction(forPublished, "delete_version").visible, false);
  const forLatest = getDocumentActions({ can, document: doc, version: latestReady });
  assert.equal(getAction(forLatest, "delete_version").visible, true);
});

test("documentPolicy matrix: can_manage false disables retry and delete with the lock reason", () => {
  const can = (_perm: string) => true;
  const v = createMockVersion({ status: "failed", retryable: true });
  const doc = createMockDoc({ latest_version: v, can_manage: false });
  const actions = getDocumentActions({ can, document: doc, version: v });
  for (const id of ["retry", "delete_version"] as const) {
    const a = getAction(actions, id);
    assert.equal(a.visible, true);
    assert.equal(a.enabled, false);
    assert.equal(a.reason, LOCK);
  }
});

test("getFailedVersionHint: shown for a failed, non-retryable version when the user may upload", () => {
  const can = (_perm: string) => true;
  const notRetryable = createMockVersion({ status: "failed", retryable: false });
  assert.equal(getFailedVersionHint({ can, version: notRetryable }), "Upload a corrected file as a new version");

  const retryable = createMockVersion({ status: "failed", retryable: true });
  assert.equal(getFailedVersionHint({ can, version: retryable }), null);
  const unknown = createMockVersion({ status: "failed", retryable: null });
  assert.equal(getFailedVersionHint({ can, version: unknown }), null);
  const ready = createMockVersion({ status: "ready", retryable: null });
  assert.equal(getFailedVersionHint({ can, version: ready }), null);
  const viewOnly = (p: string) => p === "document:view";
  assert.equal(getFailedVersionHint({ can: viewOnly, version: notRetryable }), null);
});

test("documentPolicy matrix: new_version and delete_document by permission, can_manage and latest status", () => {
  const statuses = ["queued", "processing", "ready", "published", "failed", "archived"] as const;
  const roles: Record<string, (p: string) => boolean> = {
    admin: () => true,
    teacherUploadOnly: (p) => p === "document:view" || p === "document:upload",
    teacherWithDelete: (p) => ["document:view", "document:upload", "document:delete"].includes(p),
    viewOnly: (p) => p === "document:view",
    student: () => false,
  };
  const BLOCKED = ["queued", "processing", "ready"];
  for (const [roleName, can] of Object.entries(roles)) {
    for (const canManage of [true, false]) {
      for (const status of statuses) {
        const doc = createMockDoc({
          can_manage: canManage,
          latest_version: createMockVersion({ status }),
        });
        const actions = getDocumentActions({ can, document: doc });
        const label = `${roleName} manage=${canManage} ${status}`;
        const nv = getAction(actions, "new_version");
        const dd = getAction(actions, "delete_document");

        // New version: hidden without document:upload; otherwise disabled with a reason (never hidden)
        const mayUpload = can("document:upload");
        assert.equal(nv.visible, mayUpload, `${label}: new_version visible`);
        if (mayUpload) {
          const shouldEnable = canManage && !BLOCKED.includes(status);
          assert.equal(nv.enabled, shouldEnable, `${label}: new_version enabled`);
          assert.equal(Boolean(nv.reason), !shouldEnable, `${label}: new_version reason`);
          if (!canManage) assert.match(nv.reason ?? "", /only some of the linked classes/);
          else if (BLOCKED.includes(status)) assert.equal(nv.reason, "Publish or delete the draft first");
        } else {
          assert.equal(nv.enabled, false, `${label}: hidden is never enabled`);
        }

        // Delete document: hidden without document:delete (whatever the status); disabled with the lock reason when not manageable
        const mayDelete = can("document:delete");
        assert.equal(dd.visible, mayDelete, `${label}: delete_document visible`);
        if (mayDelete) {
          assert.equal(dd.enabled, canManage, `${label}: delete_document enabled`);
          assert.equal(Boolean(dd.reason), !canManage, `${label}: delete_document reason`);
        } else {
          assert.equal(dd.enabled, false);
        }

        if (roleName === "student") {
          assert.ok(actions.every((a) => !a.visible), `${label}: student sees no action`);
        }
      }
    }
  }
});
