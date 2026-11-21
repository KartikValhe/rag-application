import { useMemo, useCallback } from "react";
import { useSearchParams } from "react-router-dom";
import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { documentsApi as defaultDocumentsApi } from "../api/documentsClient";
import type { DocumentsApi } from "../api/documentsApi";
import {
  academicApi,
  type MyClassItem,
} from "@/features/academic/api/academicApi";
import type { DocumentItem } from "../schemas/documentSchemas";

export interface UseDocumentsOptions {
  api?: DocumentsApi;
}

export function useDocuments(options: UseDocumentsOptions = {}) {
  const api = options.api ?? defaultDocumentsApi;
  const [searchParams, setSearchParams] = useSearchParams();

  const filterClass = searchParams.get("class") || "";
  const filterSubject = searchParams.get("subject") || "";
  const filterStatus = searchParams.get("status") || "";

  // 1. Fetch user classes for filters
  const classesQuery = useQuery({
    queryKey: ["myClasses"],
    queryFn: () => academicApi.getMyClasses(),
    staleTime: 5 * 60 * 1000,
  });

  const classes: MyClassItem[] = classesQuery.data ?? [];

  // 2. Compute available subjects based on the chosen class
  const availableSubjects = useMemo(() => {
    if (!filterClass) {
      return [];
    }
    const cls = classes.find((c) => c.id === filterClass);
    return cls ? cls.subjects : [];
  }, [classes, filterClass]);

  // 3. Infinite query for documents
  const documentsQuery = useInfiniteQuery({
    queryKey: [
      "documents",
      {
        class_id: filterClass || undefined,
        subject_id: filterSubject || undefined,
        status: filterStatus || undefined,
      },
    ],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      api.list({
        class_id: filterClass || undefined,
        subject_id: filterSubject || undefined,
        status: filterStatus || undefined,
        cursor: pageParam,
        limit: 20,
      }),
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
  });

  const documents: DocumentItem[] = useMemo(() => {
    return documentsQuery.data?.pages.flatMap((page) => page.items) ?? [];
  }, [documentsQuery.data]);

  const setFilter = useCallback(
    (key: "class" | "subject" | "status", value: string) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev);
        if (value) {
          next.set(key, value);
        } else {
          next.delete(key);
        }
        if (key === "class") {
          next.delete("subject");
        }
        return next;
      });
    },
    [setSearchParams]
  );

  const clearFilters = useCallback(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.delete("class");
      next.delete("subject");
      next.delete("status");
      return next;
    });
  }, [setSearchParams]);

  const hasActiveFilters = Boolean(filterClass || filterSubject || filterStatus);

  return {
    documents,
    classes,
    availableSubjects,
    filterClass,
    filterSubject,
    filterStatus,
    hasActiveFilters,
    setFilter,
    clearFilters,
    isLoading: documentsQuery.isLoading || classesQuery.isLoading,
    isInitialLoading: documentsQuery.isLoading,
    isFetchingNextPage: documentsQuery.isFetchingNextPage,
    hasNextPage: Boolean(documentsQuery.hasNextPage),
    fetchNextPage: documentsQuery.fetchNextPage,
    isError: documentsQuery.isError,
    error: documentsQuery.error,
    refetch: documentsQuery.refetch,
  };
}
