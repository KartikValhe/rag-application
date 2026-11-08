/**
 * NOTE: Client-side permission checks are cosmetic only (UI gating / hiding elements).
 * Real enforcement is strictly executed server-side via FastAPI dependencies and PostgreSQL RLS.
 * Never rely on client-side checks for security boundaries.
 */

import { useMemo } from "react";
import { useAuth } from "@/features/auth/AuthContext";

export interface PermissionCheckOptions {
  requireAll?: boolean;
}

export function usePermission() {
  const { user } = useAuth();

  const permissionsSet = useMemo(() => {
    if (!user) return new Set<string>();
    if (user.isAdmin) {
      // Admins hold all system permissions
      return new Set<string>(["*"]);
    }
    return new Set<string>(user.permissions || []);
  }, [user]);

  const rolesSet = useMemo(() => {
    if (!user) return new Set<string>();
    const roles = new Set<string>(user.roles || []);
    if (user.isAdmin) {
      roles.add("tenant_admin");
    }
    return roles;
  }, [user]);

  /**
   * Check if the authenticated user has a specific permission.
   * Admin users automatically pass all checks.
   */
  const can = (permission: string): boolean => {
    if (!user) return false;
    if (user.isAdmin || permissionsSet.has("*")) return true;
    if (permissionsSet.has(permission)) return true;

    // Check wildcard prefix (e.g., "user:*" satisfies "user:manage")
    const [domain] = permission.split(":");
    if (domain && permissionsSet.has(`${domain}:*`)) {
      return true;
    }

    return false;
  };

  /**
   * Check if the user has any of the listed permissions.
   */
  const canAny = (permissions: string[]): boolean => {
    if (!permissions.length) return true;
    return permissions.some((p) => can(p));
  };

  /**
   * Check if the user has all of the listed permissions.
   */
  const canAll = (permissions: string[]): boolean => {
    if (!permissions.length) return true;
    return permissions.every((p) => can(p));
  };

  /**
   * Check if the user has a specific role.
   */
  const hasRole = (role: string): boolean => {
    if (!user) return false;
    if (user.isAdmin && role === "tenant_admin") return true;
    return rolesSet.has(role);
  };

  /**
   * Check if the user has any of the specified roles.
   */
  const hasAnyRole = (roles: string[]): boolean => {
    if (!user || !roles.length) return false;
    if (user.isAdmin && roles.includes("tenant_admin")) return true;
    return roles.some((r) => rolesSet.has(r));
  };

  return {
    user,
    isAdmin: Boolean(user?.isAdmin),
    isTeacher: hasRole("teacher"),
    isStudent: hasRole("student"),
    roles: Array.from(rolesSet),
    permissions: Array.from(permissionsSet),
    can,
    canAny,
    canAll,
    hasRole,
    hasAnyRole,
  };
}

