"""
app/schemas/branding.py
───────────────────────
Pydantic schemas and server-side WCAG AA contrast validation for white-label branding.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


def calculate_relative_luminance(hex_color: str) -> float:
    """
    Calculates the relative luminance of an sRGB color per WCAG 2.1 specs.
    Formula: L = 0.2126 * R + 0.7152 * G + 0.0722 * B
    where R, G, B are converted from sRGB to linear light values.
    """
    hex_clean = hex_color.lstrip("#")
    r, g, b = [int(hex_clean[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]

    def to_linear(channel: float) -> float:
        return (
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )

    return 0.2126 * to_linear(r) + 0.7152 * to_linear(g) + 0.0722 * to_linear(b)


def calculate_contrast_ratio(hex1: str, hex2: str) -> float:
    """
    Calculates the contrast ratio between two hex colors per WCAG 2.1:
    (Lighter + 0.05) / (Darker + 0.05)
    """
    l1 = calculate_relative_luminance(hex1)
    l2 = calculate_relative_luminance(hex2)
    lighter = max(l1, l2)
    darker = min(l1, l2)
    return (lighter + 0.05) / (darker + 0.05)


def validate_wcag_contrast(primary_hex: str) -> None:
    """
    Server-side WCAG AA contrast validation:
      1. Primary color must work with white or dark text (>= 4.5:1 text contrast).
      2. Primary color must contrast against light surface (#ffffff, >= 3.0:1 UI contrast).
      3. Primary color must contrast against dark surface (#0f172a, >= 3.0:1 UI contrast).
    """
    white = "#ffffff"
    dark_text = "#0f172a"
    dark_surface = "#0f172a"
    light_surface = "#ffffff"

    c_white = calculate_contrast_ratio(primary_hex, white)
    c_dark = calculate_contrast_ratio(primary_hex, dark_text)

    # 1. Text readability on primary (buttons, badges)
    if c_white < 4.5 and c_dark < 4.5:
        raise ValueError(
            f"Primary color {primary_hex} does not have sufficient WCAG AA contrast (minimum 4.5:1) "
            f"with either white text ({c_white:.2f}:1) or dark text ({c_dark:.2f}:1). "
            f"Please choose a slightly lighter or darker shade."
        )

    # 2. Distinguishable against light theme surface (WCAG 2.1 AA 1.4.11 UI component contrast >= 3.0:1)
    c_light_surf = calculate_contrast_ratio(primary_hex, light_surface)
    if c_light_surf < 3.0:
        raise ValueError(
            f"Primary color {primary_hex} does not have sufficient WCAG AA contrast (minimum 3.0:1) "
            f"against the light theme surface ({c_light_surf:.2f}:1). It will be difficult to see in light mode."
        )

    # 3. Distinguishable against dark theme surface (WCAG 2.1 AA 1.4.11 UI component contrast >= 3.0:1)
    c_dark_surf = calculate_contrast_ratio(primary_hex, dark_surface)
    if c_dark_surf < 3.0:
        raise ValueError(
            f"Primary color {primary_hex} does not have sufficient WCAG AA contrast (minimum 3.0:1) "
            f"against the dark theme surface ({c_dark_surf:.2f}:1). It will be difficult to see in dark mode."
        )


class DesignTokens(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: str = Field(
        default="#2563eb",
        pattern=r"^#[0-9a-fA-F]{6}$",
        description="Primary brand color (6-digit hex)",
    )
    secondary: str = Field(
        default="#475569",
        pattern=r"^#[0-9a-fA-F]{6}$",
        description="Secondary brand color (6-digit hex)",
    )
    accent: str = Field(
        default="#06b6d4",
        pattern=r"^#[0-9a-fA-F]{6}$",
        description="Accent color (6-digit hex)",
    )
    radius: Literal["none", "sm", "md", "lg", "xl", "full"] = Field(
        default="md",
        description="Border radius scale token",
    )
    font_family: Literal["system-ui", "sans", "serif", "mono"] = Field(
        default="system-ui",
        description="System font stack token (no external font downloads)",
    )

    @field_validator("primary")
    @classmethod
    def validate_primary_color(cls, v: str) -> str:
        validate_wcag_contrast(v)
        return v.lower()

    @field_validator("secondary", "accent")
    @classmethod
    def normalize_hex_colors(cls, v: str) -> str:
        return v.lower()


DEFAULT_TOKENS = DesignTokens().model_dump()


class BrandingUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=255)
    tagline: str | None = Field(default=None, max_length=255)
    footer_text: str | None = Field(default=None, max_length=512)
    support_email: EmailStr | None = Field(default=None, max_length=255)
    tokens: DesignTokens | None = None


class BrandingResponse(BaseModel):
    display_name: str
    tagline: str | None
    footer_text: str | None
    support_email: str | None
    tokens: DesignTokens
    logo_key: str | None
    logo_url: str | None
    favicon_key: str | None
    favicon_url: str | None
    version: int
    updated_at: datetime


class PublicBrandingResponse(BaseModel):
    display_name: str
    tagline: str | None
    footer_text: str | None
    support_email: str | None
    tokens: DesignTokens
    logo_url: str | None
    favicon_url: str | None
    version: int

