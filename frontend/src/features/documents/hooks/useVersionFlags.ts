import { useMemo } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { documentsApi as defaultDocumentsApi } from "../api/documentsClient.ts";
import type { DocumentsApi } from "../api/documentsApi.ts";
import type { FlaggedChunkItem, VersionFlagsItem } from "../schemas/documentSchemas.ts";

export interface UseVersionFlagsOptions {
  documentId: string;
  versionId?: string | null;
  api?: DocumentsApi;
  enabled?: boolean;
}

export function useVersionFlags(options: UseVersionFlagsOptions) {
  const { documentId, versionId, api: customApi, enabled = true } = options;
  const api = customApi ?? defaultDocumentsApi;

  const flagsQuery = useInfiniteQuery({
    queryKey: ["documentFlags", documentId, versionId],
    initialPageParam: undefined as number | null | undefined,
    queryFn: ({ pageParam }) =>
      api.flags(documentId, versionId!, {
        afterOrdinal: pageParam,
        limit: 50,
      }),
    getNextPageParam: (lastPage: VersionFlagsItem) =>
      lastPage.next_after_ordinal != null ? lastPage.next_after_ordinal : undefined,
    enabled: Boolean(documentId && versionId) && enabled,
    retry: false,
  });

  const allItems: FlaggedChunkItem[] = useMemo(() => {
    return flagsQuery.data?.pages.flatMap((page) => page.items) ?? [];
  }, [flagsQuery.data]);

  const firstPage = flagsQuery.data?.pages[0];

  return {
    ...flagsQuery,
    items: allItems,
    firstPage,
    flaggedChunkCount: firstPage?.flagged_chunk_count,
    titleFlagged: firstPage?.title_flagged ?? false,
    filenameFlagged: firstPage?.filename_flagged ?? false,
    versionStatus: firstPage?.version_status,
  };
}
