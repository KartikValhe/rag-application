"""
app/core/config.py
──────────────────
Centralised settings loaded from environment variables (or a .env file).
Nothing in this codebase ever reads os.environ directly — always use `settings`.
"""

from functools import lru_cache

import email_validator
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Permit local development domains (.local, .localhost, .internal) in EmailStr validation
for _d in ("local", "localhost", "internal"):
    if _d in email_validator.SPECIAL_USE_DOMAIN_NAMES:
        email_validator.SPECIAL_USE_DOMAIN_NAMES.remove(_d)

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ────────────────────────────────────────────────────────────
    # Transaction-mode pooler URL (used by the FastAPI app via asyncpg).
    # asyncpg requires the scheme postgresql+asyncpg://...
    DATABASE_URL: str = ""

    # Platform (superadmin) engine connecting as app_platform role.
    PLATFORM_DATABASE_URL: str = ""

    # Direct (non-pooled) URL used exclusively by Alembic and long-running
    # worker processes that need session-mode semantics.
    MIGRATION_DATABASE_URL: str = ""

    # ── Redis ───────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_PASSWORD: str = ""

    # ── JWT / Auth ──────────────────────────────────────────────────────────
    SECRET_KEY: str = ""  # Signs tenant user JWTs.
    PLATFORM_SECRET_KEY: str = ""  # Signs platform (superadmin) JWTs — must differ.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    # Number of failed login attempts allowed before a 15-minute lockout.
    LOGIN_THROTTLE_MAX_ATTEMPTS: int = 5
    LOGIN_THROTTLE_IP_MAX_ATTEMPTS: int = 50
    LOGIN_THROTTLE_WINDOW_SECONDS: int = 900  # 15 minutes
    TRUST_PROXY: bool = False

    # ── Email ───────────────────────────────────────────────────────────────
    MAIL_HOST: str = "localhost"
    MAIL_PORT: int = 1025
    MAIL_USERNAME: str = ""
    MAIL_PASSWORD: str = ""
    MAIL_USE_TLS: bool = False
    MAIL_FROM: str = "noreply@platform.local"
    MAIL_FROM_NAME: str = "RAG Platform"

    # ── App ─────────────────────────────────────────────────────────────────
    PLATFORM_DOMAIN: str = "platform.local"
    FRONTEND_URL: str = "http://localhost:5173"
    CORS_ALLOWED_ORIGINS: str = ""  # Comma-separated list of explicit allowed origins
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "development"
    UPLOAD_DIR: str = "uploads"

    # ── Derived helpers ─────────────────────────────────────────────────────
    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"

    @property
    def cors_origins(self) -> list[str]:
        """Return an explicit, deduplicated list of allowed CORS origins."""
        origins: list[str] = [self.FRONTEND_URL.rstrip("/")]
        if self.CORS_ALLOWED_ORIGINS:
            origins.extend(
                origin.strip().rstrip("/")
                for origin in self.CORS_ALLOWED_ORIGINS.split(",")
                if origin.strip()
            )
        if self.is_development:
            origins.extend([
                "http://localhost:5173",
                "http://127.0.0.1:5173",
            ])
        # Return unique origins preserving order
        return list(dict.fromkeys(origins))

    @field_validator(
        "DATABASE_URL",
        "PLATFORM_DATABASE_URL",
        "MIGRATION_DATABASE_URL",
        mode="before",
    )
    @classmethod
    def _validate_db_urls(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError(
                "DATABASE_URL, PLATFORM_DATABASE_URL, and MIGRATION_DATABASE_URL must be provided via environment variables."
            )
        return v

    @field_validator("SECRET_KEY", "PLATFORM_SECRET_KEY", mode="before")
    @classmethod
    def _validate_secret(cls, v: str) -> str:
        if not v or v.startswith("change-me"):
            raise ValueError(
                "SECRET_KEY and PLATFORM_SECRET_KEY must be set to real secrets via environment variables."
            )
        disallowed = {
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210",
        }
        if v.strip() in disallowed:
            raise ValueError(
                "SECRET_KEY and PLATFORM_SECRET_KEY cannot use previously committed insecure default keys."
            )
        if len(v.strip()) < 32:
            raise ValueError(
                "SECRET_KEY and PLATFORM_SECRET_KEY are too weak: must be at least 32 characters."
            )
        return v

    @model_validator(mode="after")
    def _keys_must_differ(self) -> "Settings":
        if self.SECRET_KEY == self.PLATFORM_SECRET_KEY:
            raise ValueError("SECRET_KEY and PLATFORM_SECRET_KEY must be different.")
        return self

    @model_validator(mode="after")
    def _validate_redis_security(self) -> "Settings":
        if not self.is_development:
            has_password = bool(self.REDIS_PASSWORD.strip())
            if not has_password and "@" in self.REDIS_URL:
                from urllib.parse import urlparse
                parsed = urlparse(self.REDIS_URL)
                if parsed.password:
                    has_password = True
            if not has_password:
                raise ValueError("Redis password must be configured in non-development environments.")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()


# Module-level convenience alias used throughout the codebase.
settings: Settings = get_settings()

