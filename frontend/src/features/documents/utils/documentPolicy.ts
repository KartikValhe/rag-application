import type { DocumentItem, DocumentVersionItem } from "../schemas/documentSchemas.ts";

export type DocumentActionId =
  | "view"
  | "upload"
  | "new_version"
  | "retry"
  | "delete_version"
  | "delete_document"
  | "publish"
  | "test_search"
  | "usage";

export interface DocumentPolicyAction {
  id: DocumentActionId;
  visible: boolean;
  enabled: boolean;
  reason?: string;
}

export interface DocumentPolicyInput {
  can: (permission: string) => boolean;
  document?: DocumentItem | null;
  version?: DocumentVersionItem | null;
  statusInfo?: { retryable?: boolean } | null;
}

const CANNOT_MANAGE_REASON =
  "You teach this subject in only some of the linked classes, so you can only view it";

export function getDocumentActions(input: DocumentPolicyInput): DocumentPolicyAction[] {
  const { can: checkPermission, document, version, statusInfo } = input;
  const activeVersion = version ?? document?.latest_version ?? null;
  const activeStatus = activeVersion?.status;
  const isManageBlocked = document !== undefined && document !== null && document.can_manage === false;

  const actions: DocumentPolicyAction[] = [];

  // 1. View
  const canView = checkPermission("document:view");
  actions.push({
    id: "view",
    visible: canView,
    enabled: canView,
  });

  // 2. Upload
  const canUpload = checkPermission("document:upload");
  actions.push({
    id: "upload",
    visible: canUpload,
    enabled: canUpload,
  });

  // 3. New Version
  const hasLatestVersion = Boolean(document && document.latest_version);
  const newVersionVisible = canUpload && Boolean(document) && hasLatestVersion;
  let newVersionEnabled = newVersionVisible;
  let newVersionReason: string | undefined = undefined;

  if (newVersionVisible && document && document.latest_version) {
    const latestStatus = document.latest_version.status;
    if (isManageBlocked) {
      newVersionEnabled = false;
      newVersionReason = CANNOT_MANAGE_REASON;
    } else if (
      latestStatus === "queued" ||
      latestStatus === "processing" ||
      latestStatus === "ready"
    ) {
      newVersionEnabled = false;
      newVersionReason = "Publish or delete the draft first";
    }
  }
  actions.push({
    id: "new_version",
    visible: newVersionVisible,
    enabled: newVersionEnabled,
    reason: newVersionReason,
  });

  // 4. Retry
  const isFailed = activeStatus === "failed";
  const isRetryable = Boolean(activeVersion?.retryable ?? statusInfo?.retryable);
  const retryVisible = canUpload && isFailed && isRetryable;
  let retryEnabled = retryVisible;
  let retryReason: string | undefined = undefined;

  if (retryVisible) {
    if (isManageBlocked) {
      retryEnabled = false;
      retryReason = CANNOT_MANAGE_REASON;
    }
  }
  actions.push({
    id: "retry",
    visible: retryVisible,
    enabled: retryEnabled,
    reason: retryReason,
  });

  // 5. Delete Version
  const isVersionDeletable = activeStatus === "ready" || activeStatus === "failed";
  const deleteVersionVisible = canUpload && isVersionDeletable;
  let deleteVersionEnabled = deleteVersionVisible;
  let deleteVersionReason: string | undefined = undefined;

  if (deleteVersionVisible) {
    if (isManageBlocked) {
      deleteVersionEnabled = false;
      deleteVersionReason = CANNOT_MANAGE_REASON;
    }
  }
  actions.push({
    id: "delete_version",
    visible: deleteVersionVisible,
    enabled: deleteVersionEnabled,
    reason: deleteVersionReason,
  });

  // 6. Delete Document
  const canDelete = checkPermission("document:delete");
  const deleteDocVisible = canDelete;
  let deleteDocEnabled = deleteDocVisible;
  let deleteDocReason: string | undefined = undefined;

  if (deleteDocVisible) {
    if (isManageBlocked) {
      deleteDocEnabled = false;
      deleteDocReason = CANNOT_MANAGE_REASON;
    }
  }
  actions.push({
    id: "delete_document",
    visible: deleteDocVisible,
    enabled: deleteDocEnabled,
    reason: deleteDocReason,
  });

  // 7. Publish
  const isReady = activeStatus === "ready";
  const canPublish = checkPermission("document:publish");
  const publishVisible = canPublish && isReady;
  let publishEnabled = publishVisible;
  let publishReason: string | undefined = undefined;

  if (publishVisible) {
    if (isManageBlocked) {
      publishEnabled = false;
      publishReason = CANNOT_MANAGE_REASON;
    }
  }
  actions.push({
    id: "publish",
    visible: publishVisible,
    enabled: publishEnabled,
    reason: publishReason,
  });

  // 8. Test Search
  actions.push({
    id: "test_search",
    visible: canView,
    enabled: canView,
  });

  // 9. Usage
  const canManageSettings = checkPermission("settings:manage");
  actions.push({
    id: "usage",
    visible: canManageSettings,
    enabled: canManageSettings,
  });

  return actions;
}
