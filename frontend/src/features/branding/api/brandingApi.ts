/**
 * features/branding/api/brandingApi.ts
 * ────────────────────────────────────
 * API calls for fetching public tenant branding with ETag / 304 support.
 */

import { apiClient } from "@/api/client";
import { PublicBranding } from "../types";

export interface FetchBrandingResult {
  branding: PublicBranding | null;
  notModified: boolean;
  etag: string | null;
}

export async function fetchPublicBranding(
  etag?: string | null,
  tenantSlug?: string | null
): Promise<FetchBrandingResult> {
  const headers: Record<string, string> = {};

  if (etag) {
    headers["If-None-Match"] = etag;
  }
  if (tenantSlug && tenantSlug !== "default") {
    headers["X-Tenant-Slug"] = tenantSlug;
  }

  const response = await apiClient.get<PublicBranding>("/public/branding", {
    headers,
    // Allow 304 Not Modified without throwing AxiosError
    validateStatus: (status: number) =>
      (status >= 200 && status < 300) || status === 304,
  });

  if (response.status === 304) {
    return {
      branding: null,
      notModified: true,
      etag: etag || null,
    };
  }

  const responseEtag = (response.headers["etag"] as string) || null;

  return {
    branding: response.data,
    notModified: false,
    etag: responseEtag,
  };
}
