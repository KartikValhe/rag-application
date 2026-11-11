import React from "react";
import { cn } from "@/utils/cn";

export interface SpinnerProps {
  size?: "sm" | "md" | "lg";
  className?: string;
}

export const Spinner: React.FC<SpinnerProps> = ({ size = "md", className }) => {
  const sizeStyles = {
    sm: "w-4 h-4 border-2",
    md: "w-6 h-6 border-2",
    lg: "w-8 h-8 border-4",
  };

  return (
    <div
      className={cn(
        "rounded-full border-primary border-t-transparent animate-spin",
        sizeStyles[size],
        className
      )}
      role="status"
      aria-label="Loading"
    />
  );
};

