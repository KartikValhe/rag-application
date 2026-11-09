/**
 * features/branding/schemas/brandingSchema.ts
 * ───────────────────────────────────────────
 * Zod validation schema for admin branding configuration.
 */

import * as z from "zod";

export const brandingFormSchema = z.object({
  display_name: z
    .string()
    .min(1, "Display name is required")
    .max(255, "Display name must be at most 255 characters"),
  tagline: z.string().max(255, "Tagline must be at most 255 characters").optional(),
  footer_text: z.string().max(512, "Footer text must be at most 512 characters").optional(),
  support_email: z
    .string()
    .email("Please enter a valid email address")
    .max(255, "Support email must be at most 255 characters")
    .optional()
    .or(z.literal("")),
  primary: z
    .string()
    .regex(/^#[0-9a-fA-F]{6}$/, "Must be a valid 6-digit hex color (e.g. #2563eb)"),
  secondary: z
    .string()
    .regex(/^#[0-9a-fA-F]{6}$/, "Must be a valid 6-digit hex color (e.g. #475569)"),
  accent: z
    .string()
    .regex(/^#[0-9a-fA-F]{6}$/, "Must be a valid 6-digit hex color (e.g. #06b6d4)"),
  radius: z.enum(["none", "sm", "md", "lg", "xl", "full"]),
  font_family: z.enum(["system-ui", "sans", "serif", "mono"]),
});

export type BrandingFormData = z.infer<typeof brandingFormSchema>;

