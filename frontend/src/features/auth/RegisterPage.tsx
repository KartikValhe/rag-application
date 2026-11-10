import React, { useState } from "react";
import { Link } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { useAuth } from "./AuthContext";
import { Button, Input, Alert, ThemeToggle } from "@/components/ui";
import { getTenantSlug } from "@/api/client";
import { useTheme } from "@/theme/ThemeProvider";

const registerSchema = z
  .object({
    tenantSlug: z.string().min(1, "Organization ID is required"),
    fullName: z.string().min(2, "Full name must be at least 2 characters"),
    email: z.string().email("Please enter a valid email address"),
    password: z.string().min(8, "Password must be at least 8 characters"),
    confirmPassword: z.string(),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: "Passwords do not match",
    path: ["confirmPassword"],
  });

type RegisterFormData = z.infer<typeof registerSchema>;

export const RegisterPage: React.FC = () => {
  const { register: registerAuth } = useAuth();
  const { tenantSlug, setTenantSlug } = useTheme();
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [successInfo, setSuccessInfo] = useState<{
    status: string;
    message: string;
  } | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<RegisterFormData>({
    resolver: zodResolver(registerSchema),
    defaultValues: {
      tenantSlug: tenantSlug || getTenantSlug() || "",
      fullName: "",
      email: "",
      password: "",
      confirmPassword: "",
    },
  });

  const onSubmit = async (data: RegisterFormData) => {
    setErrorMsg(null);
    setSuccessInfo(null);
    try {
      setTenantSlug(data.tenantSlug);
      const res = await registerAuth({
        email: data.email,
        full_name: data.fullName,
        password: data.password,
        tenant_slug: data.tenantSlug,
      });
      setSuccessInfo(res);
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
            ? detail
            : "Registration failed. Organization may be invite-only or email is in use.";
      setErrorMsg(message);
    }
  };

  return (
        <div className="min-h-dvh flex flex-col items-center justify-center bg-background px-4 py-8 sm:py-12 relative w-full">
          <div className="absolute top-4 right-4 z-20">
            <ThemeToggle />
          </div>

            <div className="w-full max-w-md space-y-6 bg-surface p-6 sm:p-8 rounded-xl border border-border shadow-sm">
              <div className="text-center space-y-2">
                <h2 className="text-2xl font-bold tracking-tight text-text">Create Account</h2>
                <p className="text-sm text-text-muted">
                  Join your organization's learning workspace
                </p>
              </div>

              {errorMsg && (
                <Alert variant="danger" title="Registration Error">
                  {errorMsg}
                </Alert>
              )}

              {successInfo ? (
                <div className="space-y-4">
                  <Alert
                    variant={successInfo.status === "pending" ? "warning" : "success"}
                    title={
                      successInfo.status === "pending"
                        ? "Registration Submitted"
                        : "Account Created"
                    }
                  >
                    {successInfo.message}
                  </Alert>
                  <div className="pt-2">
                      <Link to="/login" className="block w-full">
                        <Button variant="outline" className="w-full">
                          Return to Sign In
                        </Button>
                      </Link>
                  </div>
                </div>
              ) : (
                <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
                  <Input
                    label="Organization ID (Slug)"
                    placeholder="e.g. acme-college"
                    autoComplete="organization"
                    autoCapitalize="none"
                    autoCorrect="off"
                    spellCheck="false"
                    error={errors.tenantSlug?.message}
                    {...register("tenantSlug")}
                  />

                  <Input
                    label="Full Name"
                    placeholder="Jane Doe"
                    autoComplete="name"
                    autoCapitalize="words"
                    error={errors.fullName?.message}
                    {...register("fullName")}
                  />

                  <Input
                    label="Email Address"
                    type="email"
                    inputMode="email"
                    autoComplete="email"
                    autoCapitalize="none"
                    autoCorrect="off"
                    placeholder="jane@institution.edu"
                    error={errors.email?.message}
                    {...register("email")}
                  />

                  <Input
                    label="Password"
                    type="password"
                    autoComplete="new-password"
                    placeholder="••••••••"
                    error={errors.password?.message}
                    {...register("password")}
                  />

                  <Input
                    label="Confirm Password"
                    type="password"
                    autoComplete="new-password"
                    placeholder="••••••••"
                    error={errors.confirmPassword?.message}
                    {...register("confirmPassword")}
                  />

                  <Button
                    type="submit"
                    className="w-full"
                    size="lg"
                    isLoading={isSubmitting}
                  >
                    Register
                  </Button>
                </form>
              )}

                <div className="text-center text-xs text-text-muted min-h-[44px] flex items-center justify-center">
                  Already have an account?{" "}
                  <Link
                    to="/login"
                    className="text-primary hover:text-primary-hover font-semibold ml-1 inline-flex items-center"
                  >
                    Sign In
                  </Link>
                </div>
              </div>
            </div>
            );
};
