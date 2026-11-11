import React, { useState, useEffect, useRef } from "react";
import { Sidebar } from "./Sidebar";
import { Topbar } from "./Topbar";
import { cn } from "@/utils/cn";
import { useBranding } from "@/features/branding";

export interface AppShellProps {
  children: React.ReactNode;
  tenantName?: string;
  className?: string;
}

export const AppShell: React.FC<AppShellProps> = ({
  children,
  tenantName,
  className,
}) => {
  const { branding } = useBranding();
  const [isMobileOpen, setIsMobileOpen] = useState(false);
  const hamburgerRef = useRef<HTMLButtonElement>(null);

  const [isCollapsed, setIsCollapsed] = useState(() => {
    try {
      return localStorage.getItem("sidebar_collapsed") === "true";
    } catch {
      return false;
    }
  });

  const handleToggleCollapse = () => {
    setIsCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("sidebar_collapsed", String(next));
      } catch {
        // ignore
      }
      return next;
    });
  };

  // Close mobile drawer on resize to desktop
  useEffect(() => {
    const handleResize = () => {
      if (window.innerWidth >= 1024) {
        setIsMobileOpen(false);
      }
    };
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, []);

  return (
      <div className="flex min-h-dvh h-dvh w-full overflow-hidden bg-background text-text antialiased">
        {/* Sidebar Navigation */}
        <Sidebar
          isCollapsed={isCollapsed}
          onToggleCollapse={handleToggleCollapse}
          isMobileOpen={isMobileOpen}
          onCloseMobile={() => setIsMobileOpen(false)}
          hamburgerRef={hamburgerRef}
        />

        {/* Main View Area */}
        <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
          <Topbar
            tenantName={tenantName}
            onToggleMobileNav={() => setIsMobileOpen((prev) => !prev)}
            hamburgerRef={hamburgerRef}
          />

          <main
            className={cn(
              "flex-1 overflow-y-auto overflow-x-hidden px-4 py-5 sm:px-6 sm:py-6 lg:px-8 focus:outline-none flex flex-col justify-between",
              className
            )}
          >
            <div className="max-w-7xl mx-auto w-full">{children}</div>
              <footer className="max-w-7xl mx-auto w-full mt-8 sm:mt-12 pt-4 border-t border-border text-center text-xs text-text-subtle pb-[max(1rem,env(safe-area-inset-bottom))]">
                {branding.footer_text || "© 2026 RAG Platform. All rights reserved."}
              </footer>
          </main>
        </div>
      </div>
      );
};
