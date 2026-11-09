/**
 * features/branding/context/BrandingContext.tsx
 * ─────────────────────────────────────────────
 * React context and provider for tenant branding:
 * - Boot initialization with zero-flash cache hydration.
 * - Dynamic document title and dynamic favicon fallback.
 * - Background ETag revalidation.
 * - Graceful offline fallback.
 */

import React, {
  createContext,
  useContext,
  useEffect,
  useState,
  useCallback,
} from "react";
import { PublicBranding, DEFAULT_BRANDING } from "../types";
import { applyThemeTokens } from "../utils/tokens";
import {
  getStoredBranding,
  saveStoredBranding,
} from "../utils/storage";
import { fetchPublicBranding } from "../api/brandingApi";
import { getTenantSlug } from "@/api/client";
import { useTheme } from "@/theme/ThemeProvider";

export interface BrandingContextType {
  branding: PublicBranding;
  isLoading: boolean;
  isCustomTheme: boolean;
  tenantSlug: string;
  refreshBranding: () => Promise<void>;
}

const BrandingContext = createContext<BrandingContextType | undefined>(
  undefined
);

const DEFAULT_FAVICON = "/vite.svg";

function updateFavicon(url: string | null): void {
  if (typeof document === "undefined") return;

  let link = document.querySelector(
    "link[rel~='icon']"
  ) as HTMLLinkElement | null;
  if (!link) {
    link = document.createElement("link");
    link.rel = "icon";
    document.head.appendChild(link);
  }

  if (!url) {
    link.href = DEFAULT_FAVICON;
    return;
  }

  // Pre-load image to verify validity before replacing favicon
  const img = new Image();
  img.onload = () => {
    if (link) link.href = url;
  };
  img.onerror = () => {
    if (link) link.href = DEFAULT_FAVICON;
  };
  img.src = url;
}

export const BrandingProvider: React.FC<{
  children: React.ReactNode;
  tenantSlug?: string;
}> = ({ children, tenantSlug: propTenantSlug }) => {
  let themeTenantSlug = "";
  try {
    themeTenantSlug = useTheme().tenantSlug;
  } catch {
    // BrandingProvider can be used outside ThemeProvider
  }

  const activeTenantSlug =
    propTenantSlug || themeTenantSlug || getTenantSlug();

  // Hydrate initial state synchronously from localStorage to prevent flash
  const [branding, setBranding] = useState<PublicBranding>(() => {
    const cached = getStoredBranding(activeTenantSlug);
    if (cached?.branding) {
      applyThemeTokens(cached.branding.tokens);
      return cached.branding;
    }
    applyThemeTokens(DEFAULT_BRANDING.tokens);
    return DEFAULT_BRANDING;
  });

  const [isLoading, setIsLoading] = useState<boolean>(false);

  // Check if current theme has customizations
  const isCustomTheme =
    branding.tokens.primary !== DEFAULT_BRANDING.tokens.primary ||
    branding.tokens.secondary !== DEFAULT_BRANDING.tokens.secondary ||
    branding.tokens.radius !== DEFAULT_BRANDING.tokens.radius ||
    branding.tokens.font_family !== DEFAULT_BRANDING.tokens.font_family ||
    !!branding.logo_url;

  // Sync document title and favicon
  useEffect(() => {
    if (branding.display_name) {
      document.title = branding.display_name;
    }
    updateFavicon(branding.favicon_url);
  }, [branding.display_name, branding.favicon_url]);

  // Revalidate branding with server in background
  const revalidate = useCallback(
    async (showLoading = false) => {
      if (showLoading) setIsLoading(true);

      const cached = getStoredBranding(activeTenantSlug);
      const currentEtag = cached?.etag;

      try {
        const result = await fetchPublicBranding(currentEtag, activeTenantSlug);

        if (!result.notModified && result.branding) {
          const fresh = result.branding;
          setBranding(fresh);
          saveStoredBranding(activeTenantSlug, fresh, result.etag);
          applyThemeTokens(fresh.tokens);
        }
      } catch {
        // Offline or server unreachable: keep cached/default branding intact
      } finally {
        if (showLoading) setIsLoading(false);
      }
    },
    [activeTenantSlug]
  );

  // Sync when tenant slug changes
  useEffect(() => {
    const cached = getStoredBranding(activeTenantSlug);
    if (cached?.branding) {
      setBranding(cached.branding);
      applyThemeTokens(cached.branding.tokens);
    } else {
      setBranding(DEFAULT_BRANDING);
      applyThemeTokens(DEFAULT_BRANDING.tokens);
    }
    revalidate();
  }, [activeTenantSlug, revalidate]);

  return (
    <BrandingContext.Provider
      value={{
        branding,
        isLoading,
        isCustomTheme,
        tenantSlug: activeTenantSlug,
        refreshBranding: () => revalidate(true),
      }}
    >
      {children}
    </BrandingContext.Provider>
  );
};

export const useBranding = (): BrandingContextType => {
  const context = useContext(BrandingContext);
  if (!context) {
    throw new Error("useBranding must be used within a BrandingProvider");
  }
  return context;
};
