export const FRIENDLY_ERROR_MESSAGE =
  "An unexpected error occurred while loading this section. Please reload the page to continue.";

export type FallbackAction = "dashboard" | "reload";

export interface ErrorFallbackContent {
  message: string;
  /** Raw error text. Only set in development, never shown to real users. */
  errorDetails: string | null;
  actions: FallbackAction[];
}

/** What the ErrorBoundary shows. Outside development: friendly text and a Reload button only. */
export function getErrorFallback(error: Error | null, isDev: boolean): ErrorFallbackContent {
  if (!isDev) {
    return { message: FRIENDLY_ERROR_MESSAGE, errorDetails: null, actions: ["reload"] };
  }
  return {
    message: FRIENDLY_ERROR_MESSAGE,
    errorDetails: error?.message ? error.message : null,
    actions: ["dashboard", "reload"],
  };
}
