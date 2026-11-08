import React from "react";
import { useAuth } from "@/features/auth/AuthContext";
import { useTheme } from "@/theme/ThemeProvider";
import { Button, Badge } from "@/components/ui";
import { GraduationCap, LogOut, ShieldAlert } from "lucide-react";

export const DashboardPage: React.FC = () => {
  const { user, logout } = useAuth();
  const { tenantSlug } = useTheme();

  return (
    <div className="min-h-screen bg-background">
      <header className="border-b border-border bg-surface px-6 py-4 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-primary-light rounded-lg text-primary">
            <GraduationCap className="h-5 w-5" />
          </div>
          <div>
            <h1 className="text-lg font-bold text-text capitalize">
              {tenantSlug} Workspace
            </h1>
            <p className="text-xs text-text-muted">Student & Faculty Portal</p>
          </div>
        </div>

        <Button variant="ghost" size="sm" onClick={() => logout()}>
          <LogOut className="h-4 w-4 mr-1.5" />
          <span>Sign Out</span>
        </Button>
      </header>

      <main className="max-w-4xl mx-auto p-8 space-y-6">
        <div className="bg-surface p-6 rounded-xl border border-border shadow-sm space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h2 className="text-xl font-bold text-text">Welcome to Your Workspace</h2>
              <p className="text-sm text-text-muted mt-0.5">
                Phase 1 (Tenancy Core and Identity) is active.
              </p>
            </div>
            <Badge variant="success">Active Member</Badge>
          </div>

          <div className="p-4 bg-surface-muted rounded-lg border border-border text-sm space-y-2">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-text-muted">User ID:</span>
              <code className="text-xs bg-surface px-2 py-1 rounded border border-border">
                {user?.id}
              </code>
            </div>
            <div className="flex items-center gap-2">
              <span className="font-semibold text-text-muted">Tenant Realm:</span>
              <span className="capitalize font-medium text-text">{tenantSlug}</span>
            </div>
          </div>

          <div className="flex items-start gap-3 p-4 bg-primary-light/30 border border-primary/20 rounded-lg text-sm text-text">
            <ShieldAlert className="h-5 w-5 text-primary shrink-0 mt-0.5" />
            <div>
              <h4 className="font-semibold text-primary">Academic Structure Pending</h4>
              <p className="text-xs text-text-muted mt-1 leading-relaxed">
                Classroom enrollments, syllabus materials, and scoped AI chat will be enabled in
                Phase 2 & Phase 6. Your identity and tenant isolation are securely established.
              </p>
            </div>
          </div>
        </div>
      </main>
    </div>
  );
};

