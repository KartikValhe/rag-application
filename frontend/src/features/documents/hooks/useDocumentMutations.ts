import { useMutation, useQueryClient } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { getAccessToken } from "@/api/client";
import { documentsApi as defaultDocumentsApi } from "../api/documentsClient";
import type { DocumentsApi, DocumentUploadPayload } from "../api/documentsApi";
import type { DocumentVersionItem } from "../schemas/documentSchemas";

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
      // If token expires in fewer than 60s, refresh first.
      // If refresh fails with 401, do not send upload; let error propagate to onError.
      if (isTokenExpiringSoon(60)) {
        await authApi.refresh();
      }

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
