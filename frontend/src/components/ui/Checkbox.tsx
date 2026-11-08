import React from "react";
import { Check } from "lucide-react";
import { cn } from "@/utils/cn";

export interface CheckboxProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> {
  label?: React.ReactNode;
  description?: string;
  error?: string;
}

export const Checkbox = React.forwardRef<HTMLInputElement, CheckboxProps>(
  ({ className, label, description, error, checked, disabled, id, ...props }, ref) => {
    const inputId = id || React.useId();

    return (
      <div className="flex flex-col space-y-1">
        <label
          htmlFor={inputId}
          className={cn(
            "flex items-start space-x-2.5 select-none",
            disabled ? "cursor-not-allowed opacity-60" : "cursor-pointer"
          )}
        >
          <div className="relative flex items-center justify-center mt-0.5">
            <input
              id={inputId}
              ref={ref}
              type="checkbox"
              checked={checked}
              disabled={disabled}
              className="peer sr-only"
              {...props}
            />
            <div
              className={cn(
                "h-4 w-4 rounded border border-border bg-surface transition-colors",
                "peer-focus-visible:ring-2 peer-focus-visible:ring-primary peer-focus-visible:ring-offset-2",
                "peer-checked:bg-primary peer-checked:border-primary peer-checked:text-primary-foreground",
                error && "border-danger",
                className
              )}
            >
              <Check className="h-3 w-3 stroke-[3] text-white opacity-0 transition-opacity peer-checked:opacity-100" />
            </div>
          </div>
          {(label || description) && (
            <div className="text-sm text-left">
              {label && <span className="font-medium text-text leading-tight block">{label}</span>}
              {description && <span className="text-xs text-text-muted mt-0.5 block">{description}</span>}
            </div>
          )}
        </label>
        {error && <p className="text-xs text-danger font-medium">{error}</p>}
      </div>
    );
  }
);

Checkbox.displayName = "Checkbox";

