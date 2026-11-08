import React from "react";
import { Link } from "react-router-dom";
import { ShieldAlert, ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/Button";

export interface ForbiddenStateProps {
  title?: string;
  description?: string;
  backPath?: string;
  backLabel?: string;
}

export const ForbiddenState: React.FC<ForbiddenStateProps> = ({
  title = "Access Denied (403)",
  description = "You do not have the necessary permissions or role assignments to view this page or perform this action.",
  backPath = "/dashboard",
  backLabel = "Return to Dashboard",
}) => {
  return (
    <div className="min-h-[50vh] flex flex-col items-center justify-center text-center p-6 bg-surface rounded-xl border border-border shadow-xs my-8 max-w-lg mx-auto">
      <div className="w-14 h-14 rounded-full bg-danger/10 text-danger flex items-center justify-center mb-4">
        <ShieldAlert className="w-7 h-7" />
      </div>
      <h2 className="text-xl font-bold text-text mb-2">{title}</h2>
      <p className="text-sm text-text-muted mb-6 max-w-sm">{description}</p>
      <Link to={backPath}>
        <Button variant="outline" leftIcon={<ArrowLeft className="w-4 h-4" />}>
          {backLabel}
        </Button>
      </Link>
    </div>
  );
};

