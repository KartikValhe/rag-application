import React from "react";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "@/theme/ThemeProvider";
import { cn } from "@/utils/cn";

export interface ThemeToggleProps {
  className?: string;
  size?: "sm" | "md";
}

export const ThemeToggle: React.FC<ThemeToggleProps> = ({ className, size = "md" }) => {
  const { isDark, toggleTheme } = useTheme();

  return (
    <button
      type="button"
      onClick={toggleTheme}
      className={cn(
        "min-w-[44px] min-h-[44px] flex items-center justify-center rounded-lg text-text-muted hover:text-text hover:bg-surface-hover border border-border bg-surface/80 backdrop-blur-xs shadow-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary select-none cursor-pointer",
        className
      )}
      aria-label={isDark ? "Switch to light mode" : "Switch to dark mode"}
      title={isDark ? "Switch to light mode" : "Switch to dark mode"}
    >
      {isDark ? (
        <Sun className={cn(size === "sm" ? "w-4 h-4" : "w-4 h-4", "text-warning")} />
      ) : (
        <Moon className={cn(size === "sm" ? "w-4 h-4" : "w-4 h-4", "text-text-muted")} />
      )}
    </button>
  );
};
