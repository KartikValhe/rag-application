import { useEffect } from "react";

/**
 * useScrollLock
 * ─────────────
 * Locks page body scroll via a CSS class on <html> and <body>.
 * Strictly adheres to project rules: classList toggle, zero inline styles.
 */
export function useScrollLock(isLocked: boolean): void {
  useEffect(() => {
    if (!isLocked) return;

    const root = document.documentElement;
    const body = document.body;

    root.classList.add("body-scroll-lock");
    body.classList.add("body-scroll-lock");

    return () => {
      root.classList.remove("body-scroll-lock");
      body.classList.remove("body-scroll-lock");
    };
  }, [isLocked]);
}

