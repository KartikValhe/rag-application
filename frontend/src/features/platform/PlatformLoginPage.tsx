import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import * as z from "zod";
import { platformApi } from "@/api/platform";
import { Button, Input, Alert, ThemeToggle } from "@/components/ui";

const platformLoginSchema = z.object({
  email: z.string().email("Please enter a valid superadmin email"),
  password: z.string().min(1, "Password is required"),
});

type PlatformLoginFormData = z.infer<typeof platformLoginSchema>;

export const PlatformLoginPage: React.FC = () => {
  const navigate = useNavigate();
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<PlatformLoginFormData>({
    resolver: zodResolver(platformLoginSchema),
    defaultValues: {
      email: "superadmin@platform.local",
      password: "",
    },
  });

  const onSubmit = async (data: PlatformLoginFormData) => {
    setErrorMsg(null);
    try {
      await platformApi.login(data.email, data.password);
      navigate("/platform");
    } catch (err: unknown) {
      const axiosErr = err as { response?: { data?: { detail?: { message?: string } | string } } };
      const detail = axiosErr.response?.data?.detail;
      const message =
        typeof detail === "object" && detail?.message
          ? detail.message
          : typeof detail === "string"
            ? detail
            : "Invalid superadmin credentials.";
      setErrorMsg(message);
    }
  };

  return (
      <div className="min-h-dvh flex items-center justify-center bg-background px-4 py-8 sm:py-12 relative">
        <div className="fixed top-4 right-4 z-50">
          <ThemeToggle />
        </div>
          <div className="w-full max-w-md space-y-6 bg-surface p-6 sm:p-8 rounded-xl border border-border shadow-sm">
            <div className="text-center space-y-2">
              <div className="inline-block px-2.5 py-1 text-xs font-bold tracking-widest text-primary bg-primary-light uppercase rounded-full">
                Platform Ops
              </div>
              <h2 className="text-2xl font-bold tracking-tight text-text">
                Superadmin Console
              </h2>
              <p className="text-sm text-text-muted">
                Global management & tenant provisioning
              </p>
            </div>

            {errorMsg && (
              <Alert variant="danger" title="Authentication Error">
                {errorMsg}
              </Alert>
            )}

            <form onSubmit={handleSubmit(onSubmit)} className="space-y-4">
              <Input
                label="Superadmin Email"
                type="email"
                placeholder="superadmin@platform.local"
                autoComplete="email"
                autoCapitalize="none"
                inputMode="email"
                error={errors.email?.message}
                {...register("email")}
              />

              <Input
                label="Password"
                type="password"
                placeholder="••••••••"
                autoComplete="current-password"
                error={errors.password?.message}
                {...register("password")}
              />

              <Button
                type="submit"
                className="w-full"
                size="lg"
                isLoading={isSubmitting}
              >
                Authenticate
              </Button>
            </form>
          </div>
        </div>
        );
};
