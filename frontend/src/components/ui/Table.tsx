import React from "react";
import { ChevronDown, ChevronUp, ChevronsUpDown, ChevronLeft, ChevronRight, LucideIcon } from "lucide-react";
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
}: TableProps<T>) {
  const displayTitle = emptyMessage || emptyTitle;
  const displayDesc = emptyMessage ? "" : emptyDescription;
  const totalPages = pagination ? Math.max(1, Math.ceil(pagination.total / pagination.pageSize)) : 1;
  const startItem = pagination && pagination.total > 0 ? (pagination.page - 1) * pagination.pageSize + 1 : 0;
  const endItem = pagination ? Math.min(pagination.page * pagination.pageSize, pagination.total) : data.length;

  return (
    <div className={cn("w-full overflow-hidden rounded-lg border border-border bg-surface shadow-sm", className)}>
      <div className="w-full overflow-x-auto">
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
                      "px-4 py-3 text-left transition-colors",
                      col.sortable && onSort ? "cursor-pointer hover:text-text hover:bg-surface-hover/50" : "",
                      col.className
                    )}
                    onClick={() => {
                      if (col.sortable && onSort) {
                        onSort(col.key);
                      }
                    }}
                  >
                    <div className="inline-flex items-center space-x-1.5">
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

      {pagination && (
        <div className="flex flex-col sm:flex-row items-center justify-between gap-3 px-4 py-3 border-t border-border bg-surface-muted/30 text-xs text-text-muted">
          <div>
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
                  className="px-2 py-1 bg-surface border border-border rounded text-text focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary text-xs"
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
              className="inline-flex items-center justify-center p-1.5 rounded border border-border bg-surface hover:bg-surface-hover text-text disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
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
              className="inline-flex items-center justify-center p-1.5 rounded border border-border bg-surface hover:bg-surface-hover text-text disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
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
