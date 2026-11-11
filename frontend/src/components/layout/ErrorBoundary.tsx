import { Component, ErrorInfo, ReactNode } from "react";
import { AlertTriangle, RefreshCw, Home } from "lucide-react";
import { Button } from "@/components/ui/Button";

interface ErrorBoundaryProps {
  children: ReactNode;
  fallbackTitle?: string;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  public override state: ErrorBoundaryState = {
    hasError: false,
    error: null,
  };

  public static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  public override componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    const isDev =
      typeof import.meta !== "undefined" &&
      typeof import.meta.env !== "undefined" &&
      Boolean(import.meta.env?.DEV);
    if (isDev) {
      console.error("ErrorBoundary caught an error:", error, errorInfo);
    }
  }

  private handleReset = () => {
    this.setState({ hasError: false, error: null });
  };

  public override render() {
    if (this.state.hasError) {
      const isDev =
        typeof import.meta !== "undefined" &&
        typeof import.meta.env !== "undefined" &&
        Boolean(import.meta.env?.DEV);
      const friendlyMessage =
        "An unexpected error occurred while loading this section. Please reload the page to continue.";
      const displayMessage =
        isDev && this.state.error?.message
          ? this.state.error.message
          : friendlyMessage;

      return (
        <div className="min-h-[50vh] flex items-center justify-center p-6 bg-background">
          <div className="max-w-md w-full bg-surface border border-border rounded-xl p-6 shadow-lg text-center space-y-4">
            <div className="w-12 h-12 rounded-full bg-danger/10 text-danger flex items-center justify-center mx-auto">
              <AlertTriangle className="w-6 h-6" />
            </div>
            <div>
              <h3 className="text-lg font-bold text-text">
                {this.props.fallbackTitle || "Something went wrong"}
              </h3>
              <p className="text-xs text-text-muted mt-1 break-words">
                {displayMessage}
              </p>
            </div>
            <div className="flex items-center justify-center gap-3 pt-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  this.handleReset();
                  window.location.href = "/";
                }}
                leftIcon={<Home className="w-4 h-4" />}
                className="min-h-[44px] sm:min-h-[36px]"
              >
                Go to Dashboard
              </Button>
              <Button
                variant="primary"
                size="sm"
                onClick={() => {
                  this.handleReset();
                  window.location.reload();
                }}
                leftIcon={<RefreshCw className="w-4 h-4" />}
                className="min-h-[44px] sm:min-h-[36px]"
              >
                Reload Page
              </Button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
