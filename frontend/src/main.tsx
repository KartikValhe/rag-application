import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import "@/styles/theme.css";
import { getTenantSlug } from "@/api/client";
import {
  getStoredBranding,
  saveStoredBranding,
  fetchPublicBranding,
  applyThemeTokens,
  DEFAULT_BRANDING,
} from "@/features/branding";

async function bootApp() {
  const rootElement = document.getElementById("root");
  if (!rootElement) {
    throw new Error("Failed to find the root element");
  }

    // Pre-render boot initialization: Ensure branding tokens are loaded before first paint
    const slug = getTenantSlug();
    const cached = getStoredBranding(slug);

    if (cached?.branding) {
      applyThemeTokens(cached.branding.tokens);
      if (cached.branding.display_name) {
        document.title = cached.branding.display_name;
      }
    } else {
      try {
        // First visit on this hostname: fetch public branding before first render
        const res = await fetchPublicBranding(null, slug);
        if (res.branding) {
          saveStoredBranding(slug, res.branding, res.etag);
          applyThemeTokens(res.branding.tokens);
          if (res.branding.display_name) {
            document.title = res.branding.display_name;
          }
        } else {
          applyThemeTokens(DEFAULT_BRANDING.tokens);
        }
      } catch {
        // Offline / server unreachable: fall back to default tokens gracefully
        applyThemeTokens(DEFAULT_BRANDING.tokens);
      }
    }

    ReactDOM.createRoot(rootElement).render(
      <React.StrictMode>
        <App />
      </React.StrictMode>
    );
  }

  bootApp();
