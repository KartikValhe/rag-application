import React from "react";
import { NavLink, useLocation } from "react-router-dom";
import {
  LayoutDashboard,
  Shield,
  BookOpen,
  GraduationCap,
  Users,
  UserCheck,
  FileText,
  X,
  School,
  PanelLeftClose,
  PanelLeftOpen,
  LogOut,
  Palette,
  User as UserIcon,
} from "lucide-react";
import { cn } from "@/utils/cn";
import { useAuth } from "@/features/auth/AuthContext";
import { usePermission } from "@/hooks/usePermission";
import { Badge } from "@/components/ui/Badge";
import { Can } from "@/components/auth/Can";
import { useBranding } from "@/features/branding";
import { useScrollLock } from "@/hooks/useScrollLock";
import { useFocusTrap } from "@/hooks/useFocusTrap";

export interface SidebarProps {
  isCollapsed: boolean;
  onToggleCollapse: () => void;
  isMobileOpen: boolean;
  onCloseMobile: () => void;
  hamburgerRef?: React.RefObject<HTMLButtonElement>;
}

interface NavItemConfig {
  label: string;
  href: string;
  icon: React.ElementType;
  permission?: string;
  badge?: string;
}

interface NavSectionConfig {
  title: string;
  items: NavItemConfig[];
  roles?: string[];
  permission?: string;
  adminOnly?: boolean;
}

export const Sidebar: React.FC<SidebarProps> = ({
  isCollapsed,
  onToggleCollapse,
  isMobileOpen,
  onCloseMobile,
  hamburgerRef,
}) => {
  const location = useLocation();
  const { user, logout } = useAuth();
  const { isAdmin, isTeacher, isStudent, can } = usePermission();
  const { branding } = useBranding();
  const [logoFailed, setLogoFailed] = React.useState(false);
  const drawerRef = React.useRef<HTMLElement | null>(null);

  // Lock body scroll on mobile when drawer is open
  useScrollLock(isMobileOpen);

  // Trap focus and handle Escape key to close drawer, returning focus to hamburger
  useFocusTrap(drawerRef, isMobileOpen, {
    onClose: onCloseMobile,
    returnFocusRef: hamburgerRef,
  });

  // Automatically close drawer on route change
  React.useEffect(() => {
    if (isMobileOpen) {
      onCloseMobile();
    }
  }, [location.pathname]);

  const navSections: NavSectionConfig[] = [
    {
      title: "Administration",
      adminOnly: true,
      items: [
        {
          label: "Admin Overview",
          href: "/admin",
          icon: LayoutDashboard,
        },
        {
          label: "Roles & Access",
          href: "/admin/roles",
          icon: Shield,
          permission: "role:manage",
        },
        {
          label: "Classes & Subjects",
          href: "/admin/academic",
          icon: School,
          permission: "class:manage",
        },
        {
          label: "Enrollments",
          href: "/admin/enrollments",
          icon: GraduationCap,
          permission: "enrollment:manage",
        },
        {
          label: "Teacher Assignments",
          href: "/admin/assignments",
          icon: UserCheck,
          permission: "assignment:manage",
        },
        {
          label: "Users & Invites",
          href: "/admin/users",
          icon: Users,
          permission: "user:manage",
        },
        {
          label: "Audit Logs",
          href: "/admin/audit",
          icon: FileText,
          permission: "audit:view",
        },
        {
          label: "Branding & Theme",
          href: "/admin/branding",
          icon: Palette,
          permission: "branding:manage",
        },
      ],
    },
    {
      title: "Teacher Portal",
      roles: ["teacher"],
      items: [
        {
          label: "Teaching Workspace",
          href: "/teacher/dashboard",
          icon: LayoutDashboard,
        },
        {
          label: "My Classes & Subjects",
          href: "/teacher/classes",
          icon: BookOpen,
        },
      ],
    },
    {
      title: "Student Portal",
      roles: ["student"],
      items: [
        {
          label: "Student Workspace",
          href: "/student/dashboard",
          icon: LayoutDashboard,
        },
        {
          label: "My Enrolled Classes",
          href: "/student/classes",
          icon: BookOpen,
        },
      ],
    },
  ];

  // Filter sections and items based on role / permissions
  const visibleSections = navSections
    .filter((sec) => {
      if (sec.adminOnly && !isAdmin) return false;
      if (sec.permission && !can(sec.permission)) return false;
      if (sec.roles) {
        const matchesRole = sec.roles.some((r) => {
          if (r === "teacher") return isTeacher;
          if (r === "student") return isStudent;
          if (r === "tenant_admin") return isAdmin;
          return user?.roles?.includes(r);
        });
        if (!matchesRole) return false;
      }
      return true;
    })
    .map((sec) => ({
      ...sec,
      items: sec.items.filter((item) => {
        if (item.permission && !can(item.permission)) return false;
        return true;
      }),
    }))
    .filter((sec) => sec.items.length > 0);

  const sidebarContent = (
    <div className="flex flex-col h-full bg-surface border-r border-border">
      {/* Brand & Collapse Header */}
      <div className="flex items-center justify-between h-16 px-4 border-b border-border select-none">
        <div className="flex items-center space-x-3 overflow-hidden">
          {branding.logo_url && !logoFailed ? (
            <img
              src={branding.logo_url}
              alt={branding.display_name}
              className="w-9 h-9 object-contain rounded-lg shrink-0"
              onError={() => setLogoFailed(true)}
            />
          ) : (
            <div className="w-9 h-9 rounded-lg bg-primary text-primary-foreground flex items-center justify-center font-bold text-base shrink-0 shadow-sm">
              {branding.display_name ? branding.display_name.charAt(0).toUpperCase() : "R"}
            </div>
          )}
          {!isCollapsed && (
            <div className="flex flex-col min-w-0">
              <span className="font-bold text-sm tracking-tight text-text truncate">
                {branding.display_name || "Academic Portal"}
              </span>
                <span className="text-[10px] uppercase font-semibold text-text-muted tracking-wider truncate">
                  {branding.tagline || "Workspace"}
                </span>
            </div>
          )}
        </div>

        {/* Desktop Collapse Toggle at Top Left */}
        <button
          type="button"
          onClick={onToggleCollapse}
          className="hidden lg:flex p-1.5 rounded-md text-text-muted hover:text-text hover:bg-surface-hover transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          aria-label={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {isCollapsed ? (
            <PanelLeftOpen className="w-4 h-4" />
          ) : (
            <PanelLeftClose className="w-4 h-4" />
          )}
        </button>

        {/* Mobile close button: 44px min touch target */}
        <button
          type="button"
          onClick={onCloseMobile}
          className="lg:hidden min-w-[44px] min-h-[44px] flex items-center justify-center rounded-md text-text-muted hover:text-text hover:bg-surface-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary cursor-pointer shrink-0"
          aria-label="Close navigation"
        >
          <X className="w-5 h-5" />
        </button>
      </div>

      {/* Navigation Links */}
      <nav className="flex-1 overflow-y-auto scrollbar-none px-3 py-4 space-y-6" aria-label="Sidebar Navigation">
        {visibleSections.map((sec, sIdx) => (
          <div key={`sec-${sIdx}`} className="space-y-1">
            {!isCollapsed && (
              <div className="px-3 text-[11px] font-semibold text-text-muted uppercase tracking-wider mb-2">
                {sec.title}
              </div>
            )}
            <ul className="space-y-1">
              {sec.items.map((item) => {
                const Icon = item.icon;
                const isActive = location.pathname === item.href;

                return (
                  <Can key={item.href} permission={item.permission}>
                    <li>
                      <NavLink
                        to={item.href}
                        onClick={onCloseMobile}
                        className={cn(
                          "flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded-md text-sm font-medium transition-all group relative",
                          isActive
                            ? "bg-primary/10 text-primary border-l-2 border-primary font-semibold"
                            : "text-text-muted hover:text-text hover:bg-surface-hover"
                        )}
                        title={isCollapsed ? item.label : undefined}
                      >
                        <Icon
                          className={cn(
                            "w-5 h-5 shrink-0 transition-colors",
                            isActive ? "text-primary" : "text-text-muted group-hover:text-text"
                          )}
                          aria-hidden="true"
                        />
                        {!isCollapsed && <span className="truncate flex-1">{item.label}</span>}
                        {!isCollapsed && item.badge && (
                          <Badge variant="primary" size="sm">
                            {item.badge}
                          </Badge>
                        )}
                      </NavLink>
                    </li>
                  </Can>
                );
              })}
            </ul>
          </div>
        ))}
      </nav>

      {/* Left Bottom Corner: User Identity & Logout */}
      <div className="p-3 border-t border-border bg-surface-muted/30">
        <div className={cn("flex items-center", isCollapsed ? "justify-center" : "justify-between gap-2")}>
          {!isCollapsed && (
            <div className="flex items-center space-x-2.5 min-w-0 flex-1">
              <div className="w-8 h-8 rounded-full bg-surface border border-border flex items-center justify-center text-text font-medium shrink-0">
                <UserIcon className="w-4 h-4 text-text-muted" />
              </div>
              <div className="min-w-0 flex-1">
                <p className="text-xs font-semibold text-text truncate">
                  {user?.email || "User"}
                </p>
                <p className="text-[10px] text-text-muted capitalize">
                  {isAdmin ? "Admin" : isTeacher ? "Teacher" : isStudent ? "Student" : "Member"}
                </p>
              </div>
            </div>
          )}

          <button
            type="button"
            onClick={() => logout()}
            className={cn(
              "min-w-[44px] min-h-[44px] flex items-center justify-center rounded-md text-text-muted hover:text-danger hover:bg-danger/10 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger cursor-pointer",
              isCollapsed ? "w-full" : ""
            )}
            title="Log Out"
            aria-label="Log Out"
          >
            <LogOut className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );

  return (
    <>
      {/* Mobile Drawer Overlay */}
      {isMobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 backdrop-blur-xs lg:hidden transition-opacity"
          onClick={onCloseMobile}
          aria-hidden="true"
        />
      )}

      {/* Mobile Drawer */}
      <aside
        ref={drawerRef}
        className={cn(
          "fixed inset-y-0 left-0 z-50 w-72 max-w-[85vw] lg:hidden transform transition-transform duration-200 ease-in-out shadow-2xl",
          isMobileOpen ? "translate-x-0" : "-translate-x-full"
        )}
        aria-label="Mobile Navigation"
      >
        {sidebarContent}
      </aside>

      {/* Desktop Static Sidebar */}
      <aside
        className={cn(
          "hidden lg:block shrink-0 transition-[width] duration-200 ease-in-out",
          isCollapsed ? "w-18" : "w-64"
        )}
        aria-label="Desktop Navigation"
      >
        {sidebarContent}
      </aside>
    </>
  );
};
