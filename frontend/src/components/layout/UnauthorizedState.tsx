import React from "react";
import { Link } from "react-router-dom";
import { Lock, LogIn } from "lucide-react";
import { Button } from "@/components/ui/Button";

export interface UnauthorizedStateProps {
  title?: string;
  description?: string;
  loginPath?: string;
}

export const UnauthorizedState: React.FC<UnauthorizedStateProps> = ({
  title = "Authentication Required",
  description = "Your session has expired or you need to sign in to access this resource.",
  loginPath = "/login",
}) => {
  return (
    <div className="min-h-[50vh] flex flex-col items-center justify-center text-center p-6 bg-surface rounded-xl border border-border shadow-xs my-8 max-w-lg mx-auto">
      <div className="w-14 h-14 rounded-full bg-warning/10 text-warning flex items-center justify-center mb-4">
        <Lock className="w-7 h-7" />
      </div>
      <h2 className="text-xl font-bold text-text mb-2">{title}</h2>
      <p className="text-sm text-text-muted mb-6 max-w-sm">{description}</p>
      <Link to={loginPath}>
        <Button variant="primary" leftIcon={<LogIn className="w-4 h-4" />}>
          Sign In
        </Button>
      </Link>
    </div>
  );
};

