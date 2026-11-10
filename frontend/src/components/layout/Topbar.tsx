import React from "react";
import { Menu, PanelLeft, User } from "lucide-react";
import { useAuth } from "@/features/auth/AuthContext";
import { usePermission } from "@/hooks/usePermission";
import { Badge } from "@/components/ui/Badge";
import { ThemeToggle } from "@/components/ui/ThemeToggle";
import { useBranding } from "@/features/branding";

export interface TopbarProps {
  onToggleMobileNav: () => void;
  onToggleCollapse?: () => void;
  isSidebarCollapsed?: boolean;
  tenantName?: string;
  hamburgerRef?: React.RefObject<HTMLButtonElement>;
}

export const Topbar: React.FC<TopbarProps> = ({
  onToggleMobileNav,
  onToggleCollapse,
  isSidebarCollapsed,
  tenantName,
  hamburgerRef,
}) => {
  const { user } = useAuth();
  const { isAdmin, isTeacher, isStudent } = usePermission();
  const { branding } = useBranding();

  const primaryRoleLabel = isAdmin
    ? "Admin"
    : isTeacher
      ? "Teacher"
      : isStudent
        ? "Student"
        : "Member";

  const primaryRoleVariant = isAdmin
    ? "danger"
    : isTeacher
      ? "primary"
      : isStudent
        ? "success"
        : "default";

  const displayName = tenantName || branding.display_name || "Academic Portal";

  return (
      <header className="sticky top-0 z-30 h-16 bg-surface/95 backdrop-blur-xs border-b border-border px-3 sm:px-6 flex items-center justify-between transition-colors">
        {/* Left: Mobile hamburger + Desktop toggle + Organization info */}
          <div className="flex items-center space-x-2 sm:space-x-3 min-w-0 pr-2">
            {/* Mobile menu trigger: 44px min touch target */}
            <button
              ref={hamburgerRef}
              type="button"
              onClick={onToggleMobileNav}
              className="lg:hidden min-w-[44px] min-h-[44px] flex items-center justify-center rounded-md text-text-muted hover:text-text hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary cursor-pointer shrink-0"
              aria-label="Open mobile navigation"
            >
              <Menu className="w-5 h-5" />
            </button>

            {/* Desktop Sidebar Toggle in Top Left Corner */}
            {onToggleCollapse && (
              <button
                type="button"
                onClick={onToggleCollapse}
                className="hidden lg:flex min-w-[44px] min-h-[44px] items-center justify-center rounded-md text-text-muted hover:text-text hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary cursor-pointer transition-colors shrink-0"
                aria-label={isSidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
                title={isSidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
              >
                <PanelLeft className="w-4 h-4" />
              </button>
            )}

                <div className="flex items-center space-x-2 min-w-0">
                  <span
                    className="font-semibold text-sm text-text truncate max-w-[120px] sm:max-w-xs md:max-w-sm"
                    title={displayName}
                  >
                    {displayName}
                  </span>
                    <span className="hidden sm:inline-block w-1.5 h-1.5 rounded-full bg-border shrink-0" />
                    <span className="hidden sm:inline-block text-xs text-text-muted shrink-0">
                      Workspace
                    </span>
                </div>
            </div>

            {/* Right: Theme switch, user role badge, user identifier */}
              <div className="flex items-center space-x-2 sm:space-x-4 shrink-0">
                {/* Light / Dark Mode Toggle */}
                <div className="flex items-center justify-center min-w-[44px] min-h-[44px]">
                  <ThemeToggle />
                </div>

                {/* User Role Badge */}
                {user && (
          <div className="shrink-0">
            <Badge variant={primaryRoleVariant} size="sm">
              {primaryRoleLabel}
            </Badge>
          </div>
                )}

                {/* User Identity (Desktop only) */}
                {user && (
                  <div className="hidden md:flex items-center space-x-2 text-xs text-text-muted border-l border-border pl-3">
                      <div className="w-7 h-7 rounded-full bg-surface-muted border border-border flex items-center justify-center text-text font-medium shrink-0">
                        <User className="w-4 h-4 text-text-muted" />
                      </div>
                      <span className="max-w-[140px] truncate font-medium text-text">
                        {user.email || user.id.slice(0, 8)}
                      </span>
                    </div>
        )}
                  </div>
    </header>
              );
};
