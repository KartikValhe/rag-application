import React from "react";
import { cn } from "@/utils/cn";
import { AlertCircle, CheckCircle2, Info, AlertTriangle, X } from "lucide-react";

export interface AlertProps {
  variant?: "info" | "success" | "warning" | "danger";
  title?: string;
  children: React.ReactNode;
  className?: string;
  onClose?: () => void;
}

export const Alert: React.FC<AlertProps> = ({
  variant = "info",
  title,
  children,
  className,
  onClose,
}) => {
  const variantStyles = {
    info: "bg-info-surface text-info-text border-info/20",
    success: "bg-success-surface text-success-text border-success/20",
    warning: "bg-warning-surface text-warning-text border-warning/20",
    danger: "bg-danger-surface text-danger-text border-danger/20",
  };

  const icons = {
    info: <Info className="h-5 w-5 shrink-0" />,
    success: <CheckCircle2 className="h-5 w-5 shrink-0" />,
    warning: <AlertTriangle className="h-5 w-5 shrink-0" />,
    danger: <AlertCircle className="h-5 w-5 shrink-0" />,
  };

  return (
    <div
      className={cn(
        "flex items-start justify-between gap-3 p-3.5 rounded-lg border text-sm",
        variantStyles[variant],
        className
      )}
      role="alert"
    >
      <div className="flex items-start gap-3">
        {icons[variant]}
        <div className="space-y-0.5">
          {title && <h5 className="font-semibold leading-tight">{title}</h5>}
          <div className="text-xs leading-relaxed opacity-90">{children}</div>
        </div>
      </div>
      {onClose && (
        <button
          type="button"
          onClick={onClose}
          className="p-1 rounded hover:opacity-75 transition-opacity"
          aria-label="Dismiss alert"
        >
          <X className="w-4 h-4" />
        </button>
      )}
    </div>
  );
};

