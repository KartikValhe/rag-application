/**
 * features/branding/api/brandingApi.ts
 * ────────────────────────────────────
 * API calls for fetching public tenant branding with ETag / 304 support,
 * and admin endpoints for managing branding, tokens, and asset uploads.
 */

import { apiClient } from "@/api/client";
import type {
  PublicBranding,
  BrandingResponse,
  BrandingUpdateRequest,
} from "../types.ts";

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

export async function getAdminBranding(): Promise<BrandingResponse> {
  const response = await apiClient.get<BrandingResponse>("/admin/branding");
  return response.data;
}

export async function updateAdminBranding(
  payload: BrandingUpdateRequest
): Promise<BrandingResponse> {
  const response = await apiClient.put<BrandingResponse>(
    "/admin/branding",
    payload
  );
  return response.data;
}

export async function uploadAdminLogo(file: File): Promise<BrandingResponse> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await apiClient.post<BrandingResponse>(
    "/admin/branding/logo",
    formData,
    {
      headers: {
        "Content-Type": "multipart/form-data",
      },
    }
  );
  return response.data;
}

export async function uploadAdminFavicon(
  file: File
): Promise<BrandingResponse> {
  const formData = new FormData();
  formData.append("file", file);
  const response = await apiClient.post<BrandingResponse>(
    "/admin/branding/favicon",
    formData,
    {
      headers: {
        "Content-Type": "multipart/form-data",
      },
    }
  );
  return response.data;
}

export async function resetAdminBranding(): Promise<BrandingResponse> {
  const response = await apiClient.post<BrandingResponse>(
    "/admin/branding/reset"
  );
  return response.data;
}
