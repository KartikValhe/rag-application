import { useQuery } from "@tanstack/react-query";
import { documentsApi as defaultDocumentsApi } from "../api/documentsClient";
import type { DocumentsApi } from "../api/documentsApi";

export interface UseDocumentVersionsOptions {
  api?: DocumentsApi;
  documentId: string;
  enabled?: boolean;
}

/** Query key of the version list; the mutation hooks invalidate it, so it lives in one place. */
export const documentVersionsKey = (documentId: string) => ["documentVersions", documentId] as const;

/**
 * Every version of one document, newest first (at most 100; `has_more` says older ones were left out).
 * Read-only. It has no timer of its own: it is refetched after a mutation and when a polled
 * status becomes terminal.
 */
export function useDocumentVersions({ api: customApi, documentId, enabled = true }: UseDocumentVersionsOptions) {
  const api = customApi ?? defaultDocumentsApi;
  return useQuery({
    queryKey: documentVersionsKey(documentId),
    queryFn: () => api.versions(documentId),
    enabled: Boolean(documentId) && enabled,
    retry: false,
  });
}
