import React from "react";
import { Link } from "react-router-dom";
import { FileQuestion, Home } from "lucide-react";
import { Button } from "@/components/ui/Button";

export interface NotFoundStateProps {
  title?: string;
  description?: string;
  homePath?: string;
  homeLabel?: string;
}

export const NotFoundState: React.FC<NotFoundStateProps> = ({
  title = "Page Not Found (404)",
  description = "The page or academic resource you are looking for does not exist or has been moved.",
  homePath = "/dashboard",
  homeLabel = "Back to Home",
}) => {
  return (
    <div className="min-h-[50vh] flex flex-col items-center justify-center text-center p-6 bg-surface rounded-xl border border-border shadow-xs my-8 max-w-lg mx-auto">
      <div className="w-14 h-14 rounded-full bg-surface-muted text-text-muted flex items-center justify-center mb-4">
        <FileQuestion className="w-7 h-7" />
      </div>
      <h2 className="text-xl font-bold text-text mb-2">{title}</h2>
      <p className="text-sm text-text-muted mb-6 max-w-sm">{description}</p>
      <Link to={homePath}>
        <Button variant="primary" leftIcon={<Home className="w-4 h-4" />}>
          {homeLabel}
        </Button>
      </Link>
    </div>
  );
};

