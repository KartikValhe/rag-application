import React, { useState } from "react";
import { Link } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { authApi } from "@/api/auth";
import { Button, Input, Alert, ThemeToggle } from "@/components/ui";
import { getTenantSlug } from "@/api/client";

const forgotSchema = z.object({
  email: z.string().email("Please enter a valid email address"),
  tenantSlug: z.string().min(1, "Organization ID is required"),
});

type ForgotFormData = z.infer<typeof forgotSchema>;

export const ForgotPasswordPage: React.FC = () => {
  const [isSuccess, setIsSuccess] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<ForgotFormData>({
    resolver: zodResolver(forgotSchema),
    defaultValues: {
      tenantSlug: getTenantSlug() || "",
      email: "",
    },
  });

  const onSubmit = async (data: ForgotFormData) => {
    setErrorMsg(null);
    try {
      await authApi.forgotPassword(data.email, data.tenantSlug);
      setIsSuccess(true);
    } catch {
      setErrorMsg("Failed to request password reset. Please verify your details.");
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4 py-12 relative">
      <div className="fixed top-4 right-4 z-50">
        <ThemeToggle />
      </div>
      <div className="w-full max-w-md space-y-6 bg-surface p-8 rounded-xl border border-border shadow-sm">
        <div className="text-center space-y-2">
          <h2 className="text-2xl font-bold tracking-tight text-text">Reset Password</h2>
          <p className="text-sm text-text-muted">
            Enter your email to receive a password reset link
          </p>
        </div>

        {errorMsg && (
          <Alert variant="danger" title="Error">
            {errorMsg}
          </Alert>
        )}

        {isSuccess ? (
          <div className="space-y-4">
            <Alert variant="success" title="Link Sent">
              If an account with that email exists, we have sent instructions to reset your password.
            </Alert>
            <Link to="/login">
              <Button variant="outline" className="w-full mt-2">
                Return to Sign In
              </Button>
            </Link>
          </div>
        ) : (
          <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
            <Input
              label="Organization ID (Slug)"
              placeholder="e.g. acme-college"
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

            <Button
              type="submit"
              className="w-full"
              size="lg"
              isLoading={isSubmitting}
            >
              Send Reset Link
            </Button>
          </form>
        )}

        <div className="text-center text-xs text-text-muted">
          Remember your password?{" "}
          <Link
            to="/login"
            className="text-primary hover:text-primary-hover font-semibold"
          >
            Sign In
          </Link>
        </div>
      </div>
    </div>
  );
};

