import React, { useMemo, useRef, useState } from "react";
import { useParams, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { AppShell, PageHeader, NotFoundState } from "@/components/layout";
import {
  Table,
  type Column,
  Badge,
  Button,
  Alert,
  Skeleton,
  ConfirmDialog,
} from "@/components/ui";
import { useOptionalToast } from "@/components/ui/Toast";
import { usePermission } from "@/hooks/usePermission";
import { Lock, FileText, AlertCircle, RefreshCw } from "lucide-react";
import { documentsApi as defaultDocumentsApi } from "../api/documentsClient";
import type { DocumentsApi } from "../api/documentsApi";
import type {
  DocumentVersionItem,
  DocumentStatusItem,
} from "../schemas/documentSchemas";
import { getStatusChip } from "../utils/statusChip";
import { getDocumentError, getVersionFailure } from "../utils/documentErrors";
import { useVersionStatus } from "../hooks/useVersionStatus";
import { isTerminalStatus } from "../utils/pollingSchedule";
import { getDocumentActions, getFailedVersionHint } from "../utils/documentPolicy";
import { sanitizeDisplayFilename } from "../utils/fileChecks";
import { useRetryVersion, useDeleteVersion } from "../hooks/useDocumentMutations";
import { VersionRowActions } from "../components/VersionRowActions";

export interface MaterialDetailPageProps {
  api?: DocumentsApi;
  responsiveLayout?: "stacked" | "scroll";
}

function formatFileSize(bytes?: number | null): string {
  if (bytes === null || bytes === undefined || isNaN(bytes)) return "-";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(dateStr?: string | null): string {
  if (!dateStr) return "-";
  try {
    const d = new Date(dateStr);
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return dateStr;
  }
}

function getBlockedReasonText(reason: string | null | undefined): string {
  if (reason === "document_manage_requires_all_classes") {
    return "You teach this subject in only some of the linked classes, so you can only view it.";
  }
  return "You have view-only access to this material.";
}

export const MaterialDetailPage: React.FC<MaterialDetailPageProps> = ({
  api: customApi,
  responsiveLayout = "stacked",
}) => {
  const { documentId = "" } = useParams<{ documentId: string }>();
  const location = useLocation();
  const api = customApi ?? defaultDocumentsApi;
  const [announcement, setAnnouncement] = useState("");
  const { can } = usePermission();
  const toast = useOptionalToast();
  const [retryError, setRetryError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DocumentVersionItem | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // A retried version starts a new processing run: its polling clock starts at the click, not at upload.
  const [retryStartedAt, setRetryStartedAt] = useState<Record<string, string>>({});
  const inFlightRef = useRef(false);

  const isAdminRoute = location.pathname.startsWith("/admin");
  const baseRoute = isAdminRoute ? "/admin/materials" : "/teacher/materials";

  // Fetch document details
  const {
    data: doc,
    isLoading,
    isError,
    error,
    refetch: refetchDoc,
  } = useQuery({
    queryKey: ["document", documentId],
    queryFn: () => api.get(documentId),
    enabled: Boolean(documentId),
    retry: false,
  });

  const latestVersion = doc?.latest_version;
  const isInFlight =
    Boolean(latestVersion) &&
    (latestVersion?.status === "queued" || latestVersion?.status === "processing");

  // In-flight version status polling
  const {
    status: polledStatus,
    stage: polledStage,
    isConnectionLost,
    isHardStop,
    longRunningInfo,
    checkAgain,
  } = useVersionStatus({
    documentId,
    versionId: latestVersion?.id,
    initialStatus: latestVersion?.status,
    initialStage: latestVersion?.stage,
    createdAt: (latestVersion && retryStartedAt[latestVersion.id]) || latestVersion?.created_at,
    api,
    enabled: isInFlight,
    onTerminalStatus: (statusItem: DocumentStatusItem) => {
      // Announce status change
      if (statusItem.status === "ready") {
        setAnnouncement(`${doc?.title || "Document"}: ready to publish`);
      } else if (statusItem.status === "failed") {
        const failure = getVersionFailure(statusItem.error_code);
        setAnnouncement(`${doc?.title || "Document"}: failed: ${failure.message}`);
      }
      refetchDoc();
    },
  });

  const retryMutation = useRetryVersion({
    api,
    onSuccess: (_version, args) => {
      inFlightRef.current = false;
      setRetryError(null);
      setRetryStartedAt((prev) => ({ ...prev, [args.versionId]: new Date().toISOString() }));
      setAnnouncement(`${doc?.title || "Document"}: waiting to be processed again`);
      toast?.success("Retry started. We are reading the file again.");
    },
    onError: (err) => {
      inFlightRef.current = false;
      setRetryError(getDocumentError(err, "retry").message);
    },
  });

  const deleteMutation = useDeleteVersion({
    api,
    onSuccess: (result) => {
      inFlightRef.current = false;
      setDeleteTarget(null);
      setDeleteError(null);
      const text = result.alreadyRemoved ? "That version was already removed." : "Version deleted.";
      setAnnouncement(text);
      toast?.success(text);
    },
    onError: (err) => {
      inFlightRef.current = false;
      setDeleteError(getDocumentError(err, "delete").message);
    },
  });

  const handleRetry = (version: DocumentVersionItem) => {
    if (inFlightRef.current || retryMutation.isPending || deleteMutation.isPending) return;
    inFlightRef.current = true;
    setRetryError(null);
    retryMutation.mutate({ documentId, versionId: version.id });
  };

  const handleConfirmDelete = () => {
    if (!deleteTarget || inFlightRef.current || deleteMutation.isPending || retryMutation.isPending) return;
    inFlightRef.current = true;
    setDeleteError(null);
    deleteMutation.mutate({ documentId, versionId: deleteTarget.id });
  };

  const handleCloseDelete = () => {
    if (deleteMutation.isPending) return;
    setDeleteTarget(null);
    setDeleteError(null);
  };

  // Synthesize versions list from document
  const versions: DocumentVersionItem[] = useMemo(() => {
    if (!doc) return [];
    if (Array.isArray((doc as unknown as { versions?: DocumentVersionItem[] }).versions)) {
      return (doc as unknown as { versions: DocumentVersionItem[] }).versions;
    }

    const map = new Map<string, DocumentVersionItem>();
    if (doc.latest_version) {
      // Overlay live polling status onto latest version row
      const liveLatest: DocumentVersionItem = {
        ...doc.latest_version,
        status: polledStatus || doc.latest_version.status,
        stage: polledStage !== undefined ? polledStage : doc.latest_version.stage,
      };
      // The live status can be ahead of the document: its retryable flag is then not known yet.
      if (liveLatest.status !== doc.latest_version.status) liveLatest.retryable = null;
      map.set(doc.latest_version.id, liveLatest);
    }
    if (doc.published_version) {
      if (!map.has(doc.published_version.id)) {
        map.set(doc.published_version.id, doc.published_version);
      }
    }
    return Array.from(map.values()).sort((a, b) => b.version_no - a.version_no);
  }, [doc, polledStatus, polledStage]);

  const breadcrumbs = useMemo(() => {
    const rootLabel = isAdminRoute ? "Administration" : "Teacher Portal";
    const rootPath = isAdminRoute ? "/admin" : "/teacher/dashboard";
    return [
      { label: rootLabel, href: rootPath },
      { label: "Course materials", href: baseRoute },
      { label: doc?.title || "Material" },
    ];
  }, [isAdminRoute, baseRoute, doc?.title]);

  // Handle 404
  const errorStatus = (error as { response?: { status?: number } })?.response?.status;
  if (isError && errorStatus === 404) {
    return (
      <AppShell>
        <NotFoundState
          title="Material Not Found (404)"
          description="We could not find this material. It may have been removed."
          homePath={baseRoute}
          homeLabel="Back to materials"
        />
      </AppShell>
    );
  }

  // Handle other load errors
  if (isError) {
    return (
      <AppShell>
        <PageHeader title="Course material" breadcrumbs={breadcrumbs} />
        <Alert variant="danger" title="Error loading material">
          <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
            <span>{getDocumentError(error, "list").message}</span>
            <Button size="sm" variant="secondary" onClick={() => refetchDoc()}>
              Try again
            </Button>
          </div>
        </Alert>
      </AppShell>
    );
  }

  // Loading skeleton
  if (isLoading || !doc) {
    return (
      <AppShell>
        <PageHeader title="Loading..." breadcrumbs={breadcrumbs} />
        <div className="space-y-4">
          <div className="p-6 bg-surface rounded-xl border border-border space-y-3">
            <Skeleton className="h-6 w-1/3" />
            <Skeleton className="h-4 w-1/4" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        </div>
      </AppShell>
    );
  }

  const columns: Column<DocumentVersionItem>[] = [
    {
      key: "version_no",
      header: "Version",
      className: "w-24 whitespace-nowrap",
      render: (v) => <span className="font-semibold text-text">v{v.version_no}</span>,
    },
    {
      key: "original_filename",
      header: "File Name",
      className: "min-w-[200px]",
      render: (v) => (
        <span className="font-medium text-text break-words">
          {v.original_filename}
        </span>
      ),
    },
    {
      key: "file_kind",
      header: "Type",
      className: "w-20 text-xs text-text-muted whitespace-nowrap",
      render: (v) => <span>{v.file_kind ? v.file_kind.toUpperCase() : "-"}</span>,
    },
    {
      key: "size_bytes",
      header: "Size",
      className: "w-24 text-xs text-text-muted whitespace-nowrap",
      render: (v) => <span>{formatFileSize(v.size_bytes)}</span>,
    },
    {
      key: "page_count",
      header: "Pages",
      className: "w-24 text-xs text-text-muted whitespace-nowrap",
      render: (v) => (
        <span>
          {v.page_count != null
            ? `${v.page_count} ${v.page_count === 1 ? "page" : "pages"}`
            : "-"}
        </span>
      ),
    },
    {
      key: "status",
      header: "Status",
      className: "min-w-[200px]",
      render: (v) => {
        const isCurrentActive = v.id === latestVersion?.id && !isTerminalStatus(v.status);
        const chip = getStatusChip(v.status, v.stage, v.flagged_chunk_count);

        return (
          <div className="space-y-1">
            <Badge variant={chip.variant}>{chip.label}</Badge>

            {/* In-flight connection lost banner */}
            {isCurrentActive && isConnectionLost && (
              <p className="text-xs text-warning flex items-center gap-1 font-medium">
                <AlertCircle className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />
                <span>Connection lost, retrying</span>
              </p>
            )}

            {/* In-flight long-running hints */}
            {isCurrentActive && longRunningInfo.hint && (
              <p className="text-xs text-text-muted">
                {longRunningInfo.hint}
              </p>
            )}

            {/* 65-minute hard stop */}
            {isCurrentActive && isHardStop && (
              <div className="pt-1">
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={checkAgain}
                  leftIcon={<RefreshCw className="w-3 h-3" />}
                >
                  Check again
                </Button>
              </div>
            )}

            {/* Plain reason for failed versions */}
            {v.status === "failed" && v.error_code && (
              <p className="text-xs text-danger font-medium mt-1">
                {getVersionFailure(v.error_code).message}
              </p>
            )}
          </div>
        );
      },
    },
    {
      key: "created_at",
      header: "Uploaded",
      className: "w-28 text-xs text-text-muted whitespace-nowrap",
      render: (v) => <span>{formatDate(v.created_at)}</span>,
    },
    {
      key: "published_at",
      header: "Published",
      className: "w-28 text-xs text-text-muted whitespace-nowrap",
      render: (v) => <span>{v.published_at ? formatDate(v.published_at) : "-"}</span>,
    },
  ];

  // Row actions come only from the policy module; the page just renders what it returns.
  const rowActions = (v: DocumentVersionItem) =>
    getDocumentActions({ can, document: doc, version: v });
  const showActionsColumn = versions.some(
    (v) =>
      rowActions(v).some((a) => (a.id === "retry" || a.id === "delete_version") && a.visible) ||
      getFailedVersionHint({ can, version: v }) !== null
  );
  if (showActionsColumn) {
    columns.push({
      key: "actions",
      header: "Actions",
      className: "min-w-[220px]",
      render: (v) => {
        const isRetrying = retryMutation.isPending && retryMutation.variables?.versionId === v.id;
        const isDeleting = deleteMutation.isPending && deleteMutation.variables?.versionId === v.id;
        return (
          <VersionRowActions
            actions={rowActions(v)}
            hint={getFailedVersionHint({ can, version: v })}
            isRetrying={isRetrying}
            isBusy={isRetrying || isDeleting}
            describedById={`version-note-${v.id}`}
            onRetry={() => handleRetry(v)}
            onDelete={() => {
              setDeleteError(null);
              setDeleteTarget(v);
            }}
          />
        );
      },
    });
  }

  const deleteTargetEnabled = deleteTarget
    ? rowActions(deleteTarget).find((a) => a.id === "delete_version")?.enabled === true
    : false;

  return (
    <AppShell>
      {/* Visually hidden aria-live polite region for status change announcements */}
      <div className="sr-only" aria-live="polite" aria-atomic="true">
        {announcement}
      </div>

      <PageHeader
        title={doc.title}
        breadcrumbs={breadcrumbs}
        description={doc.description || undefined}
      />

      {/* Document Meta Header Card */}
      <div className="p-5 bg-surface rounded-xl border border-border mb-6 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="primary" size="md">
            {doc.subject?.name ?? "Subject"}
          </Badge>
          <div className="flex flex-wrap gap-1">
            {doc.classes?.map((cls) => (
              <Badge key={cls.id} variant="secondary" size="sm">
                {cls.name}
              </Badge>
            ))}
          </div>
        </div>

        {!doc.can_manage && (
          <p className="flex items-center gap-1.5 text-xs text-warning pt-1">
            <Lock className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />
            <span>{getBlockedReasonText(doc.blocked_reason)}</span>
          </p>
        )}
      </div>

      {/* Versions Section */}
      <div className="space-y-3">
        <div className="flex items-center space-x-2">
          <FileText className="w-5 h-5 text-primary" />
          <h3 className="text-base font-semibold text-text">Versions</h3>
        </div>

        {retryError && (
          <Alert variant="danger" title="Could not try again" onClose={() => setRetryError(null)}>
            <span className="break-words">{retryError}</span>
          </Alert>
        )}

        <Table
          columns={columns}
          data={versions}
          keyExtractor={(v) => v.id}
          responsiveLayout={responsiveLayout}
          mobileActionsLayout="stacked"
        />
      </div>

      <ConfirmDialog
        isOpen={deleteTarget !== null}
        title={
          deleteTarget
            ? `Delete version ${deleteTarget.version_no} (${sanitizeDisplayFilename(deleteTarget.original_filename, 40)})?`
            : ""
        }
        message="The file will be removed. This cannot be undone."
        confirmText="Delete version"
        cancelText="Cancel"
        variant="danger"
        initialFocus="cancel"
        isLoading={deleteMutation.isPending}
        confirmDisabled={!deleteTargetEnabled}
        error={deleteError}
        onConfirm={handleConfirmDelete}
        onClose={handleCloseDelete}
      />
    </AppShell>
  );
};
