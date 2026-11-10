import React from "react";
import { ChevronDown, ChevronUp, ChevronsUpDown, ChevronLeft, ChevronRight, LucideIcon, ArrowUpDown } from "lucide-react";
import { cn } from "@/utils/cn";
import { Skeleton } from "./Skeleton";
import { EmptyState } from "./EmptyState";

export type SortDirection = "asc" | "desc";

export interface Column<T> {
  key: string;
  header: string;
  sortable?: boolean;
  className?: string;
  render?: (item: T, index: number) => React.ReactNode;
}

export interface PaginationConfig {
  page: number;
  pageSize: number;
  total: number;
  onPageChange: (newPage: number) => void;
  pageSizeOptions?: number[];
  onPageSizeChange?: (newSize: number) => void;
}

export interface TableProps<T> {
  columns: Column<T>[];
  data: T[];
  keyExtractor: (item: T, index: number) => string;
  isLoading?: boolean;
  loadingRowCount?: number;
  emptyMessage?: string;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyIcon?: LucideIcon;
  emptyAction?: React.ReactNode;
  sortKey?: string | null;
  sortDirection?: SortDirection | null;
  onSort?: (key: string) => void;
  pagination?: PaginationConfig;
  className?: string;
  onRowClick?: (item: T) => void;
  /**
   * Layout mode on small screens (< md breakpoint):
   * - "stacked" (default): transforms rows into mobile-optimized cards with key-value pairs
   * - "scroll": retains table layout inside a horizontal-scrolling container
   */
  responsiveLayout?: "stacked" | "scroll";
}

export function Table<T>({
  columns,
  data,
  keyExtractor,
  isLoading = false,
  loadingRowCount = 5,
  emptyMessage,
  emptyTitle = "No records found",
  emptyDescription = "There are no items to display in this list.",
  emptyIcon,
  emptyAction,
  sortKey,
  sortDirection,
  onSort,
  pagination,
  className,
  onRowClick,
  responsiveLayout = "stacked",
}: TableProps<T>) {
  const displayTitle = emptyMessage || emptyTitle;
  const displayDesc = emptyMessage ? "" : emptyDescription;
  const totalPages = pagination ? Math.max(1, Math.ceil(pagination.total / pagination.pageSize)) : 1;
  const startItem = pagination && pagination.total > 0 ? (pagination.page - 1) * pagination.pageSize + 1 : 0;
  const endItem = pagination ? Math.min(pagination.page * pagination.pageSize, pagination.total) : data.length;

  const sortableColumns = columns.filter((col) => col.sortable);

  // Partition columns for stacked-card display:
  // Identify action columns that should render in card footer rather than key-value body
  const isActionColumn = (col: Column<T>) => {
    const k = col.key.toLowerCase();
    const h = col.header.toLowerCase();
    return (
      k === "actions" ||
      k === "action" ||
      h === "actions" ||
      h === "action" ||
      h === "details" ||
      h === "manage access"
    );
  };

  const dataColumns = columns.filter((col) => !isActionColumn(col));
  const actionColumns = columns.filter((col) => isActionColumn(col));

  return (
    <div className={cn("w-full overflow-hidden rounded-lg border border-border bg-surface shadow-sm", className)}>
        {/* ── Mobile Stacked Cards (Visible only when responsiveLayout === 'stacked' and viewport < md) ── */}
        {responsiveLayout === "stacked" && (
          <div className="block md:hidden">
            {/* Mobile Sort Controls */}
            {sortableColumns.length > 0 && onSort && (
              <div className="flex items-center justify-between gap-2.5 px-4 py-2.5 bg-surface-muted/60 border-b border-border text-xs text-text-muted">
                <div className="flex items-center space-x-1.5 flex-1 min-w-0">
                  <ArrowUpDown className="w-3.5 h-3.5 text-primary shrink-0" aria-hidden="true" />
                  <span className="font-medium text-text shrink-0 select-none">Sort:</span>
                  <select
                    value={sortKey || ""}
                    onChange={(e) => onSort(e.target.value)}
                    className="w-full max-w-[180px] px-2 py-1.5 bg-surface border border-border rounded text-text text-xs focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary min-h-[38px] sm:min-h-[32px]"
                    aria-label="Sort table by field"
                  >
                    <option value="" disabled>Select field...</option>
                    {sortableColumns.map((col) => (
                      <option key={col.key} value={col.key}>
                        {col.header}
                      </option>
                    ))}
                  </select>
                </div>

                {sortKey && (
                  <button
                    type="button"
                    onClick={() => onSort(sortKey)}
                    className="inline-flex items-center justify-center min-w-[44px] min-h-[44px] p-2 rounded border border-border bg-surface text-text hover:bg-surface-hover shrink-0 transition-colors"
                    aria-label={`Current sort: ${sortDirection || "asc"}. Click to toggle direction.`}
                  >
                    {sortDirection === "desc" ? (
                      <ChevronDown className="w-4 h-4 text-primary" aria-hidden="true" />
                    ) : (
                      <ChevronUp className="w-4 h-4 text-primary" aria-hidden="true" />
                    )}
                  </button>
                )}
              </div>
            )}

            {/* Mobile Cards Body */}
            {isLoading ? (
              <div className="p-4 space-y-3">
                {Array.from({ length: Math.min(loadingRowCount, 4) }).map((_, rIdx) => (
                  <div
                    key={`mobile-loading-card-${rIdx}`}
                    className="p-3.5 rounded-lg border border-border/80 bg-surface space-y-2.5 animate-pulse"
                  >
                    <div className="flex justify-between items-center pb-2 border-b border-border/40">
                      <Skeleton className="h-4 w-1/3" />
                      <Skeleton className="h-4 w-1/4" />
                    </div>
                    <div className="space-y-2 pt-1">
                      <Skeleton className="h-3.5 w-3/4" />
                      <Skeleton className="h-3.5 w-1/2" />
                    </div>
                  </div>
                ))}
              </div>
            ) : data.length === 0 ? (
              <div className="px-4 py-10 text-center">
                <EmptyState
                  title={displayTitle}
                  description={displayDesc}
                  icon={emptyIcon}
                  action={emptyAction}
                />
              </div>
            ) : (
              <div className="divide-y divide-border">
                {data.map((item, index) => {
                  const rowKey = keyExtractor(item, index);
                  return (
                    <div
                      key={`mobile-card-${rowKey}`}
                      role={onRowClick ? "button" : undefined}
                      tabIndex={onRowClick ? 0 : undefined}
                      onClick={() => onRowClick?.(item)}
                      onKeyDown={
                        onRowClick
                          ? (e) => {
                            if (e.key === "Enter" || e.key === " ") {
                              e.preventDefault();
                              onRowClick(item);
                            }
                          }
                          : undefined
                      }
                      className={cn(
                        "p-4 space-y-2.5 transition-colors",
                        onRowClick ? "cursor-pointer hover:bg-surface-hover active:bg-surface-hover" : ""
                      )}
                    >
                      {/* Key/Value Data Pairs */}
                      <div className="space-y-2 text-xs sm:text-sm">
                        {dataColumns.map((col) => {
                          const cellContent = col.render
                            ? col.render(item, index)
                            : (item as Record<string, unknown>)[col.key] !== undefined
                              ? String((item as Record<string, unknown>)[col.key])
                              : null;

                          return (
                            <div
                              key={`m-cell-${col.key}`}
                              className="flex items-start justify-between gap-3"
                            >
                              <span className="font-medium text-text-muted shrink-0 select-none">
                                {col.header}
                              </span>
                              <div className="text-right text-text min-w-0 break-words flex-1 flex justify-end">
                                {cellContent}
                              </div>
                            </div>
                          );
                        })}
                      </div>

                      {/* Action Column (if present) */}
                      {actionColumns.length > 0 && (
                        <div className="pt-2.5 mt-2 border-t border-border/60 flex items-center justify-end gap-2 flex-wrap">
                          {actionColumns.map((col) => (
                            <div key={`m-act-${col.key}`} className="flex items-center gap-2">
                              {col.render ? col.render(item, index) : null}
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}

        {/* ── Desktop & Tablet Table (Or all widths when responsiveLayout === 'scroll') ── */}
        <div className={cn("w-full overflow-x-auto", responsiveLayout === "stacked" ? "hidden md:block" : "block")}>
          <table className="w-full text-left text-sm border-collapse">
            <thead className="bg-surface-muted/60 border-b border-border text-xs uppercase tracking-wider text-text-muted font-semibold select-none">
              <tr>
                {columns.map((col) => {
                  const isSorted = sortKey === col.key;
                  return (
                    <th
                      key={col.key}
                      scope="col"
                      className={cn(
                        "px-4 py-3 transition-colors",
                        col.className?.includes("text-right") ? "text-right" : "text-left",
                        col.sortable && onSort ? "cursor-pointer hover:text-text hover:bg-surface-hover/50" : "",
                        col.className
                      )}
                      onClick={() => {
                        if (col.sortable && onSort) {
                          onSort(col.key);
                        }
                      }}
                    >
                      <div className={cn("inline-flex items-center space-x-1.5", col.className?.includes("text-right") ? "justify-end w-full" : "")}>
                        <span>{col.header}</span>
                        {col.sortable && (
                          <span className="inline-flex">
                            {isSorted && sortDirection === "asc" ? (
                              <ChevronUp className="w-3.5 h-3.5 text-primary" aria-label="Sorted ascending" />
                            ) : isSorted && sortDirection === "desc" ? (
                              <ChevronDown className="w-3.5 h-3.5 text-primary" aria-label="Sorted descending" />
                            ) : (
                              <ChevronsUpDown className="w-3.5 h-3.5 text-text-muted/60" aria-label="Sortable" />
                            )}
                          </span>
                        )}
                      </div>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody className="divide-y divide-border text-text">
              {isLoading ? (
                Array.from({ length: loadingRowCount }).map((_, rIdx) => (
                  <tr key={`loading-row-${rIdx}`} className="animate-pulse">
                    {columns.map((col, cIdx) => (
                      <td key={`loading-cell-${cIdx}`} className={cn("px-4 py-3.5", col.className)}>
                        <Skeleton className="h-4 w-3/4" />
                      </td>
                    ))}
                  </tr>
                ))
              ) : data.length === 0 ? (
                <tr>
                  <td colSpan={columns.length} className="px-4 py-12 text-center">
                    <EmptyState
                      title={displayTitle}
                      description={displayDesc}
                      icon={emptyIcon}
                      action={emptyAction}
                    />
                  </td>
                </tr>
              ) : (
                data.map((item, index) => (
                  <tr
                    key={keyExtractor(item, index)}
                    onClick={() => onRowClick?.(item)}
                    className={cn(
                      "transition-colors",
                      onRowClick ? "cursor-pointer hover:bg-surface-hover" : "hover:bg-surface-hover/40"
                    )}
                  >
                    {columns.map((col) => (
                      <td key={col.key} className={cn("px-4 py-3.5 align-middle", col.className)}>
                        {col.render
                          ? col.render(item, index)
                          : (item as Record<string, unknown>)[col.key] !== undefined
                            ? String((item as Record<string, unknown>)[col.key])
                            : null}
                      </td>
                    ))}
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>

        {/* ── Pagination (Shared across both stacked-cards and desktop-table views) ── */}
        {pagination && (
          <div className="flex flex-col sm:flex-row items-center justify-between gap-3 px-4 py-3 border-t border-border bg-surface-muted/30 text-xs text-text-muted">
              <div className="text-center sm:text-left">
                Showing <span className="font-medium text-text">{startItem}</span> to{" "}
                <span className="font-medium text-text">{endItem}</span> of{" "}
                <span className="font-medium text-text">{pagination.total}</span> entries
              </div>

              <div className="flex items-center space-x-2">
                {pagination.pageSizeOptions && pagination.onPageSizeChange && (
                  <div className="flex items-center space-x-1.5 mr-2">
                    <span>Rows:</span>
                    <select
                      value={pagination.pageSize}
                      onChange={(e) => pagination.onPageSizeChange!(Number(e.target.value))}
                      className="px-2 py-1 bg-surface border border-border rounded text-text focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary text-xs min-h-[38px] sm:min-h-[32px]"
                      aria-label="Rows per page"
                    >
                      {pagination.pageSizeOptions.map((opt) => (
                        <option key={opt} value={opt}>
                          {opt}
                        </option>
                      ))}
                    </select>
                  </div>
                )}

                <button
                  type="button"
                  disabled={pagination.page <= 1 || isLoading}
                  onClick={() => pagination.onPageChange(pagination.page - 1)}
                  className="inline-flex items-center justify-center min-w-[44px] min-h-[44px] sm:min-w-[36px] sm:min-h-[36px] p-2 sm:p-1.5 rounded border border-border bg-surface hover:bg-surface-hover text-text disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  aria-label="Previous page"
                >
                  <ChevronLeft className="w-4 h-4" />
                </button>

                <span className="px-2 font-medium text-text">
                  Page {pagination.page} of {totalPages}
                </span>

                <button
                  type="button"
                  disabled={pagination.page >= totalPages || isLoading}
                  onClick={() => pagination.onPageChange(pagination.page + 1)}
                  className="inline-flex items-center justify-center min-w-[44px] min-h-[44px] sm:min-w-[36px] sm:min-h-[36px] p-2 sm:p-1.5 rounded border border-border bg-surface hover:bg-surface-hover text-text disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  aria-label="Next page"
                >
                  <ChevronRight className="w-4 h-4" />
                </button>
              </div>
            </div>
      )}
          </div>
        );
}
