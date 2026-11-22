import { useCallback, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { documentsApi as defaultDocumentsApi } from "../api/documentsClient";
import type { DocumentsApi } from "../api/documentsApi";

export interface UseDocumentUsageOptions {
  api?: DocumentsApi;
}

/**
 * Read-only usage figures for the admin usage page.
 * No polling and no automatic refetch (not on focus, not on reconnect): the figures change only when
 * the person presses Refresh. `refresh` ignores a second press while one request is already running.
 */
export function useDocumentUsage(options: UseDocumentUsageOptions = {}) {
  const api = options.api ?? defaultDocumentsApi;
  const inFlightRef = useRef(false);

  const query = useQuery({
    queryKey: ["documentUsage"],
    queryFn: () => api.usage(),
    retry: false,
    staleTime: Infinity,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });

  const { refetch } = query;
  const refresh = useCallback(async () => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      await refetch();
    } finally {
      inFlightRef.current = false;
    }
  }, [refetch]);

  return {
    usage: query.data ?? null,
    /** When the shown figures were fetched (a Date), or null before the first success. */
    fetchedAt: query.dataUpdatedAt > 0 ? new Date(query.dataUpdatedAt) : null,
    isLoading: query.isLoading,
    isRefreshing: query.isFetching && !query.isLoading,
    isError: query.isError,
    error: query.error,
    refresh,
  };
}
