/**
 * <Can> Component
 *
 * NOTE: Client-side permission checks are cosmetic only (rendering UI elements conditionally).
 * Real enforcement and data isolation are strictly performed server-side via FastAPI dependencies and RLS.
 */

import React from "react";
import { usePermission } from "@/hooks/usePermission";

export interface CanProps {
  /** A single permission string (e.g., "role:manage") */
  permission?: string;
  /** Multiple permissions */
  permissions?: string[];
  /** Single role name (e.g., "teacher") */
  role?: string;
  /** Multiple role names */
  roles?: string[];
  /** If multiple permissions/roles are passed, require all? Default false (any) */
  requireAll?: boolean;
  /** Fallback content rendered if permission/role check fails */
  fallback?: React.ReactNode;
  /** Content rendered if check passes */
  children: React.ReactNode;
}

export const Can: React.FC<CanProps> = ({
  permission,
  permissions,
  role,
  roles,
  requireAll = false,
  fallback = null,
  children,
}) => {
  const { can, canAny, canAll, hasRole, hasAnyRole } = usePermission();

  let hasAccess = true;

  // Single permission check
  if (permission && !can(permission)) {
    hasAccess = false;
  }

  // Multiple permissions check
  if (permissions && permissions.length > 0) {
    const passed = requireAll ? canAll(permissions) : canAny(permissions);
    if (!passed) {
      hasAccess = false;
    }
  }

  // Single role check
  if (role && !hasRole(role)) {
    hasAccess = false;
  }

  // Multiple roles check
  if (roles && roles.length > 0) {
    const passed = hasAnyRole(roles);
    if (!passed) {
      hasAccess = false;
    }
  }

  if (!hasAccess) {
    return <>{fallback}</>;
  }

  return <>{children}</>;
};

