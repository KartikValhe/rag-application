import React, { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { authApi } from "@/api/auth";
import { Button, Alert, Spinner, ThemeToggle } from "@/components/ui";

export const VerifyEmailPage: React.FC = () => {
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token");
  const [status, setStatus] = useState<"loading" | "success" | "error">("loading");
  const [message, setMessage] = useState<string>("");

  useEffect(() => {
    if (!token) {
      setStatus("error");
      setMessage("Verification link is missing or malformed.");
      return;
    }

    const verify = async () => {
      try {
        const res = await authApi.verifyEmail(token);
        setStatus("success");
        setMessage(res.message || "Your email address has been verified.");
      } catch (err: unknown) {
        setStatus("error");
        const axiosErr = err as { response?: { data?: { detail?: { message?: string } } } };
        setMessage(
          axiosErr.response?.data?.detail?.message ||
            "Verification link has expired or is invalid."
        );
      }
    };

    verify();
  }, [token]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-background px-4 py-12 relative">
      <div className="fixed top-4 right-4 z-50">
        <ThemeToggle />
      </div>
      <div className="w-full max-w-md space-y-6 bg-surface p-8 rounded-xl border border-border shadow-sm text-center">
        <h2 className="text-2xl font-bold tracking-tight text-text">Email Verification</h2>

        {status === "loading" && (
          <div className="py-8 flex flex-col items-center justify-center gap-3">
            <Spinner size="lg" />
            <p className="text-sm text-text-muted">Verifying your email token...</p>
          </div>
        )}

        {status === "success" && (
          <div className="space-y-4">
            <Alert variant="success" title="Verified!">
              {message}
            </Alert>
            <Link to="/login">
              <Button className="w-full">Proceed to Sign In</Button>
            </Link>
          </div>
        )}

        {status === "error" && (
          <div className="space-y-4">
            <Alert variant="danger" title="Verification Failed">
              {message}
            </Alert>
            <Link to="/login">
              <Button variant="outline" className="w-full">
                Back to Sign In
              </Button>
            </Link>
          </div>
        )}
      </div>
    </div>
  );
};

