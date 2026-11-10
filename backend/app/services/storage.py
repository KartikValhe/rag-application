"""
app/services/storage.py
───────────────────────
Object storage abstraction and local filesystem implementation.
Provides raster image validation, sanitization, and metadata stripping using Pillow.
"""

import io
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException, status
from PIL import Image

from app.core.config import settings

MAX_IMAGE_FILE_SIZE = 1 * 1024 * 1024  # 1 MB
MAX_LOGO_DIMENSIONS = (1024, 1024)
MAX_FAVICON_DIMENSIONS = (256, 256)
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}


class ObjectStore(Protocol):
    """Abstract interface for object storage (local filesystem now, Supabase/S3 later)."""

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        """Store an object under the given key and return the key."""
        ...

    async def get(self, key: str) -> bytes | None:
        """Retrieve the bytes for the given key, or None if not found."""
        ...

    async def delete(self, key: str) -> None:
        """Delete the object with the given key if it exists."""
        ...

    def get_url(self, key: str) -> str:
        """Return the public URL path for the given key."""
        ...


class LocalStorage:
    """Local filesystem implementation of ObjectStore."""

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir is None:
            base_dir = Path(settings.UPLOAD_DIR)
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _resolve_path(self, key: str) -> Path:
        """
        Safely resolve a storage key to a filesystem path.
        Prevents directory traversal attacks.
        """
        # Normalize slashes and remove leading slashes
        clean_key = key.replace("\\", "/").lstrip("/")
        target_path = (self.base_dir / clean_key).resolve()

        # Enforce that target_path is within base_dir
        if not str(target_path).startswith(str(self.base_dir)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"code": "invalid_storage_key", "message": "Invalid storage path."},
            )
        return target_path

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        target_path = self._resolve_path(key)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(data)
        return key

    async def get(self, key: str) -> bytes | None:
        target_path = self._resolve_path(key)
        if target_path.exists() and target_path.is_file():
            return target_path.read_bytes()
        return None

    async def delete(self, key: str) -> None:
        target_path = self._resolve_path(key)
        if target_path.exists() and target_path.is_file():
            target_path.unlink()

    def get_url(self, key: str) -> str:
        clean_key = key.replace("\\", "/").lstrip("/")
        return f"/api/v1/public/branding/media/{clean_key}"


# Global storage instance
storage: ObjectStore = LocalStorage()


def get_storage() -> ObjectStore:
    return storage


def validate_and_sanitize_image(
    data: bytes,
    asset_type: str,
) -> tuple[bytes, str, str]:
    """
    Validate, sanitize, and strip metadata from uploaded images.

    Rules:
      1. Size limit: max 1 MB.
      2. No SVG! Strictly raster images (PNG, JPEG, WebP) identified by content sniffing.
      3. Dimension limits: Logo (1024x1024), Favicon (256x256).
      4. Strips EXIF and all embedded metadata by re-encoding onto a clean canvas.

    Returns:
      (sanitized_bytes, content_type, file_extension)
    """
    if len(data) > MAX_IMAGE_FILE_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "file_too_large", "message": "File size exceeds maximum allowed 1 MB."},
        )

    # 1. Reject SVG and XML signatures explicitly
    lower_data_head = data[:1024].lower()
    if b"<svg" in lower_data_head or b"<?xml" in lower_data_head or b"<!doctype svg" in lower_data_head:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "svg_not_allowed", "message": "SVG files are not allowed. Please upload PNG, JPEG, or WebP."},
        )

    # 2. Content sniffing using Pillow
    try:
        img_verify = Image.open(io.BytesIO(data))
        img_verify.verify()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_image", "message": "Uploaded file is not a valid image."},
        ) from None

    # Re-open after verify() to inspect properties and process pixels
    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_image", "message": "Unable to process image."},
        ) from None

    fmt = (img.format or "").upper()
    if fmt not in ALLOWED_FORMATS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "unsupported_format",
                "message": f"Unsupported image format: {fmt}. Allowed formats are PNG, JPEG, and WebP.",
            },
        )

    # 3. Dimension checks
    max_dims = MAX_LOGO_DIMENSIONS if asset_type == "logo" else MAX_FAVICON_DIMENSIONS
    if img.width > max_dims[0] or img.height > max_dims[1]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "dimensions_exceeded",
                "message": (
                    f"{asset_type.capitalize()} dimensions ({img.width}x{img.height}) "
                    f"exceed maximum allowed {max_dims[0]}x{max_dims[1]} pixels."
                ),
            },
        )

    # 4. Strip metadata and re-encode to a clean canvas
    output = io.BytesIO()
    has_alpha = img.mode in ("RGBA", "LA", "P") or (
        img.mode == "P" and "transparency" in img.info
    )

    if fmt == "PNG":
        target_mode = "RGBA" if has_alpha else "RGB"
        converted = img.convert(target_mode)
        clean_img = Image.new(target_mode, converted.size)
        clean_img.paste(converted)
        clean_img.save(output, format="PNG", optimize=True)
        content_type = "image/png"
        ext = "png"
    elif fmt == "JPEG":
        converted = img.convert("RGB")
        clean_img = Image.new("RGB", converted.size)
        clean_img.paste(converted)
        clean_img.save(output, format="JPEG", quality=90, optimize=True)
        content_type = "image/jpeg"
        ext = "jpg"
    elif fmt == "WEBP":
        target_mode = "RGBA" if has_alpha else "RGB"
        converted = img.convert(target_mode)
        clean_img = Image.new(target_mode, converted.size)
        clean_img.paste(converted)
        clean_img.save(output, format="WEBP", quality=90)
        content_type = "image/webp"
        ext = "webp"
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "unsupported_format", "message": "Unsupported image format."},
        )

    return output.getvalue(), content_type, ext

