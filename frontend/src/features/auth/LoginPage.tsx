import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { useAuth } from "./AuthContext";
import { Button, Input, Alert, ThemeToggle } from "@/components/ui";
import { getTenantSlug } from "@/api/client";
import { useTheme } from "@/theme/ThemeProvider";
import { useBranding } from "@/features/branding";

const loginSchema = z.object({
  email: z.string().email("Please enter a valid email address"),
  password: z.string().min(8, "Password must be at least 8 characters"),
  tenantSlug: z.string().min(1, "Tenant slug is required"),
});

type LoginFormData = z.infer<typeof loginSchema>;

export const LoginPage: React.FC = () => {
  const { login } = useAuth();
  const { tenantSlug, setTenantSlug } = useTheme();
  const { branding } = useBranding();
  const navigate = useNavigate();
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [logoFailed, setLogoFailed] = useState<boolean>(false);

  const {
    register,
    handleSubmit,
    setValue,
    formState: { errors, isSubmitting },
  } = useForm<LoginFormData>({
    resolver: zodResolver(loginSchema),
    defaultValues: {
      tenantSlug: (tenantSlug && tenantSlug !== "default" ? tenantSlug : getTenantSlug()) || "acme",
      email: "",
      password: "",
    },
  });

  const onSubmit = async (data: LoginFormData) => {
    setErrorMsg(null);
    try {
      setTenantSlug(data.tenantSlug);
      const res = await login({
        email: data.email,
        password: data.password,
        tenant_slug: data.tenantSlug,
      });
      if (res.is_admin || res.roles?.includes("tenant_admin")) {
        navigate("/admin");
      } else if (res.roles?.includes("teacher")) {
        navigate("/teacher/dashboard");
      } else {
        navigate("/student/dashboard");
      }
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
            ? detail
            : "Invalid email, password, or tenant.";
      setErrorMsg(message);
    }
  };

  const fillQuickCredentials = (slug: string, email: string, pass: string) => {
    setValue("tenantSlug", slug, { shouldValidate: true });
    setValue("email", email, { shouldValidate: true });
    setValue("password", pass, { shouldValidate: true });
    setTenantSlug(slug);
    setLogoFailed(false);
  };

  return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-background px-4 py-12 relative">
        <div className="fixed top-4 right-4 z-50">
          <ThemeToggle />
        </div>
        <div className="w-full max-w-md space-y-6 bg-surface p-8 rounded-xl border border-border shadow-sm">
            <div className="text-center space-y-3">
              {branding.logo_url && !logoFailed ? (
                <img
                  src={branding.logo_url}
                  alt={branding.display_name}
                  className="h-12 max-w-[200px] object-contain mx-auto mb-2"
                  onError={() => setLogoFailed(true)}
                />
              ) : (
                <div className="w-12 h-12 rounded-xl bg-primary text-primary-foreground flex items-center justify-center font-bold text-xl mx-auto mb-2 shadow-sm">
                  {branding.display_name ? branding.display_name.charAt(0).toUpperCase() : "R"}
                </div>
              )}
              <h2 className="text-2xl font-bold tracking-tight text-text">
                {branding.display_name || "Sign In"}
              </h2>
              <p className="text-sm text-text-muted">
                {branding.tagline || "Access your organization's learning workspace"}
              </p>
            </div>

            {errorMsg && (
              <Alert variant="danger" title="Login Failed">
                {errorMsg}
              </Alert>
            )}

            <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
              <Input
                label="Organization ID (Slug)"
                placeholder="e.g. acme"
                error={errors.tenantSlug?.message}
                {...register("tenantSlug")}
              />

              <Input
                label="Email Address"
                type="email"
                placeholder="you@institution.edu"
                error={errors.email?.message}
                {...register("email")}
              />

              <div className="space-y-1">
                <Input
                  label="Password"
                  type="password"
                  placeholder="••••••••"
                  error={errors.password?.message}
                  {...register("password")}
                />
                <div className="flex justify-end pt-1">
                  <Link
                    to="/forgot-password"
                    className="text-xs text-primary hover:text-primary-hover font-medium"
                  >
                    Forgot password?
                  </Link>
                </div>
              </div>

              <Button
                type="submit"
                className="w-full"
                size="lg"
                isLoading={isSubmitting}
              >
                Sign In
              </Button>
            </form>

            <div className="text-center text-xs text-text-muted space-y-2">
              <div>
                Need an account?{" "}
                <Link
                  to="/register"
                  className="text-primary hover:text-primary-hover font-semibold"
                >
                  Register
                </Link>
              </div>

              {import.meta.env.DEV && (
                <div className="pt-3 border-t border-border text-left">
                  <p className="font-semibold text-text mb-1">Quick Demo Accounts:</p>
                  <div className="flex flex-wrap gap-2 text-xs">
                    <button
                      type="button"
                      onClick={() => fillQuickCredentials("acme", "admin@acme.edu", "Admin123!")}
                      className="px-2 py-1 bg-surface-muted hover:bg-surface-elevated rounded border border-border text-text"
                    >
                      Tenant Admin (acme)
                    </button>
                    <button
                      type="button"
                      onClick={() => fillQuickCredentials("acme", "teacher@acme.edu", "Teacher123!")}
                      className="px-2 py-1 bg-surface-muted hover:bg-surface-elevated rounded border border-border text-text"
                    >
                      Teacher (acme)
                    </button>
                    <button
                      type="button"
                      onClick={() => fillQuickCredentials("acme", "student@acme.edu", "Student123!")}
                      className="px-2 py-1 bg-surface-muted hover:bg-surface-elevated rounded border border-border text-text"
                    >
                      Student (acme)
                    </button>
                  </div>
                  <div className="mt-2 text-right">
                    <Link to="/platform/login" className="text-xs text-primary hover:underline">
                      Platform Superadmin Login &rarr;
                    </Link>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Tenant-branded Footer Text */}
          <footer className="mt-6 text-center text-xs text-text-subtle">
            {branding.footer_text || "© 2026 RAG Platform. All rights reserved."}
          </footer>
        </div>
        );
};

