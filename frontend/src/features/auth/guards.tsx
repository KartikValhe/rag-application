import React from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useAuth } from "./AuthContext";
import { getPlatformToken } from "@/api/client";
import { Spinner } from "@/components/ui";
import { AppShell } from "@/components/layout/AppShell";
import { ForbiddenState } from "@/components/layout/ForbiddenState";

export const ProtectedRoute: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const { isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div className="flex h-screen w-full items-center justify-center bg-background">
        <Spinner size="lg" />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  return <>{children}</>;
};

export const AdminRoute: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const { user, isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div className="flex h-screen w-full items-center justify-center bg-background">
        <Spinner size="lg" />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  const isAdmin = user?.isAdmin || user?.roles?.includes("tenant_admin");

  if (!isAdmin) {
    const isTeacher = user?.roles?.includes("teacher");
    return (
      <AppShell>
        <ForbiddenState
          title="Administrative Access Required (403)"
          description="You do not have administrative permissions to view or manage this academic section."
          backPath={isTeacher ? "/teacher/dashboard" : "/student/dashboard"}
          backLabel={isTeacher ? "Return to Teacher Portal" : "Return to Student Portal"}
        />
      </AppShell>
    );
  }

  return <>{children}</>;
};

export const TeacherRoute: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const { user, isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div className="flex h-screen w-full items-center justify-center bg-background">
        <Spinner size="lg" />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  const isTeacherOrAdmin =
    user?.isAdmin ||
    user?.roles?.includes("tenant_admin") ||
    user?.roles?.includes("teacher");

  if (!isTeacherOrAdmin) {
    return (
      <AppShell>
        <ForbiddenState
          title="Faculty Access Required (403)"
          description="You do not have permission to access the teaching faculty portal."
          backPath="/student/dashboard"
          backLabel="Return to Student Portal"
        />
      </AppShell>
    );
  }

  return <>{children}</>;
};

export const StudentRoute: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const { user, isAuthenticated, isLoading } = useAuth();
  const location = useLocation();

  if (isLoading) {
    return (
      <div className="flex h-screen w-full items-center justify-center bg-background">
        <Spinner size="lg" />
      </div>
    );
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  const isStudentOrAdmin =
    user?.isAdmin ||
    user?.roles?.includes("tenant_admin") ||
    user?.roles?.includes("student");

  if (!isStudentOrAdmin) {
    return (
      <AppShell>
        <ForbiddenState
          title="Student Access Required (403)"
          description="You do not have permission to access the student learning portal."
          backPath="/teacher/dashboard"
          backLabel="Return to Teacher Portal"
        />
      </AppShell>
    );
  }

  return <>{children}</>;
};

export const PublicRoute: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const { isAuthenticated, user, isLoading } = useAuth();

  if (isLoading) {
    return (
      <div className="flex h-screen w-full items-center justify-center bg-background">
        <Spinner size="lg" />
      </div>
    );
  }

  if (isAuthenticated) {
    if (user?.isAdmin || user?.roles?.includes("tenant_admin")) {
      return <Navigate to="/admin" replace />;
    }
    if (user?.roles?.includes("teacher")) {
      return <Navigate to="/teacher/dashboard" replace />;
    }
    return <Navigate to="/student/dashboard" replace />;
  }

  return <>{children}</>;
};

export const PlatformRoute: React.FC<{ children: React.ReactNode }> = ({
  children,
}) => {
  const token = getPlatformToken();
  const location = useLocation();

  if (!token) {
    return <Navigate to="/platform/login" state={{ from: location }} replace />;
  }

  return <>{children}</>;
};

