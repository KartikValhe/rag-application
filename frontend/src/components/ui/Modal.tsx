import React, { useRef } from "react";
import { X } from "lucide-react";
import { cn } from "@/utils/cn";
import { useScrollLock } from "@/hooks/useScrollLock";
import { useFocusTrap } from "@/hooks/useFocusTrap";

export type ModalSize = "sm" | "md" | "lg" | "xl" | "full";

export interface ModalProps {
  isOpen: boolean;
  onClose: () => void;
  title?: React.ReactNode;
  description?: string;
  children: React.ReactNode;
  footer?: React.ReactNode;
  size?: ModalSize;
  closeOnBackdrop?: boolean;
  className?: string;
}

export const Modal: React.FC<ModalProps> = ({
  isOpen,
  onClose,
  title,
  description,
  children,
  footer,
  size = "md",
  closeOnBackdrop = true,
  className,
}) => {
  const modalRef = useRef<HTMLDivElement>(null);

    // Lock body scroll via CSS class (zero inline styles)
    useScrollLock(isOpen);

    // Accessible focus trapping & Escape key listener
    useFocusTrap(modalRef, isOpen, { onClose });

  if (!isOpen) return null;

  const sizeClasses: Record<ModalSize, string> = {
    sm: "max-w-sm",
    md: "max-w-md",
    lg: "max-w-lg",
    xl: "max-w-2xl",
    full: "max-w-4xl",
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      className="fixed inset-0 z-50 flex items-end sm:items-center justify-center p-0 sm:p-6 overflow-hidden"
    >
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/60 backdrop-blur-sm transition-opacity"
        onClick={() => closeOnBackdrop && onClose()}
        aria-hidden="true"
      />

      {/* Modal Dialog Content: Full-screen bottom sheet on mobile, centered card on sm+ */}
      <div
        ref={modalRef}
        className={cn(
          "relative w-full border border-border bg-surface text-text shadow-xl transition-all z-10 overflow-hidden flex flex-col",
          "max-h-[92dvh] sm:max-h-[85dvh] rounded-t-2xl sm:rounded-xl sm:my-8",
          sizeClasses[size],
          className
        )}
      >
        {/* Header */}
            <div className="flex items-center justify-between border-b border-border-subtle p-4 sm:p-5 shrink-0 bg-surface">
              <div className="space-y-0.5 min-w-0 pr-3">
                {title && <h2 className="text-base sm:text-lg font-semibold text-text truncate">{title}</h2>}
                {description && <p className="text-xs text-text-muted line-clamp-2">{description}</p>}
              </div>
              <button
                type="button"
                onClick={onClose}
                className="min-w-[44px] min-h-[44px] flex items-center justify-center rounded-md text-text-subtle hover:bg-surface-hover hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary shrink-0 cursor-pointer"
                aria-label="Close dialog"
              >
                <X className="h-5 w-5" />
              </button>
            </div>

            {/* Body: scrollable */}
            <div className="p-4 sm:p-6 overflow-y-auto flex-1">{children}</div>

            {/* Sticky footer with safe-area padding on mobile */}
            {footer && (
                <div className="border-t border-border-subtle bg-surface p-4 sm:px-6 sm:py-3.5 shrink-0 sticky bottom-0 z-10 pb-[max(1rem,env(safe-area-inset-bottom))]">
                  {footer}
                </div>
        )}
              </div>
    </div>
          );
};
