/**
 * features/branding/components/BrandingEditorPage.tsx
 * ───────────────────────────────────────────────────
 * Admin branding editor with live preview, WCAG AA contrast validation,
 * image upload handling, and optimistic concurrency.
 * Follows strict design system rules: zero inline styles, Tailwind tokens only.
 */

import React, { useState, useEffect, useMemo, useRef, useCallback } from "react";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import {
  Sun,
  Moon,
  Upload,
  RefreshCw,
  Save,
  CheckCircle2,
  AlertCircle,
  Trash2,
  Layout,
  Palette,
  Type,
  ImageIcon,
} from "lucide-react";

import { AppShell } from "@/components/layout/AppShell";
import {
  Button,
  Input,
  Select,
  Alert,
  Card,
  Badge,
  Skeleton,
  ConfirmDialog,
  useToast,
} from "@/components/ui";

import {
  getAdminBranding,
  updateAdminBranding,
  uploadAdminLogo,
  uploadAdminFavicon,
  resetAdminBranding,
} from "../api/brandingApi.ts";

import {
  checkWcagContrast,
  generateScopedThemeCss,
  isValidHex,
} from "../utils/tokens.ts";

import {
  brandingFormSchema,
  type BrandingFormData,
} from "../schemas/brandingSchema.ts";

import { useBranding } from "../context/BrandingContext.tsx";
import type {
  BrandingResponse,
  DesignTokens,
  RadiusToken,
  FontFamilyToken,
} from "../types.ts";

const PREVIEW_STYLE_ID = "branding-preview-css";
const PREVIEW_SCOPE_CLASS = "branding-preview-scope";

export const BrandingEditorPage: React.FC = () => {
  const toast = useToast();
  const { refreshBranding } = useBranding();

  const [isLoading, setIsLoading] = useState<boolean>(true);
  const [isSaving, setIsSaving] = useState<boolean>(false);
  const [currentBranding, setCurrentBranding] = useState<BrandingResponse | null>(null);
  const [previewDark, setPreviewDark] = useState<boolean>(false);
  const [showResetConfirm, setShowResetConfirm] = useState<boolean>(false);

  // Logo & Favicon upload state
  const [logoFile, setLogoFile] = useState<File | null>(null);
  const [logoPreviewUrl, setLogoPreviewUrl] = useState<string | null>(null);
  const [faviconFile, setFaviconFile] = useState<File | null>(null);
  const [faviconPreviewUrl, setFaviconPreviewUrl] = useState<string | null>(null);

  const logoInputRef = useRef<HTMLInputElement>(null);
  const faviconInputRef = useRef<HTMLInputElement>(null);

  const {
    register,
    handleSubmit,
    watch,
    setValue,
    reset,
    formState: { errors, isDirty },
  } = useForm<BrandingFormData>({
    resolver: zodResolver(brandingFormSchema),
    defaultValues: {
      display_name: "",
      tagline: "",
      footer_text: "",
      support_email: "",
      primary: "#2563eb",
      secondary: "#475569",
      accent: "#06b6d4",
      radius: "md",
      font_family: "system-ui",
    },
  });

  const watchedPrimary = watch("primary");
  const watchedSecondary = watch("secondary");
  const watchedAccent = watch("accent");
  const watchedRadius = watch("radius");
  const watchedFontFamily = watch("font_family");
  const watchedDisplayName = watch("display_name");
  const watchedTagline = watch("tagline");
  const watchedFooterText = watch("footer_text");

  // Load current branding on mount
  const loadBranding = useCallback(async () => {
    setIsLoading(true);
    try {
      const data = await getAdminBranding();
      setCurrentBranding(data);
      reset({
        display_name: data.display_name || "",
        tagline: data.tagline || "",
        footer_text: data.footer_text || "",
        support_email: data.support_email || "",
        primary: data.tokens.primary || "#2563eb",
        secondary: data.tokens.secondary || "#475569",
        accent: data.tokens.accent || "#06b6d4",
        radius: data.tokens.radius || "md",
        font_family: data.tokens.font_family || "system-ui",
      });
      setLogoPreviewUrl(data.logo_url);
      setFaviconPreviewUrl(data.favicon_url);
    } catch {
      toast.error("Failed to load tenant branding configuration.");
    } finally {
      setIsLoading(false);
    }
  }, [reset, toast]);

  useEffect(() => {
    loadBranding();
  }, [loadBranding]);

  // Clean up object URLs on unmount
  useEffect(() => {
    return () => {
      if (logoPreviewUrl && logoPreviewUrl.startsWith("blob:")) {
        URL.revokeObjectURL(logoPreviewUrl);
      }
      if (faviconPreviewUrl && faviconPreviewUrl.startsWith("blob:")) {
        URL.revokeObjectURL(faviconPreviewUrl);
      }
    };
  }, [logoPreviewUrl, faviconPreviewUrl]);

  // Unsaved changes browser warning
  useEffect(() => {
    const handleBeforeUnload = (e: BeforeUnloadEvent) => {
      if (isDirty || logoFile || faviconFile) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, [isDirty, logoFile, faviconFile]);

  // Real-time WCAG AA Contrast Evaluation
  const contrastAnalysis = useMemo(() => {
    if (!isValidHex(watchedPrimary)) {
      return {
        isValid: false,
        whiteContrast: 1,
        darkTextContrast: 1,
        lightSurfaceContrast: 1,
        darkSurfaceContrast: 1,
        errorMessage: "Primary color must be a valid 6-digit hex code.",
      };
    }
    return checkWcagContrast(watchedPrimary);
  }, [watchedPrimary]);

  // Real-time Scoped Preview CSS Generator
  // Strictly isolates CSS variables to .branding-preview-scope without inline styles
  useEffect(() => {
    if (typeof document === "undefined") return;

    const draftTokens: DesignTokens = {
      primary: isValidHex(watchedPrimary) ? watchedPrimary : "#2563eb",
      secondary: isValidHex(watchedSecondary) ? watchedSecondary : "#475569",
      accent: isValidHex(watchedAccent) ? watchedAccent : "#06b6d4",
      radius: (watchedRadius as RadiusToken) || "md",
      font_family: (watchedFontFamily as FontFamilyToken) || "system-ui",
    };

    const scopedCss = generateScopedThemeCss(draftTokens, PREVIEW_SCOPE_CLASS);
    let styleEl = document.getElementById(PREVIEW_STYLE_ID) as HTMLStyleElement | null;
    if (!styleEl) {
      styleEl = document.createElement("style");
      styleEl.id = PREVIEW_STYLE_ID;
      document.head.appendChild(styleEl);
    }
    styleEl.textContent = scopedCss;

    return () => {
      const el = document.getElementById(PREVIEW_STYLE_ID);
      if (el) el.remove();
    };
  }, [watchedPrimary, watchedSecondary, watchedAccent, watchedRadius, watchedFontFamily]);

  // Client-side Image Validation & Selection
  const handleImageSelect = (
    e: React.ChangeEvent<HTMLInputElement>,
    type: "logo" | "favicon"
  ) => {
    const file = e.target.files?.[0];
    if (!file) return;

    // 1. MIME Type check
    const allowedTypes = ["image/png", "image/jpeg", "image/webp"];
    if (!allowedTypes.includes(file.type)) {
      toast.error("Unsupported file type. Only PNG, JPEG, and WebP are allowed (No SVG).");
      e.target.value = "";
      return;
    }

    // 2. File size check (1 MB)
    if (file.size > 1024 * 1024) {
      toast.error("File is too large. Maximum allowed size is 1 MB.");
      e.target.value = "";
      return;
    }

    // 3. Dimension check in browser
    const objectUrl = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      const maxDim = type === "logo" ? 2000 : 512;
      if (img.width > maxDim || img.height > maxDim) {
        toast.error(
          `Image dimensions (${img.width}x${img.height}) exceed maximum allowed (${maxDim}x${maxDim}).`
        );
        URL.revokeObjectURL(objectUrl);
        e.target.value = "";
        return;
      }

      if (type === "logo") {
        if (logoPreviewUrl && logoPreviewUrl.startsWith("blob:")) {
          URL.revokeObjectURL(logoPreviewUrl);
        }
        setLogoFile(file);
        setLogoPreviewUrl(objectUrl);
      } else {
        if (faviconPreviewUrl && faviconPreviewUrl.startsWith("blob:")) {
          URL.revokeObjectURL(faviconPreviewUrl);
        }
        setFaviconFile(file);
        setFaviconPreviewUrl(objectUrl);
      }
    };
    img.onerror = () => {
      toast.error("Invalid image content. File could not be decoded.");
      URL.revokeObjectURL(objectUrl);
      e.target.value = "";
    };
    img.src = objectUrl;
  };

  const handleRemoveImage = (type: "logo" | "favicon") => {
    if (type === "logo") {
      if (logoPreviewUrl && logoPreviewUrl.startsWith("blob:")) {
        URL.revokeObjectURL(logoPreviewUrl);
      }
      setLogoFile(null);
      setLogoPreviewUrl(null);
      if (logoInputRef.current) logoInputRef.current.value = "";
    } else {
      if (faviconPreviewUrl && faviconPreviewUrl.startsWith("blob:")) {
        URL.revokeObjectURL(faviconPreviewUrl);
      }
      setFaviconFile(null);
      setFaviconPreviewUrl(null);
      if (faviconInputRef.current) faviconInputRef.current.value = "";
    }
  };

  // Form Submission with Optimistic Concurrency
  const onSubmit = async (data: BrandingFormData) => {
    if (!contrastAnalysis.isValid) {
      toast.error(contrastAnalysis.errorMessage || "Primary color does not meet WCAG AA contrast standards.");
      return;
    }

    setIsSaving(true);
    try {
      // 1. Upload Logo if changed
      if (logoFile) {
        await uploadAdminLogo(logoFile);
      }

      // 2. Upload Favicon if changed
      if (faviconFile) {
        await uploadAdminFavicon(faviconFile);
      }

      // 3. Update Tokens and Copy with Optimistic Concurrency
      const updatePayload = {
        display_name: data.display_name,
        tagline: data.tagline || null,
        footer_text: data.footer_text || null,
        support_email: data.support_email ? data.support_email : null,
        tokens: {
          primary: data.primary,
          secondary: data.secondary,
          accent: data.accent,
          radius: data.radius,
          font_family: data.font_family,
        },
        expected_version: currentBranding?.version,
      };

      const updated = await updateAdminBranding(updatePayload);
      setCurrentBranding(updated);
      setLogoFile(null);
      setFaviconFile(null);

      // Refresh application-wide context and local storage immediately
      await refreshBranding();

      toast.success("Branding and theme configuration updated successfully.");
      reset(data);
    } catch (err: unknown) {
      const axiosErr = err as {
        response?: {
          status?: number;
          data?: { detail?: { code?: string; message?: string } | string };
        };
      };
      const status = axiosErr.response?.status;
      const detail = axiosErr.response?.data?.detail;

      if (status === 409) {
        toast.error(
          "Branding was modified by another administrator. Please refresh the page.",
          "Concurrency Conflict"
        );
        loadBranding();
      } else {
        const msg =
          typeof detail === "object" && detail?.message
            ? detail.message
            : typeof detail === "string"
              ? detail
              : "Failed to update branding settings.";
        toast.error(msg);
      }
    } finally {
      setIsSaving(false);
    }
  };

  // Reset to Defaults Action
  const handleReset = async () => {
    setIsSaving(true);
    try {
      const resetResp = await resetAdminBranding();
      setCurrentBranding(resetResp);
      setLogoFile(null);
      setFaviconFile(null);
      setLogoPreviewUrl(null);
      setFaviconPreviewUrl(null);

      reset({
        display_name: resetResp.display_name || "",
        tagline: resetResp.tagline || "",
        footer_text: resetResp.footer_text || "",
        support_email: resetResp.support_email || "",
        primary: resetResp.tokens.primary,
        secondary: resetResp.tokens.secondary,
        accent: resetResp.tokens.accent,
        radius: resetResp.tokens.radius,
        font_family: resetResp.tokens.font_family,
      });

      await refreshBranding();
      setShowResetConfirm(false);
      toast.success("Branding reset to system defaults.");
    } catch {
      toast.error("Failed to reset branding to defaults.");
    } finally {
      setIsSaving(false);
    }
  };

  const hasUnsavedChanges = isDirty || !!logoFile || !!faviconFile;

  if (isLoading) {
    return (
      <AppShell>
        <div className="space-y-6 max-w-7xl mx-auto py-4">
          <Skeleton className="h-10 w-64" />
          <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
            <div className="lg:col-span-7 space-y-6">
              <Skeleton className="h-48 w-full" />
              <Skeleton className="h-64 w-full" />
            </div>
            <div className="lg:col-span-5">
              <Skeleton className="h-96 w-full" />
            </div>
          </div>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <div className="max-w-7xl mx-auto space-y-6 pb-12">
        {/* Page Header */}
        <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4 border-b border-border pb-5">
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-text">
              Branding & Appearance
            </h1>
            <p className="text-sm text-text-muted mt-1">
              Customize your institution's theme tokens, typography, brand assets, and public copy.
            </p>
          </div>

          <div className="flex items-center space-x-3">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setShowResetConfirm(true)}
              disabled={isSaving}
              className="text-text-muted hover:text-text"
            >
              <RefreshCw className="w-4 h-4 mr-1.5" />
              Reset to Defaults
            </Button>

            <Button
              type="button"
              variant="primary"
              size="sm"
              onClick={handleSubmit(onSubmit)}
              isLoading={isSaving}
              disabled={isSaving || !hasUnsavedChanges || !contrastAnalysis.isValid}
            >
              <Save className="w-4 h-4 mr-1.5" />
              {hasUnsavedChanges ? "Save Changes" : "Saved"}
            </Button>
          </div>
        </div>

        {/* Main Content: Form (Left) & Live Preview (Right) */}
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
          {/* Left Column: Configuration Form */}
          <form onSubmit={handleSubmit(onSubmit)} className="lg:col-span-7 space-y-6">
            {/* Contrast Warning Banner */}
            {!contrastAnalysis.isValid && (
              <Alert variant="danger" title="WCAG AA Contrast Violation">
                {contrastAnalysis.errorMessage}
                <div className="mt-2 text-xs">
                  The primary color must maintain at least 4.5:1 text contrast and 3.0:1 UI contrast against both light and dark theme surfaces.
                </div>
              </Alert>
            )}

            {/* 1. Identity & Copy */}
            <Card className="p-6 space-y-4">
              <div className="flex items-center space-x-2 border-b border-border pb-3">
                <Layout className="w-5 h-5 text-primary" />
                <h2 className="text-base font-semibold text-text">Brand Identity & Copy</h2>
              </div>

              <div className="space-y-4 pt-1">
                <Input
                  label="Organization / Display Name *"
                  placeholder="e.g. Acme Academy"
                  error={errors.display_name?.message}
                  {...register("display_name")}
                />

                <Input
                  label="Tagline"
                  placeholder="e.g. Academic Workspace & AI Learning Platform"
                  error={errors.tagline?.message}
                  {...register("tagline")}
                />

                <Input
                  label="Footer Copyright Text"
                  placeholder="e.g. © 2026 Acme Academy. All rights reserved."
                  error={errors.footer_text?.message}
                  {...register("footer_text")}
                />

                <Input
                  label="Support Contact Email"
                  type="email"
                  placeholder="support@institution.edu"
                  error={errors.support_email?.message}
                  {...register("support_email")}
                />
              </div>
            </Card>

            {/* 2. Color Palette & WCAG AA Validation */}
            <Card className="p-6 space-y-4">
              <div className="flex items-center justify-between border-b border-border pb-3">
                <div className="flex items-center space-x-2">
                  <Palette className="w-5 h-5 text-primary" />
                  <h2 className="text-base font-semibold text-text">Theme Colors</h2>
                </div>
                {contrastAnalysis.isValid ? (
                  <Badge variant="success" size="sm" className="flex items-center space-x-1">
                    <CheckCircle2 className="w-3.5 h-3.5 mr-1" />
                    WCAG AA Pass
                  </Badge>
                ) : (
                  <Badge variant="danger" size="sm" className="flex items-center space-x-1">
                    <AlertCircle className="w-3.5 h-3.5 mr-1" />
                    Contrast Fail
                  </Badge>
                )}
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-3 gap-4 pt-1">
                {/* Primary Color */}
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-text">Primary Brand Color *</label>
                  <div className="flex items-center space-x-2">
                    <input
                      type="color"
                      value={isValidHex(watchedPrimary) ? watchedPrimary : "#2563eb"}
                      onChange={(e) => setValue("primary", e.target.value, { shouldDirty: true })}
                      className="w-10 h-10 p-0 border border-border rounded-md cursor-pointer bg-surface shrink-0"
                      aria-label="Pick primary color"
                    />
                    <Input
                      placeholder="#2563eb"
                      error={errors.primary?.message}
                      className="font-mono text-xs uppercase"
                      {...register("primary")}
                    />
                  </div>
                </div>

                {/* Secondary Color */}
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-text">Secondary Accent</label>
                  <div className="flex items-center space-x-2">
                    <input
                      type="color"
                      value={isValidHex(watchedSecondary) ? watchedSecondary : "#475569"}
                      onChange={(e) => setValue("secondary", e.target.value, { shouldDirty: true })}
                      className="w-10 h-10 p-0 border border-border rounded-md cursor-pointer bg-surface shrink-0"
                      aria-label="Pick secondary color"
                    />
                    <Input
                      placeholder="#475569"
                      error={errors.secondary?.message}
                      className="font-mono text-xs uppercase"
                      {...register("secondary")}
                    />
                  </div>
                </div>

                {/* Accent Color */}
                <div className="space-y-1.5">
                  <label className="text-xs font-medium text-text">Highlight Accent</label>
                  <div className="flex items-center space-x-2">
                    <input
                      type="color"
                      value={isValidHex(watchedAccent) ? watchedAccent : "#06b6d4"}
                      onChange={(e) => setValue("accent", e.target.value, { shouldDirty: true })}
                      className="w-10 h-10 p-0 border border-border rounded-md cursor-pointer bg-surface shrink-0"
                      aria-label="Pick accent color"
                    />
                    <Input
                      placeholder="#06b6d4"
                      error={errors.accent?.message}
                      className="font-mono text-xs uppercase"
                      {...register("accent")}
                    />
                  </div>
                </div>
              </div>

              {/* Contrast Metrics Detail */}
              <div className="bg-surface-muted border border-border rounded-lg p-3 text-xs space-y-2 mt-2">
                <span className="font-semibold text-text">Primary Color Contrast Analysis:</span>
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-text-muted">
                  <div className="flex flex-col">
                    <span>White Text</span>
                    <span className="font-medium text-text">
                      {contrastAnalysis.whiteContrast.toFixed(2)}:1
                    </span>
                  </div>
                  <div className="flex flex-col">
                    <span>Dark Text</span>
                    <span className="font-medium text-text">
                      {contrastAnalysis.darkTextContrast.toFixed(2)}:1
                    </span>
                  </div>
                  <div className="flex flex-col">
                    <span>Light Surface (#FFF)</span>
                    <span className="font-medium text-text">
                      {contrastAnalysis.lightSurfaceContrast.toFixed(2)}:1
                    </span>
                  </div>
                  <div className="flex flex-col">
                    <span>Dark Surface (#0F172A)</span>
                    <span className="font-medium text-text">
                      {contrastAnalysis.darkSurfaceContrast.toFixed(2)}:1
                    </span>
                  </div>
                </div>
              </div>
            </Card>

            {/* 3. Typography & Radii */}
            <Card className="p-6 space-y-4">
              <div className="flex items-center space-x-2 border-b border-border pb-3">
                <Type className="w-5 h-5 text-primary" />
                <h2 className="text-base font-semibold text-text">Shape & Typography</h2>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 pt-1">
                <Select
                  label="Corner Radius"
                  value={watchedRadius}
                  onChange={(e) => setValue("radius", e.target.value as RadiusToken, { shouldDirty: true })}
                  options={[
                    { value: "none", label: "Sharp (0px)" },
                    { value: "sm", label: "Subtle (2px - 8px)" },
                    { value: "md", label: "Standard (4px - 12px)" },
                    { value: "lg", label: "Rounded (6px - 16px)" },
                    { value: "xl", label: "Extra Rounded (8px - 20px)" },
                    { value: "full", label: "Pill / Full" },
                  ]}
                  error={errors.radius?.message}
                />

                <Select
                  label="Font Stack (System Fonts Only)"
                  value={watchedFontFamily}
                  onChange={(e) => setValue("font_family", e.target.value as FontFamilyToken, { shouldDirty: true })}
                  options={[
                    { value: "system-ui", label: "System UI (San Francisco / Segoe UI / Roboto)" },
                    { value: "sans", label: "Modern Sans-Serif" },
                    { value: "serif", label: "Academic Serif (Georgia / Cambria / Times)" },
                    { value: "mono", label: "Monospace (SF Mono / Consolas / Menlo)" },
                  ]}
                  error={errors.font_family?.message}
                />
              </div>
            </Card>

            {/* 4. Brand Assets: Logo & Favicon */}
            <Card className="p-6 space-y-4">
              <div className="flex items-center space-x-2 border-b border-border pb-3">
                <ImageIcon className="w-5 h-5 text-primary" />
                <h2 className="text-base font-semibold text-text">Brand Assets</h2>
              </div>

              <div className="grid grid-cols-1 sm:grid-cols-2 gap-6 pt-1">
                {/* Logo Upload */}
                <div className="space-y-3">
                  <label className="text-xs font-semibold text-text">Header & Login Logo</label>
                  <p className="text-xs text-text-muted">
                    PNG, JPEG, or WebP. Max 1 MB, up to 2000x2000 px.
                  </p>

                  <div className="border border-dashed border-border rounded-lg p-4 flex flex-col items-center justify-center space-y-3 bg-surface-muted/50 min-h-[140px]">
                    {logoPreviewUrl ? (
                      <div className="flex flex-col items-center space-y-2">
                        <img
                          src={logoPreviewUrl}
                          alt="Logo Preview"
                          className="h-12 max-w-[180px] object-contain"
                        />
                        <button
                          type="button"
                          onClick={() => handleRemoveImage("logo")}
                          className="text-xs text-danger hover:underline flex items-center space-x-1 cursor-pointer"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                          <span>Remove</span>
                        </button>
                      </div>
                    ) : (
                      <div className="text-center text-text-subtle text-xs">
                        No custom logo uploaded
                      </div>
                    )}

                    <input
                      type="file"
                      ref={logoInputRef}
                      accept="image/png,image/jpeg,image/webp"
                      onChange={(e) => handleImageSelect(e, "logo")}
                      className="hidden"
                    />

                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => logoInputRef.current?.click()}
                      className="text-xs"
                    >
                      <Upload className="w-3.5 h-3.5 mr-1" />
                      Choose Logo
                    </Button>
                  </div>
                </div>

                {/* Favicon Upload */}
                <div className="space-y-3">
                  <label className="text-xs font-semibold text-text">Browser Favicon</label>
                  <p className="text-xs text-text-muted">
                    PNG, JPEG, or WebP. Max 1 MB, square recommended (&le; 512x512 px).
                  </p>

                  <div className="border border-dashed border-border rounded-lg p-4 flex flex-col items-center justify-center space-y-3 bg-surface-muted/50 min-h-[140px]">
                    {faviconPreviewUrl ? (
                      <div className="flex flex-col items-center space-y-2">
                        <img
                          src={faviconPreviewUrl}
                          alt="Favicon Preview"
                          className="w-8 h-8 object-contain rounded"
                        />
                        <button
                          type="button"
                          onClick={() => handleRemoveImage("favicon")}
                          className="text-xs text-danger hover:underline flex items-center space-x-1 cursor-pointer"
                        >
                          <Trash2 className="w-3.5 h-3.5" />
                          <span>Remove</span>
                        </button>
                      </div>
                    ) : (
                      <div className="text-center text-text-subtle text-xs">
                        Default platform icon
                      </div>
                    )}

                    <input
                      type="file"
                      ref={faviconInputRef}
                      accept="image/png,image/jpeg,image/webp"
                      onChange={(e) => handleImageSelect(e, "favicon")}
                      className="hidden"
                    />

                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => faviconInputRef.current?.click()}
                      className="text-xs"
                    >
                      <Upload className="w-3.5 h-3.5 mr-1" />
                      Choose Favicon
                    </Button>
                  </div>
                </div>
              </div>
            </Card>
          </form>

          {/* Right Column: Live Interactive Preview */}
          <div className="lg:col-span-5 sticky top-20 space-y-4">
            <div className="flex items-center justify-between">
              <div>
                <h2 className="text-base font-semibold text-text">Live Component Preview</h2>
                <p className="text-xs text-text-muted">
                  Interactive simulation rendered strictly using draft tokens.
                </p>
              </div>

              {/* Light / Dark Preview Switch */}
              <button
                type="button"
                onClick={() => setPreviewDark((prev) => !prev)}
                className="flex items-center space-x-1.5 px-2.5 py-1 text-xs rounded-md border border-border bg-surface hover:bg-surface-hover text-text cursor-pointer transition-colors"
                title="Toggle preview theme"
              >
                {previewDark ? (
                  <>
                    <Sun className="w-3.5 h-3.5 text-warning" />
                    <span>Light Preview</span>
                  </>
                ) : (
                  <>
                    <Moon className="w-3.5 h-3.5 text-primary" />
                    <span>Dark Preview</span>
                  </>
                )}
              </button>
            </div>

            {/* Scoped Preview Container */}
            <div
              className={`branding-preview-scope ${previewDark ? "preview-dark bg-[#090d16] text-[#f8fafc]" : "bg-[#f8fafc] text-[#0f172a]"
                } border border-border rounded-xl p-5 shadow-sm space-y-6 transition-colors`}
            >
              {/* Mock 1: Brand Navigation Bar */}
              <div className="border border-border rounded-lg bg-surface p-3 flex items-center justify-between">
                <div className="flex items-center space-x-2.5">
                  {logoPreviewUrl ? (
                    <img
                      src={logoPreviewUrl}
                      alt="Preview Logo"
                      className="w-7 h-7 object-contain rounded"
                    />
                  ) : (
                    <div className="w-7 h-7 rounded bg-primary text-primary-foreground flex items-center justify-center font-bold text-xs">
                      {watchedDisplayName ? watchedDisplayName.charAt(0).toUpperCase() : "R"}
                    </div>
                  )}
                  <div className="flex flex-col">
                    <span className="font-bold text-xs leading-none">
                      {watchedDisplayName || "Academic Portal"}
                    </span>
                    <span className="text-[10px] text-text-muted mt-0.5">
                      {watchedTagline || "Workspace"}
                    </span>
                  </div>
                </div>

                <div className="flex items-center space-x-2 text-xs">
                  <span className="px-2 py-0.5 rounded bg-primary-light text-primary font-medium text-[10px]">
                    Active Tenant
                  </span>
                </div>
              </div>

              {/* Mock 2: Login Card Simulation */}
              <div className="border border-border rounded-lg bg-surface p-4 space-y-3">
                <div className="text-center space-y-1">
                  <span className="font-bold text-sm block">Sign in to your account</span>
                  <span className="text-[11px] text-text-muted block">
                    {watchedTagline || "Access your organization's learning workspace"}
                  </span>
                </div>

                <div className="space-y-2">
                  <div className="h-8 border border-border rounded px-2.5 flex items-center text-xs text-text-subtle bg-surface">
                    name@institution.edu
                  </div>
                  <div className="h-8 border border-border rounded px-2.5 flex items-center text-xs text-text-subtle bg-surface">
                    ••••••••
                  </div>
                  <button
                    type="button"
                    className="w-full h-8 rounded bg-primary text-primary-foreground hover:bg-primary-hover active:bg-primary-active font-medium text-xs shadow-sm transition-colors cursor-default"
                  >
                    Sign In
                  </button>
                </div>

                <div className="pt-2 border-t border-border text-center text-[10px] text-text-subtle truncate">
                  {watchedFooterText || "© 2026 RAG Platform. All rights reserved."}
                </div>
              </div>

              {/* Mock 3: Button & UI Components */}
              <div className="space-y-2">
                <span className="text-[11px] font-semibold text-text-muted uppercase tracking-wider block">
                  Buttons & Surfaces
                </span>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    className="px-3 py-1.5 rounded bg-primary text-primary-foreground hover:bg-primary-hover font-medium text-xs shadow-sm cursor-default"
                  >
                    Primary Button
                  </button>
                  <button
                    type="button"
                    className="px-3 py-1.5 rounded bg-surface hover:bg-surface-hover text-text border border-border font-medium text-xs shadow-sm cursor-default"
                  >
                    Secondary
                  </button>
                  <button
                    type="button"
                    className="px-3 py-1.5 rounded bg-danger text-white font-medium text-xs shadow-sm cursor-default"
                  >
                    Danger
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Reset Confirmation Dialog */}
        <ConfirmDialog
          isOpen={showResetConfirm}
          title="Reset Branding to System Defaults?"
          message="This action will restore all design tokens, copy, logos, and favicons to the system default settings. This will be visible to all users of this organization."
          confirmText="Reset to Defaults"
          confirmVariant="danger"
          isLoading={isSaving}
          onConfirm={handleReset}
          onClose={() => setShowResetConfirm(false)}
        />
      </div>
    </AppShell>
  );
};