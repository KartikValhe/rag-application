import React from "react";
import { Menu, PanelLeft, User } from "lucide-react";
import { useAuth } from "@/features/auth/AuthContext";
import { usePermission } from "@/hooks/usePermission";
import { Badge } from "@/components/ui/Badge";
import { ThemeToggle } from "@/components/ui/ThemeToggle";

export interface TopbarProps {
  onToggleMobileNav: () => void;
  onToggleCollapse?: () => void;
  isSidebarCollapsed?: boolean;
  tenantName?: string;
}

export const Topbar: React.FC<TopbarProps> = ({
  onToggleMobileNav,
  onToggleCollapse,
  isSidebarCollapsed,
  tenantName,
}) => {
  const { user } = useAuth();
  const { isAdmin, isTeacher, isStudent } = usePermission();

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

  return (
    <header className="sticky top-0 z-30 h-16 bg-surface/95 backdrop-blur-xs border-b border-border px-4 sm:px-6 flex items-center justify-between transition-colors">
      {/* Left: Mobile hamburger + Desktop toggle + Organization info */}
      <div className="flex items-center space-x-3">
        {/* Mobile menu trigger */}
        <button
          type="button"
          onClick={onToggleMobileNav}
          className="lg:hidden p-2 rounded-md text-text-muted hover:text-text hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary cursor-pointer"
          aria-label="Open mobile navigation"
        >
          <Menu className="w-5 h-5" />
        </button>

        {/* Desktop Sidebar Toggle in Top Left Corner */}
        {onToggleCollapse && (
          <button
            type="button"
            onClick={onToggleCollapse}
            className="hidden lg:flex p-2 rounded-md text-text-muted hover:text-text hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary cursor-pointer transition-colors"
            aria-label={isSidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
            title={isSidebarCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          >
            <PanelLeft className="w-4 h-4" />
          </button>
        )}

        <div className="flex items-center space-x-2">
          <span className="font-semibold text-sm text-text">
            {tenantName || "Academic Portal"}
          </span>
          <span className="hidden sm:inline-block w-1.5 h-1.5 rounded-full bg-border" />
          <span className="hidden sm:inline-block text-xs text-text-muted">
            Workspace
          </span>
        </div>
      </div>

      {/* Right: Theme switch, user role badge, user identifier */}
      <div className="flex items-center space-x-3 sm:space-x-4">
        {/* Light / Dark Mode Toggle */}
        <ThemeToggle />

        {/* User Role Badge */}
        {user && (
          <Badge variant={primaryRoleVariant} size="sm">
            {primaryRoleLabel}
          </Badge>
        )}

        {/* User Identity */}
        {user && (
          <div className="hidden md:flex items-center space-x-2 text-xs text-text-muted border-l border-border pl-3">
            <div className="w-7 h-7 rounded-full bg-surface-muted border border-border flex items-center justify-center text-text font-medium">
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
