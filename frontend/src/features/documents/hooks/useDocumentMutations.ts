import { useMutation, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { getAccessToken } from "@/api/client";
import { documentsApi as defaultDocumentsApi } from "../api/documentsClient";
import type { DocumentsApi, DocumentUploadPayload, NewVersionPayload } from "../api/documentsApi";
import type { DocumentVersionItem } from "../schemas/documentSchemas";
import { getDocumentError } from "../utils/documentErrors";

export interface UseUploadDocumentOptions {
  api?: DocumentsApi;
  onSuccess?: (data: DocumentVersionItem) => void;
  onError?: (error: unknown) => void;
}

export interface UploadMutationArgs {
  form: FormData | DocumentUploadPayload;
  onProgress?: (progress: number) => void;
  signal?: AbortSignal;
}

/**
 * Reads token exp directly from getAccessToken() (not stale AuthUser.exp).
 */
export function getTokenExp(): number | null {
  const token = getAccessToken();
  if (!token) return null;
  try {
    const parts = token.split(".");
    if (parts.length === 3) {
      const base64 = parts[1].replace(/-/g, "+").replace(/_/g, "/");
      const payload = JSON.parse(atob(base64));
      if (typeof payload.exp === "number") {
        return payload.exp;
      }
    }
  } catch {
    // ignore decode errors
  }
  return null;
}

/**
 * Checks whether the access token expires within `thresholdSeconds` (default 60s).
 */
export function isTokenExpiringSoon(thresholdSeconds = 60): boolean {
  const exp = getTokenExp();
  if (exp === null) return false;
  const now = Math.floor(Date.now() / 1000);
  return exp - now < thresholdSeconds;
}

/** If the access token expires within 60 s, refresh it first; a failed refresh stops the upload. */
async function ensureFreshToken(): Promise<void> {
  if (isTokenExpiringSoon(60)) {
    await authApi.refresh();
  }
}

/**
 * Central hook for document upload mutations.
 * Enforces:
 * - Pre-upload token freshness (refreshes when exp < 60s)
 * - Zero auto-retries (retry: false)
 * - Cache invalidation on success
 */
export function useUploadDocument(options: UseUploadDocumentOptions = {}) {
  const { api = defaultDocumentsApi, onSuccess, onError } = options;
  const queryClient = useQueryClient();

  return useMutation({
    retry: false, // Never auto-retry an upload (rate limits count attempts)
    mutationFn: async ({ form, onProgress, signal }: UploadMutationArgs) => {
      // If refresh fails with 401, do not send upload; let error propagate to onError.
      await ensureFreshToken();
      return await api.upload(form, { onProgress, signal });
    },
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      onSuccess?.(data);
    },
    onError: (err) => {
      onError?.(err);
    },
  });
}

export interface VersionMutationArgs {
  documentId: string;
  versionId: string;
}

function invalidateDocument(queryClient: QueryClient, documentId: string) {
  queryClient.invalidateQueries({ queryKey: ["documents"] });
  queryClient.invalidateQueries({ queryKey: ["document", documentId] });
}

function isManageDenied(err: unknown): boolean {
  const info = getDocumentError(err);
  return info.code === "document_manage_requires_all_classes";
}

export interface UseRetryVersionOptions {
  api?: DocumentsApi;
  onSuccess?: (data: DocumentVersionItem, args: VersionMutationArgs) => void;
  onError?: (error: unknown, args: VersionMutationArgs) => void;
}

/**
 * Retry a failed version by hand (202, the version goes back to Waiting).
 * - Never retried automatically (retry: false; the server rate-limits manual retries).
 * - No optimistic update: the list and the document are refetched after success.
 * - 403 document_manage_requires_all_classes refetches the document (its can_manage changed).
 */
export function useRetryVersion(options: UseRetryVersionOptions = {}) {
  const { api = defaultDocumentsApi, onSuccess, onError } = options;
  const queryClient = useQueryClient();

  return useMutation({
    retry: false,
    mutationFn: ({ documentId, versionId }: VersionMutationArgs) =>
      api.retryVersion(documentId, versionId),
    onSuccess: (data, args) => {
      invalidateDocument(queryClient, args.documentId);
      queryClient.invalidateQueries({
        queryKey: ["documentVersionStatus", args.documentId, args.versionId],
      });
      onSuccess?.(data, args);
    },
    onError: (err, args) => {
      if (isManageDenied(err)) invalidateDocument(queryClient, args.documentId);
      onError?.(err, args);
    },
  });
}

export interface UseDeleteVersionOptions {
  api?: DocumentsApi;
  onSuccess?: (result: { alreadyRemoved: boolean }, args: VersionMutationArgs) => void;
  onError?: (error: unknown, args: VersionMutationArgs) => void;
}

/**
 * Delete a ready or failed version (204).
 * - A 404 after the click means someone else removed it: that counts as success ("already removed").
 * - 403 document_manage_requires_all_classes refetches the document and is not retried.
 */
export function useDeleteVersion(options: UseDeleteVersionOptions = {}) {
  const { api = defaultDocumentsApi, onSuccess, onError } = options;
  const queryClient = useQueryClient();

  return useMutation({
    retry: false,
    mutationFn: async ({ documentId, versionId }: VersionMutationArgs) => {
      try {
        await api.deleteVersion(documentId, versionId);
        return { alreadyRemoved: false };
      } catch (err) {
        const status = (err as { response?: { status?: number } })?.response?.status;
        if (status === 404) return { alreadyRemoved: true };
        throw err;
      }
    },
    onSuccess: (result, args) => {
      invalidateDocument(queryClient, args.documentId);
      queryClient.removeQueries({
        queryKey: ["documentVersionStatus", args.documentId, args.versionId],
      });
      onSuccess?.(result, args);
    },
    onError: (err, args) => {
      if (isManageDenied(err)) invalidateDocument(queryClient, args.documentId);
      onError?.(err, args);
    },
  });
}

export interface UploadVersionMutationArgs {
  documentId: string;
  form: FormData | NewVersionPayload;
  onProgress?: (progress: number) => void;
  signal?: AbortSignal;
}

export interface UseUploadVersionOptions {
  api?: DocumentsApi;
  onSuccess?: (data: DocumentVersionItem) => void;
  onError?: (error: unknown) => void;
}

/**
 * Upload a new version of an existing document (only the file is sent).
 * Same rules as a new document: token refresh first, never retried automatically, no optimistic update.
 * After success the list and the document are refetched. After 409 document_processing_in_progress or
 * 403 document_manage_requires_all_classes the document is refetched too, so its buttons catch up.
 */
export function useUploadVersion(options: UseUploadVersionOptions = {}) {
  const { api = defaultDocumentsApi, onSuccess, onError } = options;
  const queryClient = useQueryClient();

  return useMutation({
    retry: false,
    mutationFn: async ({ documentId, form, onProgress, signal }: UploadVersionMutationArgs) => {
      await ensureFreshToken();
      return await api.uploadVersion(documentId, form, { onProgress, signal });
    },
    onSuccess: (data, args) => {
      invalidateDocument(queryClient, args.documentId);
      onSuccess?.(data);
    },
    onError: (err, args) => {
      const code = getDocumentError(err).code;
      if (code === "document_manage_requires_all_classes" || code === "document_processing_in_progress") {
        invalidateDocument(queryClient, args.documentId);
      }
      onError?.(err);
    },
  });
}

export interface DocumentMutationArgs {
  documentId: string;
}

export interface UseDeleteDocumentOptions {
  api?: DocumentsApi;
  onSuccess?: (result: { alreadyRemoved: boolean }, args: DocumentMutationArgs) => void;
  onError?: (error: unknown, args: DocumentMutationArgs) => void;
}

/**
 * Delete a whole document (204).
 * - A 404 after the click means someone else removed it: that counts as success ("already removed").
 * - 403 document_manage_requires_all_classes refetches the document and is not retried.
 * - On success the list is refetched and the document is dropped from the cache (a refetch would only 404).
 */
export function useDeleteDocument(options: UseDeleteDocumentOptions = {}) {
  const { api = defaultDocumentsApi, onSuccess, onError } = options;
  const queryClient = useQueryClient();

  return useMutation({
    retry: false,
    mutationFn: async ({ documentId }: DocumentMutationArgs) => {
      try {
        await api.deleteDocument(documentId);
        return { alreadyRemoved: false };
      } catch (err) {
        const status = (err as { response?: { status?: number } })?.response?.status;
        if (status === 404) return { alreadyRemoved: true };
        throw err;
      }
    },
    onSuccess: (result, args) => {
      queryClient.invalidateQueries({ queryKey: ["documents"] });
      queryClient.removeQueries({ queryKey: ["document", args.documentId] });
      queryClient.removeQueries({ queryKey: ["documentVersionStatus", args.documentId] });
      onSuccess?.(result, args);
    },
    onError: (err, args) => {
      if (isManageDenied(err)) invalidateDocument(queryClient, args.documentId);
      onError?.(err, args);
    },
  });
}
