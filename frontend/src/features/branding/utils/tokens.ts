/**
 * features/branding/utils/tokens.ts
 * ─────────────────────────────────
 * Runtime design token engine:
 * - Color manipulation and WCAG AA contrast calculations.
 * - Dynamic token derivation for light and dark modes (hover, active, subtle, foreground).
 * - Allow-list enforcement for border radii and system font stacks.
 * - Dynamic CSS injection into the DOM (global and scoped).
 */

import type {
  DesignTokens,
  RadiusToken,
  FontFamilyToken,
} from "../types.ts";
import { DEFAULT_TOKENS } from "../types.ts";

// ── Allow-lists ─────────────────────────────────────────────────────────────

export const RADIUS_ALLOWLIST: Record<
  RadiusToken,
  { sm: string; md: string; lg: string; xl: string; full: string }
> = {
  none: {
    sm: "0px",
    md: "0px",
    lg: "0px",
    xl: "0px",
    full: "0px",
  },
  sm: {
    sm: "0.125rem",
    md: "0.25rem",
    lg: "0.375rem",
    xl: "0.5rem",
    full: "9999px",
  },
  md: {
    sm: "0.25rem",
    md: "0.375rem",
    lg: "0.5rem",
    xl: "0.75rem",
    full: "9999px",
  },
  lg: {
    sm: "0.375rem",
    md: "0.5rem",
    lg: "0.75rem",
    xl: "1rem",
    full: "9999px",
  },
  xl: {
    sm: "0.5rem",
    md: "0.75rem",
    lg: "1rem",
    xl: "1.25rem",
    full: "9999px",
  },
  full: {
    sm: "0.5rem",
    md: "0.75rem",
    lg: "1.25rem",
    xl: "1.75rem",
    full: "9999px",
  },
};

export const FONT_FAMILY_ALLOWLIST: Record<FontFamilyToken, string> = {
  "system-ui":
    '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif',
  sans: 'ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", sans-serif',
  serif: 'ui-serif, Georgia, Cambria, "Times New Roman", Times, serif',
  mono: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace',
};

// ── Color Math & WCAG Contrast ──────────────────────────────────────────────

export function isValidHex(hex: unknown): hex is string {
  return typeof hex === "string" && /^#[0-9a-fA-F]{6}$/.test(hex);
}

export function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  if (!isValidHex(hex)) return null;
  const clean = hex.slice(1);
  return {
    r: parseInt(clean.slice(0, 2), 16),
    g: parseInt(clean.slice(2, 4), 16),
    b: parseInt(clean.slice(4, 6), 16),
  };
}

export function rgbToHex(r: number, g: number, b: number): string {
  const clamp = (n: number) => Math.max(0, Math.min(255, Math.round(n)));
  const toHex = (n: number) => clamp(n).toString(16).padStart(2, "0");
  return `#${toHex(r)}${toHex(g)}${toHex(b)}`;
}

export function calculateRelativeLuminance(hex: string): number {
  const rgb = hexToRgb(hex);
  if (!rgb) return 0;
  const toLinear = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  return (
    0.2126 * toLinear(rgb.r) +
    0.7152 * toLinear(rgb.g) +
    0.0722 * toLinear(rgb.b)
  );
}

export function calculateContrastRatio(hex1: string, hex2: string): number {
  const l1 = calculateRelativeLuminance(hex1);
  const l2 = calculateRelativeLuminance(hex2);
  const lighter = Math.max(l1, l2);
  const darker = Math.min(l1, l2);
  return (lighter + 0.05) / (darker + 0.05);
}

export function mixColors(hex1: string, hex2: string, weight: number): string {
  const rgb1 = hexToRgb(hex1);
  const rgb2 = hexToRgb(hex2);
  if (!rgb1 || !rgb2) return hex1;
  const clampedWeight = Math.max(0, Math.min(1, weight));
  const r = rgb1.r + (rgb2.r - rgb1.r) * clampedWeight;
  const g = rgb1.g + (rgb2.g - rgb1.g) * clampedWeight;
  const b = rgb1.b + (rgb2.b - rgb1.b) * clampedWeight;
  return rgbToHex(r, g, b);
}

export function getAccessibleForeground(
  backgroundHex: string,
  isDarkTheme: boolean
): string {
  const white = "#ffffff";
  const dark = isDarkTheme ? "#090d16" : "#0f172a";
  const contrastWhite = calculateContrastRatio(backgroundHex, white);
  const contrastDark = calculateContrastRatio(backgroundHex, dark);

  // Per WCAG AA, 4.5:1 is required for normal text.
  if (contrastWhite >= 4.5) return white;
  if (contrastDark >= 4.5) return dark;
  return contrastWhite >= contrastDark ? white : dark;
}

export interface WcagContrastResult {
  isValid: boolean;
  whiteContrast: number;
  darkTextContrast: number;
  lightSurfaceContrast: number;
  darkSurfaceContrast: number;
  errorMessage?: string;
}

export function checkWcagContrast(primaryHex: string): WcagContrastResult {
  if (!isValidHex(primaryHex)) {
    return {
      isValid: false,
      whiteContrast: 1,
      darkTextContrast: 1,
      lightSurfaceContrast: 1,
      darkSurfaceContrast: 1,
      errorMessage: "Primary color must be a valid 6-digit hex code (e.g. #2563eb).",
    };
  }

  const white = "#ffffff";
  const darkText = "#0f172a";
  const darkSurface = "#0f172a";
  const lightSurface = "#ffffff";

  const cWhite = calculateContrastRatio(primaryHex, white);
  const cDark = calculateContrastRatio(primaryHex, darkText);
  const cLightSurf = calculateContrastRatio(primaryHex, lightSurface);
  const cDarkSurf = calculateContrastRatio(primaryHex, darkSurface);

  if (cWhite < 4.5 && cDark < 4.5) {
    return {
      isValid: false,
      whiteContrast: cWhite,
      darkTextContrast: cDark,
      lightSurfaceContrast: cLightSurf,
      darkSurfaceContrast: cDarkSurf,
      errorMessage: `Primary color ${primaryHex} lacks sufficient text contrast (minimum 4.5:1) with either white (${cWhite.toFixed(2)}:1) or dark text (${cDark.toFixed(2)}:1).`,
    };
  }

  if (cLightSurf < 3.0) {
    return {
      isValid: false,
      whiteContrast: cWhite,
      darkTextContrast: cDark,
      lightSurfaceContrast: cLightSurf,
      darkSurfaceContrast: cDarkSurf,
      errorMessage: `Primary color ${primaryHex} has insufficient contrast against light backgrounds (${cLightSurf.toFixed(2)}:1 < 3.0:1). Please pick a darker shade.`,
    };
  }

  if (cDarkSurf < 3.0) {
    return {
      isValid: false,
      whiteContrast: cWhite,
      darkTextContrast: cDark,
      lightSurfaceContrast: cLightSurf,
      darkSurfaceContrast: cDarkSurf,
      errorMessage: `Primary color ${primaryHex} has insufficient contrast against dark backgrounds (${cDarkSurf.toFixed(2)}:1 < 3.0:1). Please pick a lighter or more saturated shade.`,
    };
  }

  return {
    isValid: true,
    whiteContrast: cWhite,
    darkTextContrast: cDark,
    lightSurfaceContrast: cLightSurf,
    darkSurfaceContrast: cDarkSurf,
  };
}

// ── Derived Token Generation ────────────────────────────────────────────────

export interface ComputedPalette {
  primary: string;
  primaryHover: string;
  primaryActive: string;
  primaryLight: string;
  primaryForeground: string;
  secondary: string;
  secondaryHover: string;
  secondaryForeground: string;
}

export function computePalette(
  tokens: DesignTokens,
  isDark: boolean
): ComputedPalette {
  const primary = isValidHex(tokens.primary)
    ? tokens.primary
    : DEFAULT_TOKENS.primary;
  const secondary = isValidHex(tokens.secondary)
    ? tokens.secondary
    : DEFAULT_TOKENS.secondary;

  if (!isDark) {
    // Light Mode Token Derivation
    const primaryHover = mixColors(primary, "#000000", 0.12);
    const primaryActive = mixColors(primary, "#000000", 0.22);
    const primaryLight = mixColors(primary, "#ffffff", 0.92);
    const primaryForeground = getAccessibleForeground(primary, false);

    const secondaryHover = mixColors(secondary, "#000000", 0.15);
    const secondaryForeground = getAccessibleForeground(secondary, false);

    return {
      primary,
      primaryHover,
      primaryActive,
      primaryLight,
      primaryForeground,
      secondary,
      secondaryHover,
      secondaryForeground,
    };
  }

  // Dark Mode Token Derivation
  const contrastAgainstDarkSurface = calculateContrastRatio(primary, "#090d16");
  const darkPrimary =
    contrastAgainstDarkSurface < 4.0
      ? mixColors(primary, "#ffffff", 0.22)
      : primary;

  const darkPrimaryHover = mixColors(darkPrimary, "#ffffff", 0.15);
  const darkPrimaryActive = mixColors(darkPrimary, "#000000", 0.15);
  const darkPrimaryLight = mixColors(darkPrimary, "#090d16", 0.82);
  const darkPrimaryForeground = getAccessibleForeground(darkPrimary, true);

  const darkSecondary = mixColors(secondary, "#ffffff", 0.35);
  const darkSecondaryHover = mixColors(darkSecondary, "#ffffff", 0.2);
  const darkSecondaryForeground = getAccessibleForeground(darkSecondary, true);

  return {
    primary: darkPrimary,
    primaryHover: darkPrimaryHover,
    primaryActive: darkPrimaryActive,
    primaryLight: darkPrimaryLight,
    primaryForeground: darkPrimaryForeground,
    secondary: darkSecondary,
    secondaryHover: darkSecondaryHover,
    secondaryForeground: darkSecondaryForeground,
  };
}

// ── CSS Generation & Injection ──────────────────────────────────────────────

export function generateThemeCss(tokens: DesignTokens): string {
  const light = computePalette(tokens, false);
  const dark = computePalette(tokens, true);

  const radiusKey = tokens.radius in RADIUS_ALLOWLIST ? tokens.radius : "md";
  const radii = RADIUS_ALLOWLIST[radiusKey];

  const fontKey =
    tokens.font_family in FONT_FAMILY_ALLOWLIST
      ? tokens.font_family
      : "system-ui";
  const fontStack = FONT_FAMILY_ALLOWLIST[fontKey];

  return `
/* Dynamic Tenant Branding Theme */
:root {
  --color-primary: ${light.primary};
  --color-primary-hover: ${light.primaryHover};
  --color-primary-active: ${light.primaryActive};
  --color-primary-light: ${light.primaryLight};
  --color-primary-foreground: ${light.primaryForeground};

  --color-secondary: ${light.secondary};
  --color-secondary-hover: ${light.secondaryHover};
  --color-secondary-foreground: ${light.secondaryForeground};

  --radius-sm: ${radii.sm};
  --radius-md: ${radii.md};
  --radius-lg: ${radii.lg};
  --radius-xl: ${radii.xl};
  --radius-full: ${radii.full};

  --font-family-base: ${fontStack};
  --font-family-heading: ${fontStack};
}

.dark {
  --color-primary: ${dark.primary};
  --color-primary-hover: ${dark.primaryHover};
  --color-primary-active: ${dark.primaryActive};
  --color-primary-light: ${dark.primaryLight};
  --color-primary-foreground: ${dark.primaryForeground};

  --color-secondary: ${dark.secondary};
  --color-secondary-hover: ${dark.secondaryHover};
  --color-secondary-foreground: ${dark.secondaryForeground};
}
`.trim();
}

/**
 * Generates scoped CSS for the live preview container.
 * Strictly scopes variables to .${scopeClassName} without modifying :root or .dark globals.
 */
export function generateScopedThemeCss(
  tokens: DesignTokens,
  scopeClassName: string
): string {
  const light = computePalette(tokens, false);
  const dark = computePalette(tokens, true);

  const radiusKey = tokens.radius in RADIUS_ALLOWLIST ? tokens.radius : "md";
  const radii = RADIUS_ALLOWLIST[radiusKey];

  const fontKey =
    tokens.font_family in FONT_FAMILY_ALLOWLIST
      ? tokens.font_family
      : "system-ui";
  const fontStack = FONT_FAMILY_ALLOWLIST[fontKey];

  return `
.${scopeClassName} {
  --color-primary: ${light.primary};
  --color-primary-hover: ${light.primaryHover};
  --color-primary-active: ${light.primaryActive};
  --color-primary-light: ${light.primaryLight};
  --color-primary-foreground: ${light.primaryForeground};

  --color-secondary: ${light.secondary};
  --color-secondary-hover: ${light.secondaryHover};
  --color-secondary-foreground: ${light.secondaryForeground};

  --radius-sm: ${radii.sm};
  --radius-md: ${radii.md};
  --radius-lg: ${radii.lg};
  --radius-xl: ${radii.xl};
  --radius-full: ${radii.full};

  --font-family-base: ${fontStack};
  --font-family-heading: ${fontStack};
}

.${scopeClassName}.preview-dark, .${scopeClassName} .preview-dark {
  --color-primary: ${dark.primary};
  --color-primary-hover: ${dark.primaryHover};
  --color-primary-active: ${dark.primaryActive};
  --color-primary-light: ${dark.primaryLight};
  --color-primary-foreground: ${dark.primaryForeground};

  --color-secondary: ${dark.secondary};
  --color-secondary-hover: ${dark.secondaryHover};
  --color-secondary-foreground: ${dark.secondaryForeground};
}
`.trim();
}

export const THEME_STYLE_ELEMENT_ID = "app-theme-tokens";

export function applyThemeTokens(tokens: DesignTokens): void {
  if (typeof document === "undefined") return;

  const css = generateThemeCss(tokens);
  let styleEl = document.getElementById(
    THEME_STYLE_ELEMENT_ID
  ) as HTMLStyleElement | null;

  if (!styleEl) {
    styleEl = document.createElement("style");
    styleEl.id = THEME_STYLE_ELEMENT_ID;
    document.head.appendChild(styleEl);
  }

  styleEl.textContent = css;
}
