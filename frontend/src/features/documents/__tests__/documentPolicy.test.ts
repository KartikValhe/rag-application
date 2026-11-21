import test from "node:test";
import assert from "node:assert/strict";
import { getDocumentActions, type DocumentActionId, type DocumentPolicyAction } from "../utils/documentPolicy.ts";
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

test("documentPolicy: state-specific visibility and enabled rules", () => {
  const can = (_perm: string) => true;

  // 1. Retry visible only for failed + retryable
  const failedRetryable = createMockVersion({ status: "failed", retryable: true });
  const actions1 = getDocumentActions({ can, version: failedRetryable });
  assert.equal(getAction(actions1, "retry").visible, true);
  assert.equal(getAction(actions1, "retry").enabled, true);

  const failedNotRetryable = createMockVersion({ status: "failed", retryable: false });
  const actions2 = getDocumentActions({ can, version: failedNotRetryable });
  assert.equal(getAction(actions2, "retry").visible, false, "Not retryable: retry button is hidden");

  // 2. Publish visible only when status is ready
  const publishedVersion = createMockVersion({ status: "published" });
  const actions3 = getDocumentActions({ can, version: publishedVersion });
  assert.equal(getAction(actions3, "publish").visible, false, "Publish hidden when already published");

  // 3. Delete version visible only when ready or failed
  const processingVersion = createMockVersion({ status: "processing" });
  const actions4 = getDocumentActions({ can, version: processingVersion });
  assert.equal(getAction(actions4, "delete_version").visible, false, "Delete version hidden when processing");
  assert.equal(getAction(actions1, "delete_version").visible, true, "Delete version visible when failed");

  // 4. New version enabled when latest version is published or archived or null
  const docPublished = createMockDoc({ latest_version: publishedVersion, can_manage: true });
  const actions5 = getDocumentActions({ can, document: docPublished });
  assert.equal(getAction(actions5, "new_version").visible, true);
  assert.equal(getAction(actions5, "new_version").enabled, true);
});
