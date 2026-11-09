/**
 * features/branding/utils/storage.ts
 * ──────────────────────────────────
 * Safe, hardened localStorage persistence for tenant branding.
 * - Caches ONLY validated tokens and text (NEVER trusts raw cached CSS).
 * - Discards invalid or unknown version caches.
 * - Handles private browsing modes cleanly.
 */

import type { PublicBranding, StoredBrandingCache, DesignTokens } from "../types.ts";
import { isValidHex, RADIUS_ALLOWLIST, FONT_FAMILY_ALLOWLIST } from "./tokens.ts";

const STORAGE_PREFIX = "app_branding_";

export function getBrandingStorageKey(slug: string): string {
  const cleanSlug = (slug || "default").trim().toLowerCase();
  return `${STORAGE_PREFIX}${cleanSlug}`;
}

export function isValidStoredBranding(data: unknown): data is StoredBrandingCache {
  if (!data || typeof data !== "object") return false;
  const d = data as Partial<StoredBrandingCache>;
  if (typeof d.version !== "number" || d.version < 1) return false;
  if (!d.branding || typeof d.branding !== "object") return false;

  const b = d.branding as Partial<PublicBranding>;
  if (typeof b.display_name !== "string") return false;
  if (!b.tokens || typeof b.tokens !== "object") return false;

  const t = b.tokens as Partial<DesignTokens>;
  if (!isValidHex(t.primary) || !isValidHex(t.secondary) || !isValidHex(t.accent)) {
    return false;
  }
  if (!t.radius || !(t.radius in RADIUS_ALLOWLIST)) return false;
  if (!t.font_family || !(t.font_family in FONT_FAMILY_ALLOWLIST)) return false;

  return true;
}

export function getStoredBranding(slug: string): StoredBrandingCache | null {
  try {
    if (typeof window === "undefined" || !window.localStorage) {
      return null;
    }
    const key = getBrandingStorageKey(slug);
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;

      const parsed = JSON.parse(raw);
      if (!isValidStoredBranding(parsed)) {
        // Discard invalid or tainted cache
        window.localStorage.removeItem(key);
        return null;
      }
      return parsed;
    } catch {
      // Private browsing or disabled storage
      return null;
    }
  }

export function saveStoredBranding(
    slug: string,
    branding: PublicBranding,
    etag?: string | null
  ): void {
    try {
      if (typeof window === "undefined" || !window.localStorage) {
        return;
      }
      const key = getBrandingStorageKey(slug);
      // Hardening: Store only validated structured fields, never raw CSS strings
      const payload: StoredBrandingCache = {
        version: branding.version,
        etag: etag || null,
        branding,
        cachedAt: Date.now(),
      };
      window.localStorage.setItem(key, JSON.stringify(payload));
    } catch {
      // Quota exceeded or private browsing - fail silently
    }
  }

  export function clearStoredBranding(slug: string): void {
    try {
      if (typeof window === "undefined" || !window.localStorage) {
        return;
      }
      const key = getBrandingStorageKey(slug);
      window.localStorage.removeItem(key);
    } catch {
      // Fail silently
    }
  }
