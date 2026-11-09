/**
 * features/branding/types.ts
 * ─────────────────────────
 * Type definitions for white-label theming and public branding.
 */

export type RadiusToken = "none" | "sm" | "md" | "lg" | "xl" | "full";
export type FontFamilyToken = "system-ui" | "sans" | "serif" | "mono";

export interface DesignTokens {
  primary: string;
  secondary: string;
  accent: string;
  radius: RadiusToken;
  font_family: FontFamilyToken;
}

export interface PublicBranding {
  display_name: string;
  tagline: string | null;
  footer_text: string | null;
  support_email: string | null;
  tokens: DesignTokens;
  logo_url: string | null;
  favicon_url: string | null;
  version: number;
}

export interface BrandingResponse {
  display_name: string;
  tagline: string | null;
  footer_text: string | null;
  support_email: string | null;
  tokens: DesignTokens;
  logo_key: string | null;
  logo_url: string | null;
  favicon_key: string | null;
  favicon_url: string | null;
  version: number;
  updated_at: string;
}

export interface BrandingUpdateRequest {
  display_name?: string | null;
  tagline?: string | null;
  footer_text?: string | null;
  support_email?: string | null;
  tokens?: DesignTokens;
  expected_version?: number;
}

export interface StoredBrandingCache {
  version: number;
  etag?: string | null;
  branding: PublicBranding;
  cachedAt: number;
}

export const DEFAULT_TOKENS: DesignTokens = {
  primary: "#2563eb",
  secondary: "#475569",
  accent: "#06b6d4",
  radius: "md",
  font_family: "system-ui",
};

export const DEFAULT_BRANDING: PublicBranding = {
  display_name: "RAG Platform",
  tagline: "Academic Workspace & AI Learning Platform",
  footer_text: "© 2026 RAG Platform. All rights reserved.",
  support_email: "support@ragplatform.local",
  tokens: DEFAULT_TOKENS,
  logo_url: null,
  favicon_url: null,
  version: 1,
};
