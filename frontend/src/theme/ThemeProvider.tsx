import React, { createContext, useContext, useEffect, useState } from "react";

export interface ThemeContextType {
  tenantSlug: string;
  isCustomThemeApplied: boolean;
  setTenantSlug: (slug: string) => void;
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

export const ThemeProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [tenantSlug, setTenantSlugState] = useState<string>(() => {
    const searchParams = new URLSearchParams(window.location.search);
    const param = searchParams.get("tenant") || searchParams.get("tenant_slug");
    if (param) return param;
    const stored = localStorage.getItem("tenant_slug");
    return stored || "acme";
  });

  const [isCustomThemeApplied] = useState(false);

  const setTenantSlug = (slug: string) => {
    localStorage.setItem("tenant_slug", slug);
    setTenantSlugState(slug);
  };

  useEffect(() => {
    // Phase 1 placeholder: ensures root tokens are ready
    // Phase 3 will fetch GET /api/public/branding and write CSS variables to document.documentElement
    document.documentElement.setAttribute("data-tenant", tenantSlug);
  }, [tenantSlug]);

  return (
    <ThemeContext.Provider
      value={{
        tenantSlug,
        isCustomThemeApplied,
        setTenantSlug,
      }}
    >
      {children}
    </ThemeContext.Provider>
  );
};

export const useTheme = () => {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return context;
};

