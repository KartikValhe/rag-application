import React, { createContext, useContext, useEffect, useState } from "react";

export type ThemeMode = "light" | "dark" | "system";

export interface ThemeContextType {
  theme: ThemeMode;
  isDark: boolean;
  setTheme: (mode: ThemeMode) => void;
  toggleTheme: () => void;
  tenantSlug: string;
  isCustomThemeApplied: boolean;
  setTenantSlug: (slug: string) => void;
}

const ThemeContext = createContext<ThemeContextType | undefined>(undefined);

export const ThemeProvider: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const [theme, setThemeState] = useState<ThemeMode>(() => {
    const stored = localStorage.getItem("app_theme") as ThemeMode | null;
    return stored || "system";
  });

  const [isDark, setIsDark] = useState<boolean>(() => {
    const stored = localStorage.getItem("app_theme");
    if (stored === "dark") return true;
    if (stored === "light") return false;
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  });

  const [tenantSlug, setTenantSlugState] = useState<string>(() => {
    const searchParams = new URLSearchParams(window.location.search);
    const param = searchParams.get("tenant") || searchParams.get("tenant_slug");
    if (param) return param;
    const stored = localStorage.getItem("tenant_slug");
    return stored || "acme";
  });

  const [isCustomThemeApplied] = useState(false);

  // Sync theme changes with DOM and localStorage
  useEffect(() => {
    const applyTheme = () => {
      let resolvedDark = false;
      if (theme === "dark") {
        resolvedDark = true;
      } else if (theme === "light") {
        resolvedDark = false;
      } else {
        resolvedDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      }

      setIsDark(resolvedDark);
      if (resolvedDark) {
        document.documentElement.classList.add("dark");
      } else {
        document.documentElement.classList.remove("dark");
      }
    };

    applyTheme();

    if (theme === "system") {
      const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
      const listener = () => applyTheme();
      mediaQuery.addEventListener("change", listener);
      return () => mediaQuery.removeEventListener("change", listener);
    }
  }, [theme]);

  const setTheme = (mode: ThemeMode) => {
    localStorage.setItem("app_theme", mode);
    setThemeState(mode);
  };

  const toggleTheme = () => {
    const nextMode: ThemeMode = isDark ? "light" : "dark";
    setTheme(nextMode);
  };

  const setTenantSlug = (slug: string) => {
    localStorage.setItem("tenant_slug", slug);
    setTenantSlugState(slug);
  };

  useEffect(() => {
    document.documentElement.setAttribute("data-tenant", tenantSlug);
  }, [tenantSlug]);

  return (
    <ThemeContext.Provider
      value={{
        theme,
        isDark,
        setTheme,
        toggleTheme,
        tenantSlug,
        isCustomThemeApplied,
        setTenantSlug,
      }}
    >
      {children}
    </ThemeContext.Provider>
  );
};

export const useTheme = (): ThemeContextType => {
  const context = useContext(ThemeContext);
  if (!context) {
    throw new Error("useTheme must be used within a ThemeProvider");
  }
  return context;
};
