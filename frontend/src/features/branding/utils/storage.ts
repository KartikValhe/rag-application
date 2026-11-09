/**
 * features/branding/utils/storage.ts
 * ──────────────────────────────────
 * Safe localStorage persistence for tenant branding.
 * Handles private browsing modes and quota restrictions cleanly.
 */

import type { PublicBranding, StoredBrandingCache } from "../types.ts";
import { generateThemeCss } from "./tokens.ts";

const STORAGE_PREFIX = "app_branding_";

export function getBrandingStorageKey(slug: string): string {
  const cleanSlug = (slug || "default").trim().toLowerCase();
  return `${STORAGE_PREFIX}${cleanSlug}`;
}

export function getStoredBranding(slug: string): StoredBrandingCache | null {
  try {
    if (typeof window === "undefined" || !window.localStorage) {
      return null;
    }
    const key = getBrandingStorageKey(slug);
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;

    const parsed = JSON.parse(raw) as StoredBrandingCache;
    if (parsed && parsed.branding && typeof parsed.version === "number") {
      return parsed;
    }
    return null;
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
    const css = generateThemeCss(branding.tokens);
    const payload: StoredBrandingCache = {
      version: branding.version,
      etag: etag || null,
      branding,
      css,
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
