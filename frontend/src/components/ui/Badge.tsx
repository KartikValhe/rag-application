import React from "react";
import { cn } from "@/utils/cn";

export type BadgeVariant = "default" | "secondary" | "primary" | "success" | "warning" | "danger" | "info";
export type BadgeSize = "sm" | "md";

export interface BadgeProps extends React.HTMLAttributes<HTMLSpanElement> {
  variant?: BadgeVariant;
  size?: BadgeSize;
}

export const Badge: React.FC<BadgeProps> = ({
  className,
  variant = "default",
  size = "md",
  children,
  ...props
}) => {
  const variantClasses: Record<BadgeVariant, string> = {
    default: "bg-surface-muted text-text-muted border border-border",
    secondary: "bg-surface-muted text-text-muted border border-border",
    primary: "bg-primary-light text-primary border border-primary/20",
    success: "bg-success-surface text-success-text border border-success/30",
    warning: "bg-warning-surface text-warning-text border border-warning/30",
    danger: "bg-danger-surface text-danger-text border border-danger/30",
    info: "bg-info-surface text-info-text border border-info/30",
  };

  const sizeClasses: Record<BadgeSize, string> = {
    sm: "px-2 py-0.5 text-[10px]",
    md: "px-2.5 py-0.5 text-xs",
  };

  return (
    <span
      className={cn(
        "inline-flex items-center font-medium rounded-full leading-none",
        variantClasses[variant],
        sizeClasses[size],
        className
      )}
      {...props}
    >
      {children}
    </span>
  );
};
