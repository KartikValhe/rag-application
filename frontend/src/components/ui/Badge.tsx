import React from "react";
import { cn } from "@/utils/cn";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: "primary" | "secondary" | "success" | "warning" | "danger" | "info";
  size?: "sm" | "md";
}

export const Badge: React.FC<BadgeProps> = ({
  className,
  variant = "secondary",
  size = "md",
  children,
  ...props
}) => {
  const variantStyles = {
    primary: "bg-primary-light text-primary",
    secondary: "bg-surface-muted text-text-muted border border-border",
    success: "bg-success-surface text-success-text",
    warning: "bg-warning-surface text-warning-text",
    danger: "bg-danger-surface text-danger-text",
    info: "bg-info-surface text-info-text",
  };

  const sizeStyles = {
    sm: "text-xs px-2 py-0.5 font-medium rounded-full",
    md: "text-xs px-2.5 py-1 font-semibold rounded-full",
  };

  return (
    <span
      className={cn(
        "inline-flex items-center justify-center tracking-wide",
        variantStyles[variant],
        sizeStyles[size],
        className
      )}
      {...props}
    >
      {children}
    </span>
  );
};

