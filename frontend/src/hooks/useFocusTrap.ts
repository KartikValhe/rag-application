import { useEffect, useRef } from "react";

export interface FocusTrapOptions {
  onClose?: () => void;
  returnFocusRef?: React.RefObject<HTMLElement>;
  /** Element to focus first instead of the first focusable one (for example Cancel in a destructive dialog). */
  initialFocusRef?: React.RefObject<HTMLElement>;
  enabled?: boolean;
}

/**
 * useFocusTrap
 * ────────────
 * Accessible focus trap for dialogs, drawers, and modal sheets.
 * - Confines keyboard Tab / Shift+Tab navigation to the active container.
 * - Handles Escape key to trigger close.
 * - Restores focus to the triggering element (or previously active element) upon close.
 */
export function useFocusTrap(
  containerRef: React.RefObject<HTMLElement>,
  isActive: boolean,
  options: FocusTrapOptions = {}
): void {
  const { onClose, returnFocusRef, initialFocusRef, enabled = true } = options;
  const initialFocusRefRef = useRef(initialFocusRef);
  initialFocusRefRef.current = initialFocusRef;
  const previousActiveElement = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const returnFocusRefRef = useRef(returnFocusRef);
  returnFocusRefRef.current = returnFocusRef;

  useEffect(() => {
    if (!isActive || !enabled) return;

    // Capture element that had focus prior to opening trap
    previousActiveElement.current = document.activeElement as HTMLElement | null;

    const container = containerRef.current;
    if (!container) return;

    const focusableSelectors = [
      'a[href]',
      'button:not([disabled])',
      'textarea:not([disabled])',
      'input:not([disabled])',
      'select:not([disabled])',
      '[tabindex]:not([tabindex="-1"])',
    ].join(", ");

    const isVisible = (el: HTMLElement) => {
      if (el.offsetWidth > 0 || el.offsetHeight > 0 || el.getClientRects().length > 0) {
        return true;
      }
      // jsdom environment fallback (no layout engine)
      return typeof process !== "undefined" && process.env?.NODE_ENV === "test";
    };

    const getFocusables = (): HTMLElement[] => {
      if (!container) return [];
      return Array.from(
        container.querySelectorAll<HTMLElement>(focusableSelectors)
      ).filter(
        (el) =>
          !el.hasAttribute("disabled") &&
          el.getAttribute("aria-hidden") !== "true" &&
          isVisible(el)
      );
    };

    const focusables = getFocusables();
    if (focusables.length > 0 && focusables[0]) {
      // Focus the requested element (or the first one) after browser render cycle
      requestAnimationFrame(() => {
        (initialFocusRefRef.current?.current ?? focusables[0])?.focus();
      });
    }

    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onCloseRef.current?.();
        return;
      }

      if (e.key !== "Tab") return;

      const elements = getFocusables();
      if (elements.length === 0) {
        e.preventDefault();
        return;
      }

      const first = elements[0];
      const last = elements[elements.length - 1];

      if (e.shiftKey) {
        if (document.activeElement === first || !container.contains(document.activeElement)) {
          e.preventDefault();
          last.focus();
        }
      } else {
        if (document.activeElement === last || !container.contains(document.activeElement)) {
          e.preventDefault();
          first.focus();
        }
      }
    };

    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("keydown", handleKeyDown);

      // Return focus to trigger or previous element
      if (returnFocusRefRef.current?.current) {
        returnFocusRefRef.current.current.focus();
      } else if (previousActiveElement.current) {
        previousActiveElement.current.focus();
      }
    };
  }, [isActive, enabled, containerRef]);
}
