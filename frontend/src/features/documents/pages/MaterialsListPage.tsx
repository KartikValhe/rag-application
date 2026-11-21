import React, { useMemo } from "react";
import { useLocation } from "react-router-dom";
import { AppShell, PageHeader } from "@/components/layout";
import {
  Table,
  type Column,
  Badge,
  Button,
  Select,
  Alert,
  EmptyState,
} from "@/components/ui";
import { Lock, FolderArchive } from "lucide-react";
import { useDocuments } from "../hooks/useDocuments";
import { getStatusChip } from "../utils/statusChip";
import { getDocumentError } from "../utils/documentErrors";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentItem } from "../schemas/documentSchemas";
import { useAuth } from "@/features/auth/AuthContext";

export interface MaterialsListPageProps {
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

export const MaterialsListPage: React.FC<MaterialsListPageProps> = ({
  api,
  responsiveLayout = "scroll",
}) => {
  const location = useLocation();
  const { user } = useAuth();
  const isAdmin = Boolean(user?.isAdmin || user?.roles?.includes("tenant_admin"));
  const isTeacher = Boolean(user?.roles?.includes("teacher"));

  const {
    documents,
    classes,
    availableSubjects,
    filterClass,
    filterSubject,
    filterStatus,
    hasActiveFilters,
    setFilter,
    clearFilters,
    isLoading,
    isFetchingNextPage,
    hasNextPage,
    fetchNextPage,
    isError,
    error,
    refetch,
  } = useDocuments({ api });

  const isAdminRoute = location.pathname.startsWith("/admin");
  const breadcrumbs = isAdminRoute
    ? [
        { label: "Administration", href: "/admin" },
        { label: "Course materials" },
      ]
    : [
        { label: "Teacher Portal", href: "/teacher/dashboard" },
        { label: "Course materials" },
      ];

  const columns: Column<DocumentItem>[] = useMemo(
    () => [
      {
        key: "title",
        header: "Title & Subject",
        className: "min-w-[220px]",
        render: (doc) => (
          <div className="space-y-1">
            <div className="font-medium text-text break-words">
              {doc.title}
            </div>
            <div className="text-xs text-text-muted break-words">
              {doc.subject?.name ?? "-"}
            </div>
            {doc.description && (
              <div className="text-xs text-text-muted/80 line-clamp-2 break-words">
                {doc.description}
              </div>
            )}
            {!doc.can_manage && (
              <p className="flex items-center gap-1 text-xs text-warning mt-1">
                <Lock className="w-3 h-3 shrink-0" aria-hidden="true" />
                <span>{getBlockedReasonText(doc.blocked_reason)}</span>
              </p>
            )}
          </div>
        ),
      },
      {
        key: "classes",
        header: "Classes",
        className: "min-w-[140px]",
        render: (doc) => {
          const classesList = doc.classes || [];
          const visibleClasses = classesList.slice(0, 3);
          const remaining = classesList.length - 3;
          return (
            <div className="flex flex-wrap gap-1">
              {visibleClasses.map((cls) => (
                <Badge key={cls.id} variant="secondary" size="sm">
                  {cls.name}
                </Badge>
              ))}
              {remaining > 0 && (
                <Badge variant="default" size="sm">
                  +{remaining}
                </Badge>
              )}
            </div>
          );
        },
      },
      {
        key: "status",
        header: "Status",
        className: "min-w-[140px]",
        render: (doc) => {
          const latest = doc.latest_version;
          const chip = getStatusChip(
            latest?.status,
            latest?.stage,
            latest?.flagged_chunk_count
          );
          const showDual =
            doc.published_version &&
            latest &&
            doc.published_version.version_no !== latest.version_no;

          return (
            <div className="flex flex-col gap-1 items-start">
              <Badge variant={chip.variant}>{chip.label}</Badge>
              {showDual && (
                <Badge variant="success">
                  Published: v{doc.published_version?.version_no}
                </Badge>
              )}
            </div>
          );
        },
      },
      {
        key: "size",
        header: "Size & Type",
        className: "text-xs text-text-muted whitespace-nowrap",
        render: (doc) => {
          const latest = doc.latest_version;
          if (!latest) return "-";
          const sizeStr = formatFileSize(latest.size_bytes);
          const kindStr = latest.file_kind ? latest.file_kind.toUpperCase() : "";
          return `${sizeStr}${kindStr ? ` • ${kindStr}` : ""}`;
        },
      },
      {
        key: "updated",
        header: "Updated",
        className: "text-xs text-text-muted whitespace-nowrap",
        render: (doc) => formatDate(doc.updated_at),
      },
    ],
    []
  );

  const emptyConfig = useMemo(() => {
    if (isTeacher && !isAdmin && classes.length === 0) {
      return {
        title: "No assigned classes",
        description: "Ask your administrator to assign you to a class to view its materials.",
        action: undefined,
      };
    }
    if (hasActiveFilters) {
      return {
        title: "No materials match",
        description: "No course materials match the selected filters.",
        action: (
          <Button variant="secondary" onClick={clearFilters}>
            Clear filters
          </Button>
        ),
      };
    }
    return {
      title: "No materials yet",
      description: "No course materials have been added yet.",
      action: undefined,
    };
  }, [isTeacher, isAdmin, classes.length, hasActiveFilters, clearFilters]);

  return (
    <AppShell>
      <PageHeader
        title="Course materials"
        description="Browse textbooks, syllabus guides, lecture notes, and curriculum resources."
        breadcrumbs={breadcrumbs}
      />

      {/* Filter Bar */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-6 bg-surface p-4 rounded-xl border border-border">
        <Select
          label="Class"
          value={filterClass}
          onChange={(e) => setFilter("class", e.target.value)}
          options={[
            { value: "", label: "All classes" },
            ...classes.map((cls) => ({ value: cls.id, label: cls.name })),
          ]}
          disabled={classes.length === 0}
        />
        <Select
          label="Subject"
          value={filterSubject}
          onChange={(e) => setFilter("subject", e.target.value)}
          options={[
            { value: "", label: "All subjects" },
            ...availableSubjects.map((sub) => ({
              value: sub.id,
              label: sub.name,
            })),
          ]}
          disabled={availableSubjects.length === 0}
        />
        <Select
          label="Status"
          value={filterStatus}
          onChange={(e) => setFilter("status", e.target.value)}
          options={[
            { value: "", label: "All statuses" },
            { value: "processing", label: "Processing" },
            { value: "ready", label: "Draft" },
            { value: "published", label: "Published" },
            { value: "failed", label: "Failed" },
          ]}
        />
      </div>

      {/* Error Alert */}
      {isError && (
        <Alert variant="danger" title="Error loading materials" className="mb-4">
          <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
            <span>{getDocumentError(error, "list").message}</span>
            <Button size="sm" variant="secondary" onClick={() => refetch()}>
              Try again
            </Button>
          </div>
        </Alert>
      )}

      {/* Main Content: Loading, Empty, or Table */}
      {!isLoading && !isError && documents.length === 0 ? (
        <div className="py-8 bg-surface rounded-xl border border-border">
          <EmptyState
            icon={FolderArchive}
            title={emptyConfig.title}
            description={emptyConfig.description}
            action={emptyConfig.action}
          />
        </div>
      ) : (
        <>
          <Table
            columns={columns}
            data={documents}
            keyExtractor={(d) => d.id}
            isLoading={isLoading}
            loadingRowCount={5}
            responsiveLayout={responsiveLayout}
          />

          {hasNextPage && (
            <div className="mt-4 flex justify-center">
              <Button
                variant="secondary"
                onClick={() => fetchNextPage()}
                isLoading={isFetchingNextPage}
                disabled={isFetchingNextPage}
              >
                Load more
              </Button>
            </div>
          )}
        </>
      )}
    </AppShell>
  );
};
