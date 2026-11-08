"""
app/core/config.py
──────────────────
Centralised settings loaded from environment variables (or a .env file).
Nothing in this codebase ever reads os.environ directly — always use `settings`.
"""

from functools import lru_cache
import email_validator

# Permit local development domains (.local, .localhost, .internal) in EmailStr validation
for _d in ("local", "localhost", "internal"):
    if _d in email_validator.SPECIAL_USE_DOMAIN_NAMES:
        email_validator.SPECIAL_USE_DOMAIN_NAMES.remove(_d)

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Database ────────────────────────────────────────────────────────────
    # Transaction-mode pooler URL (used by the FastAPI app via asyncpg).
    # asyncpg requires the scheme postgresql+asyncpg://...
    DATABASE_URL: str

    # Platform (superadmin) engine connecting as app_platform role.
    PLATFORM_DATABASE_URL: str = "postgresql+asyncpg://app_platform:platform_password@localhost:5433/ragplatform"

    # Direct (non-pooled) URL used exclusively by Alembic and long-running
    # worker processes that need session-mode semantics.
    MIGRATION_DATABASE_URL: str

    # ── Redis ───────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── JWT / Auth ──────────────────────────────────────────────────────────
    SECRET_KEY: str  # Signs tenant user JWTs.
    PLATFORM_SECRET_KEY: str  # Signs platform (superadmin) JWTs — must differ.
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    # Number of failed login attempts allowed before a 15-minute lockout.
    LOGIN_THROTTLE_MAX_ATTEMPTS: int = 5
    LOGIN_THROTTLE_WINDOW_SECONDS: int = 900  # 15 minutes

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
    LOG_LEVEL: str = "INFO"
    ENVIRONMENT: str = "development"

    # ── Derived helpers ─────────────────────────────────────────────────────
    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"

    @field_validator("SECRET_KEY", "PLATFORM_SECRET_KEY", mode="before")
    @classmethod
    def _not_empty(cls, v: str) -> str:
        if not v or v.startswith("change-me"):
            raise ValueError(
                "SECRET_KEY and PLATFORM_SECRET_KEY must be set to real secrets."
            )
        return v

    @model_validator(mode="after")
    def _keys_must_differ(self) -> "Settings":
        if self.SECRET_KEY == self.PLATFORM_SECRET_KEY:
            raise ValueError("SECRET_KEY and PLATFORM_SECRET_KEY must be different.")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton Settings instance."""
    return Settings()


# Module-level convenience alias used throughout the codebase.
settings: Settings = get_settings()

