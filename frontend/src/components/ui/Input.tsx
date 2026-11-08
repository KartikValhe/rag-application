import React from "react";
import { cn } from "@/utils/cn";

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  helperText?: string;
  helper?: string;
  leftIcon?: React.ReactNode;
  rightIcon?: React.ReactNode;
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
    ({ className, label, error, helperText, helper, leftIcon, rightIcon, id, ...props }, ref) => {
      const inputId = id || (label ? label.toLowerCase().replace(/\s+/g, "-") : undefined);
      const displayHelper = helperText || helper;

      return (
        <div className="w-full space-y-1.5 text-left">
          {label && (
            <label htmlFor={inputId} className="block text-sm font-medium text-text">
              {label}
              {props.required && <span className="ml-1 text-danger">*</span>}
            </label>
          )}
          <div className="relative flex items-center">
            {leftIcon && (
              <div className="pointer-events-none absolute left-3 flex items-center text-text-subtle">
                {leftIcon}
              </div>
            )}
            <input
              id={inputId}
              ref={ref}
              className={cn(
                "input-base",
                leftIcon && "pl-9",
                rightIcon && "pr-9",
                error && "border-danger focus-visible:ring-danger",
                className
              )}
              {...props}
            />
            {rightIcon && (
              <div className="absolute right-3 flex items-center text-text-subtle">
                {rightIcon}
              </div>
            )}
          </div>
          {error && <p className="text-xs text-danger font-medium">{error}</p>}
          {!error && displayHelper && <p className="text-xs text-text-muted">{displayHelper}</p>}
        </div>
      );
    }
);

Input.displayName = "Input";
