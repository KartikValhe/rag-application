import React, { useEffect, useRef } from "react";
import { AppShell, PageHeader } from "@/components/layout";
import { Alert, Button } from "@/components/ui";
import { RefreshCw } from "lucide-react";
import { useDocumentUsage } from "../hooks/useDocumentUsage";
import { getDocumentError } from "../utils/documentErrors";
import { formatBytes, formatCount, getMeter, type Meter, type MeterLevel } from "../utils/usageView";
import type { DocumentsApi } from "../api/documentsApi";
import type { DocumentUsageItem } from "../schemas/documentSchemas";

export interface MaterialsUsagePageProps {
  api?: DocumentsApi;
}

const LEVEL_TEXT_CLASS: Record<MeterLevel, string> = {
  unknown: "text-text-muted",
  ok: "text-text-muted",
  warning: "text-warning font-medium",
  full: "text-danger font-semibold",
};

const LEVEL_BAR_CLASS: Record<MeterLevel, string> = {
  unknown: "accent-primary",
  ok: "accent-primary",
  warning: "accent-warning",
  full: "accent-danger",
};

interface UsageCardProps {
  id: string;
  label: string;
  meter: Meter;
}

const UsageCard: React.FC<UsageCardProps> = ({ id, label, meter }) => (
  <div data-usage-card className="rounded-xl border border-border bg-surface p-4 min-w-0">
    <p id={`${id}-label`} data-usage-label className="text-sm font-medium text-text">
      {label}
    </p>
    <p className="mt-1 text-lg font-semibold text-text break-words">{meter.text}</p>
    {meter.progress && (
      <progress
        aria-labelledby={`${id}-label`}
        value={meter.progress.value}
        max={meter.progress.max}
        className={`mt-2 block w-full h-3 ${LEVEL_BAR_CLASS[meter.level]}`}
      />
    )}
    {meter.levelText && (
      <p className={`mt-2 text-sm ${LEVEL_TEXT_CLASS[meter.level]}`}>{meter.levelText}</p>
    )}
  </div>
);

function hasAnyFigure(usage: DocumentUsageItem): boolean {
  return usage.documents !== null || usage.storage_bytes !== null || usage.pages !== null;
}

function formatFetchedAt(date: Date): string {
  return date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export const MaterialsUsagePage: React.FC<MaterialsUsagePageProps> = ({ api }) => {
  const { usage, fetchedAt, isLoading, isRefreshing, isError, error, refresh } = useDocumentUsage({ api });
  const rootRef = useRef<HTMLDivElement>(null);

  // After navigation, focus goes to the page heading (made focusable with tabindex -1)
  useEffect(() => {
    const heading = rootRef.current?.querySelector("h1");
    if (heading) {
      heading.setAttribute("tabindex", "-1");
      heading.focus();
    }
  }, []);

  const limits = usage?.limits;
  const refreshButton = (
    <Button
      variant="secondary"
      onClick={() => void refresh()}
      disabled={isLoading || isRefreshing}
      isLoading={isRefreshing}
      aria-busy={isRefreshing}
      leftIcon={<RefreshCw className="w-4 h-4" aria-hidden="true" />}
      className="w-full sm:w-auto"
    >
      {isRefreshing ? "Refreshing" : "Refresh"}
    </Button>
  );

  const limitRows: Array<[string, string]> = limits
    ? [
        ["Largest file", formatBytes(limits.max_file_bytes)],
        ["Most pages in one PDF", formatCount(limits.max_pdf_pages)],
        ["Most slides in one presentation", formatCount(limits.max_pptx_slides)],
        ["Uploads per person per hour", formatCount(limits.uploads_per_user_per_hour)],
        ["Uploads per organisation per hour", formatCount(limits.uploads_per_tenant_per_hour)],
      ]
    : [];

  return (
    <AppShell>
      <div ref={rootRef}>
        <PageHeader
          title="Usage and limits"
          description="How much of your organisation's course-material allowance is in use."
          breadcrumbs={[
            { label: "Administration", href: "/admin" },
            { label: "Course materials", href: "/admin/materials" },
            { label: "Usage and limits" },
          ]}
          actions={refreshButton}
        />

        {fetchedAt && (
          <p className="mb-4 text-sm text-text-muted">
            Fetched at <time dateTime={fetchedAt.toISOString()}>{formatFetchedAt(fetchedAt)}</time>. These figures do
            not update by themselves; press Refresh to load them again.
          </p>
        )}

        {isError && (
          <Alert variant="danger" className="mb-4">
            <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between gap-3">
              <span>{getDocumentError(error, "usage").message}</span>
              {!usage && (
                <Button size="sm" variant="secondary" onClick={() => void refresh()} className="w-full sm:w-auto">
                  Try again
                </Button>
              )}
            </div>
          </Alert>
        )}

        {isLoading && (
          <p role="status" className="py-8 text-center text-sm text-text-muted">
            Loading usage figures...
          </p>
        )}

        {usage && !hasAnyFigure(usage) && (
          <div className="rounded-xl border border-dashed border-border bg-surface p-6 text-center">
            <p className="text-base font-medium text-text">No usage data yet.</p>
            <p className="mt-1 text-sm text-text-muted">Figures appear here once the server reports them.</p>
          </div>
        )}

        {usage && hasAnyFigure(usage) && limits && (
          <>
            <section aria-labelledby="usage-allowance-heading">
              <h2 id="usage-allowance-heading" className="mb-3 text-lg font-semibold text-text">
                Allowance in use
              </h2>
              <div data-testid="usage-cards" className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                <UsageCard
                  id="usage-documents"
                  label="Documents"
                  meter={getMeter(usage.documents, limits.max_documents, "count")}
                />
                <UsageCard
                  id="usage-storage"
                  label="Storage"
                  meter={getMeter(usage.storage_bytes, limits.max_storage_bytes, "bytes")}
                />
                <UsageCard
                  id="usage-pages"
                  label="Pages"
                  meter={getMeter(usage.pages, limits.max_pages, "count")}
                />
              </div>
              {usage.documents === 0 && usage.storage_bytes === 0 && usage.pages === 0 && (
                <p className="mt-3 text-sm text-text-muted">No materials have been uploaded yet.</p>
              )}
              <p className="mt-3 text-sm text-text-muted">Deleted materials are not counted.</p>
            </section>

            <section aria-labelledby="usage-limits-heading" className="mt-8">
              <h2 id="usage-limits-heading" className="mb-3 text-lg font-semibold text-text">
                Upload limits
              </h2>
              <div
                className="rounded-xl border border-border bg-surface divide-y divide-border"
              >
                {limitRows.map(([label, value]) => (
                  <dl
                    key={label}
                    data-limit-row
                    className="flex flex-col gap-1 p-4 sm:flex-row sm:items-center sm:justify-between"
                  >
                    <dt className="text-sm text-text-muted">{label}</dt>
                    <dd className="text-sm font-medium text-text break-words">{value}</dd>
                  </dl>
                ))}
              </div>
              <p className="mt-3 text-sm text-text-muted">
                The number of uploads made so far this hour is not shown here.
              </p>
            </section>
          </>
        )}
      </div>
    </AppShell>
  );
};
