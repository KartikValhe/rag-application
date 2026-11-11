import React from "react";
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "@/theme/ThemeProvider";
import { BrandingProvider, BrandingEditorPage } from "@/features/branding";
import { ToastProvider } from "@/components/ui/Toast";
import { AuthProvider } from "@/features/auth/AuthContext";
import {
  ProtectedRoute,
  AdminRoute,
  TeacherRoute,
  StudentRoute,
  PublicRoute,
  PlatformRoute,
} from "@/features/auth/guards";

import { LoginPage } from "@/features/auth/LoginPage";
import { RegisterPage } from "@/features/auth/RegisterPage";
import { ForgotPasswordPage } from "@/features/auth/ForgotPasswordPage";
import { ResetPasswordPage } from "@/features/auth/ResetPasswordPage";
import { VerifyEmailPage } from "@/features/auth/VerifyEmailPage";
import { AcceptInvitationPage } from "@/features/auth/AcceptInvitationPage";

import { PlatformLoginPage } from "@/features/platform/PlatformLoginPage";
import { PlatformDashboardPage } from "@/features/platform/PlatformDashboardPage";
import { AdminDashboardPage } from "@/features/admin/AdminDashboardPage";
import { RolesPage } from "@/features/roles/RolesPage";
import { AcademicPage } from "@/features/academic/AcademicPage";
import { EnrollmentsPage } from "@/features/enrollments/EnrollmentsPage";
import { AssignmentsPage } from "@/features/assignments/AssignmentsPage";
import { AuditLogPage } from "@/features/audit/AuditLogPage";
import { DashboardPage } from "@/features/dashboard/DashboardPage";
import { TeacherClassesPage } from "@/features/teacher/TeacherClassesPage";
import { TeacherClassDetailPage } from "@/features/teacher/TeacherClassDetailPage";
import { StudentClassesPage } from "@/features/student/StudentClassesPage";
import { StudentClassDetailPage } from "@/features/student/StudentClassDetailPage";
import { TimetableEditorPage, ScheduleViewPage } from "@/features/timetable";
import { ErrorBoundary } from "@/components/layout";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

export const App: React.FC = () => {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <BrandingProvider>
          <ToastProvider>
            <AuthProvider>
              <BrowserRouter>
                <ErrorBoundary>
                  <Routes>
                  {/* Public Auth Routes */}
                  <Route
                    path="/login"
                    element={
                      <PublicRoute>
                        <LoginPage />
                      </PublicRoute>
                    }
                  />
                  <Route
                    path="/register"
                    element={
                      <PublicRoute>
                        <RegisterPage />
                      </PublicRoute>
                    }
                  />
                  <Route
                    path="/forgot-password"
                    element={
                      <PublicRoute>
                        <ForgotPasswordPage />
                      </PublicRoute>
                    }
                  />
                  <Route
                    path="/reset-password"
                    element={
                      <PublicRoute>
                        <ResetPasswordPage />
                      </PublicRoute>
                    }
                  />
                  <Route path="/verify-email" element={<VerifyEmailPage />} />
                  <Route path="/accept-invitation" element={<AcceptInvitationPage />} />

                  {/* Superadmin Platform Routes */}
                  <Route path="/platform/login" element={<PlatformLoginPage />} />
                  <Route
                    path="/platform"
                    element={
                      <PlatformRoute>
                        <PlatformDashboardPage />
                      </PlatformRoute>
                    }
                  />

                  {/* Tenant Admin Routes */}
                  <Route
                    path="/admin"
                    element={
                      <AdminRoute>
                        <AdminDashboardPage />
                      </AdminRoute>
                    }
                  />
                  <Route
                    path="/admin/roles"
                    element={
                      <AdminRoute>
                        <RolesPage />
                      </AdminRoute>
                    }
                  />
                  <Route
                    path="/admin/academic"
                    element={
                      <AdminRoute>
                        <AcademicPage />
                      </AdminRoute>
                    }
                  />
                  <Route
                    path="/admin/enrollments"
                    element={
                      <AdminRoute>
                        <EnrollmentsPage />
                      </AdminRoute>
                    }
                  />
                  <Route
                    path="/admin/assignments"
                    element={
                      <AdminRoute>
                        <AssignmentsPage />
                      </AdminRoute>
                    }
                  />
                  <Route
                    path="/admin/users"
                    element={
                      <AdminRoute>
                        <AdminDashboardPage />
                      </AdminRoute>
                    }
                  />
                  <Route
                    path="/admin/audit"
                    element={
                      <AdminRoute>
                        <AuditLogPage />
                      </AdminRoute>
                    }
                  />
                  <Route
                    path="/admin/branding"
                    element={
                      <AdminRoute>
                        <BrandingEditorPage />
                      </AdminRoute>
                    }
                  />
                  <Route
                    path="/admin/timetables"
                    element={
                      <AdminRoute>
                        <TimetableEditorPage />
                      </AdminRoute>
                    }
                  />
                  <Route
                    path="/admin/schedule"
                    element={
                      <AdminRoute>
                        <ScheduleViewPage />
                      </AdminRoute>
                    }
                  />

                  {/* Member & Portal Dashboards */}
                  <Route
                    path="/dashboard"
                    element={
                      <ProtectedRoute>
                        <DashboardPage />
                      </ProtectedRoute>
                    }
                  />

                  {/* Teacher Portal Routes */}
                  <Route
                    path="/teacher/dashboard"
                    element={
                      <TeacherRoute>
                        <DashboardPage />
                      </TeacherRoute>
                    }
                  />
                  <Route
                    path="/teacher/classes"
                    element={
                      <TeacherRoute>
                        <TeacherClassesPage />
                      </TeacherRoute>
                    }
                  />
                  <Route
                    path="/teacher/classes/:classId"
                    element={
                      <TeacherRoute>
                        <TeacherClassDetailPage />
                      </TeacherRoute>
                    }
                  />
                  <Route
                    path="/teacher/schedule"
                    element={
                      <TeacherRoute>
                        <ScheduleViewPage />
                      </TeacherRoute>
                    }
                  />
                  <Route
                    path="/teacher/timetables"
                    element={
                      <TeacherRoute>
                        <TimetableEditorPage />
                      </TeacherRoute>
                    }
                  />

                  {/* Student Portal Routes */}
                  <Route
                    path="/student/dashboard"
                    element={
                      <StudentRoute>
                        <DashboardPage />
                      </StudentRoute>
                    }
                  />
                  <Route
                    path="/student/classes"
                    element={
                      <StudentRoute>
                        <StudentClassesPage />
                      </StudentRoute>
                    }
                  />
                  <Route
                    path="/student/classes/:classId"
                    element={
                      <StudentRoute>
                        <StudentClassDetailPage />
                      </StudentRoute>
                    }
                  />
                  <Route
                    path="/student/schedule"
                    element={
                      <StudentRoute>
                        <ScheduleViewPage />
                      </StudentRoute>
                    }
                  />

                  {/* Default redirect */}
                  <Route path="/" element={<Navigate to="/login" replace />} />
                  <Route path="*" element={<Navigate to="/login" replace />} />
                </Routes>
              </ErrorBoundary>
            </BrowserRouter>
            </AuthProvider>
          </ToastProvider>
        </BrandingProvider>
      </ThemeProvider>
    </QueryClientProvider >
  );
};

