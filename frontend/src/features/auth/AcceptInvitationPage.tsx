import React, { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { adminApi } from "@/api/admin";
import { Button, Input, Alert, ThemeToggle } from "@/components/ui";

const acceptSchema = z
  .object({
    fullName: z.string().min(2, "Full name is required"),
    password: z.string().min(8, "Password must be at least 8 characters"),
    confirmPassword: z.string(),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: "Passwords do not match",
    path: ["confirmPassword"],
  });

type AcceptFormData = z.infer<typeof acceptSchema>;

export const AcceptInvitationPage: React.FC = () => {
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token") || "";
  const [isSuccess, setIsSuccess] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<AcceptFormData>({
    resolver: zodResolver(acceptSchema),
  });

  const onSubmit = async (data: AcceptFormData) => {
    if (!token) {
      setErrorMsg("Invitation token is missing or malformed.");
      return;
    }
    setErrorMsg(null);
    try {
      await adminApi.acceptInvitation({
        token,
        full_name: data.fullName,
        password: data.password,
      });
      setIsSuccess(true);
    } catch {
      setErrorMsg("Invitation link is invalid or has expired.");
    }
  };

  return (
      <div className="min-h-dvh flex items-center justify-center bg-background px-4 py-8 sm:py-12 relative">
        <div className="fixed top-4 right-4 z-50">
          <ThemeToggle />
        </div>
          <div className="w-full max-w-md space-y-6 bg-surface p-6 sm:p-8 rounded-xl border border-border shadow-sm">
            <div className="text-center space-y-2">
              <h2 className="text-2xl font-bold tracking-tight text-text">Accept Invitation</h2>
              <p className="text-sm text-text-muted">
                Set up your name and password to activate your account
              </p>
            </div>

            {errorMsg && (
              <Alert variant="danger" title="Error">
                {errorMsg}
              </Alert>
            )}

            {isSuccess ? (
              <div className="space-y-4">
                <Alert variant="success" title="Account Activated!">
                  Your account has been set up successfully. You can now log in.
                </Alert>
                <Link to="/login">
                  <Button className="w-full mt-2">Sign In</Button>
                </Link>
              </div>
            ) : (
              <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
                <Input
                  label="Full Name"
                  placeholder="Your name"
                  autoComplete="name"
                  error={errors.fullName?.message}
                  {...register("fullName")}
                />

                <Input
                  label="Choose Password"
                  type="password"
                  placeholder="••••••••"
                  autoComplete="new-password"
                  error={errors.password?.message}
                  {...register("password")}
                />

                <Input
                  label="Confirm Password"
                  type="password"
                  placeholder="••••••••"
                  autoComplete="new-password"
                  error={errors.confirmPassword?.message}
                  {...register("confirmPassword")}
                />

                <Button
                  type="submit"
                  className="w-full"
                  size="lg"
                  isLoading={isSubmitting}
                >
                  Activate Account
                </Button>
              </form>
            )}
          </div>
        </div>
        );
};
