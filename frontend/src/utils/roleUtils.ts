export interface UserRoleLike {
  isAdmin?: boolean;
  roles?: string[];
}

export function isAdminUser(user: UserRoleLike | null | undefined): boolean {
  return Boolean(user?.isAdmin || user?.roles?.includes("tenant_admin"));
}

export function isTeacherUser(user: UserRoleLike | null | undefined): boolean {
  return Boolean(user?.roles?.includes("teacher"));
}

export function isTeacherOnlyUser(user: UserRoleLike | null | undefined): boolean {
  return isTeacherUser(user) && !isAdminUser(user);
}

export function getMaterialsBasePath(pathname: string): string {
  return pathname.startsWith("/admin") ? "/admin/materials" : "/teacher/materials";
}
