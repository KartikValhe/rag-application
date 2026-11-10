import React, { createContext, useContext, useState, useCallback } from "react";
import { CheckCircle2, AlertTriangle, AlertCircle, Info, X } from "lucide-react";
import { cn } from "@/utils/cn";

export type ToastType = "success" | "error" | "warning" | "info";

export interface ToastMessage {
  id: string;
  type: ToastType;
  title?: string;
  message: string;
}

export interface ToastContextType {
  toast: {
    success: (message: string, title?: string) => void;
    error: (message: string, title?: string) => void;
    warning: (message: string, title?: string) => void;
    info: (message: string, title?: string) => void;
  };
  removeToast: (id: string) => void;
}

const ToastContext = createContext<ToastContextType | undefined>(undefined);

export const ToastProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  const removeToast = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const addToast = useCallback((type: ToastType, message: string, title?: string) => {
    const id = Math.random().toString(36).substring(2, 9);
    setToasts((prev) => [...prev, { id, type, title, message }]);

    // Auto-dismiss after 4.5 seconds
    setTimeout(() => {
      removeToast(id);
    }, 4500);
  }, [removeToast]);

  const toast = {
    success: (message: string, title?: string) => addToast("success", message, title),
    error: (message: string, title?: string) => addToast("error", message, title),
    warning: (message: string, title?: string) => addToast("warning", message, title),
    info: (message: string, title?: string) => addToast("info", message, title),
  };

  const icons: Record<ToastType, React.ReactNode> = {
    success: <CheckCircle2 className="h-5 w-5 text-success" />,
    error: <AlertCircle className="h-5 w-5 text-danger" />,
    warning: <AlertTriangle className="h-5 w-5 text-warning" />,
    info: <Info className="h-5 w-5 text-info" />,
  };

  const borders: Record<ToastType, string> = {
    success: "border-success/30",
    error: "border-danger/30",
    warning: "border-warning/30",
    info: "border-info/30",
  };

  return (
    <ToastContext.Provider value={{ toast, removeToast }}>
      {children}
      {/* Floating Notifications Container: Inset & full-width on mobile without horizontal overflow */}
      <div
        role="region"
        aria-live="polite"
        aria-label="Notifications"
        className="fixed bottom-4 inset-x-4 sm:inset-x-auto sm:right-4 sm:bottom-4 z-50 flex flex-col space-y-2 pointer-events-none sm:max-w-sm w-auto sm:w-full pb-[env(safe-area-inset-bottom)]"
      >
        {toasts.map((t) => (
          <div
            key={t.id}
            className={cn(
              "pointer-events-auto flex items-start space-x-3 rounded-lg border bg-surface p-4 shadow-lg text-text transition-all animate-in slide-in-from-bottom-2",
              borders[t.type]
            )}
          >
            <div className="flex-shrink-0 mt-0.5">{icons[t.type]}</div>
              <div className="flex-1 text-left min-w-0">
                {t.title && <h4 className="text-sm font-semibold text-text truncate">{t.title}</h4>}
                <p className="text-xs text-text-muted mt-0.5 leading-relaxed break-words">{t.message}</p>
              </div>
              {/* 44px minimum touch target for dismiss button */}
              <button
                type="button"
                onClick={() => removeToast(t.id)}
                className="min-w-[44px] min-h-[44px] flex items-center justify-center text-text-subtle hover:text-text rounded-md transition-colors shrink-0 -mr-2 -my-2 cursor-pointer focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary"
                aria-label="Dismiss notification"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
        ))}
          </div>
    </ToastContext.Provider>
  );
};

export const useToast = (): ToastContextType["toast"] => {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error("useToast must be used within a ToastProvider");
  }
  return context.toast;
};
