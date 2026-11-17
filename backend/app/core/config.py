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

    # Document ingestion worker engine, connecting as the app_worker role. That role can only
    # claim and update rows of app.ingest_jobs (see migration 0008). Optional for the API
    # process; required by the worker (step 5b). No default and no secret is stored here.
    WORKER_DATABASE_URL: str = ""

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
    UPLOAD_DIR: str = "uploads"  # PUBLIC branding media only (logos, favicons)
    # PRIVATE document store (original uploads). Must not be inside or contain UPLOAD_DIR. Nothing
    # serves this directory; documents leave only through an authenticated, streaming route.
    DOCUMENT_STORE_DIR: str = "document_store"

    # ── Document limits (design A2) ─────────────────────────────────────────
    # Platform defaults for every tenant. A superadmin can override them per tenant
    # (tenants.doc_limits), always within the hard ceilings in app/services/document_limits.py.
    DOC_MAX_FILE_BYTES: int = 10 * 1024 * 1024
    DOC_MAX_PDF_PAGES: int = 200
    DOC_MAX_PPTX_SLIDES: int = 100
    QUOTA_MAX_DOCUMENTS: int = 200
    QUOTA_MAX_STORAGE_BYTES: int = 500 * 1024 * 1024
    QUOTA_MAX_PAGES: int = 5000
    DOC_UPLOAD_RATE_USER: int = 20  # uploads per user per hour
    DOC_UPLOAD_RATE_TENANT: int = 100  # uploads per tenant per hour
    DOC_RETRY_RATE_USER: int = 10  # manual retries per user per hour
    DOC_RETRY_RATE_VERSION: int = 3  # manual retries per version per hour
    DOC_RETRY_MAX_PER_VERSION: int = 5  # manual retries of one version, in total (then retry_limit_reached)

    # ── Ingestion worker (design A3, step 5b-2) ─────────────────────────────
    INGEST_GLOBAL_CONCURRENCY: int = 2  # jobs running at once on the whole platform
    INGEST_TENANT_CONCURRENCY: int = 1  # jobs running at once per tenant
    INGEST_LEASE_SECONDS: int = 120  # job lease and slot TTL; a dead worker's job is free after this
    INGEST_HEARTBEAT_SECONDS: int = 30  # how often the lease and both slots are renewed
    INGEST_MAX_JOB_SECONDS: int = 1800  # hard cap on one attempt (slot max lifetime)
    INGEST_MAX_ATTEMPTS: int = 3  # attempts for an outage (database, Redis, storage)
    # A crashed parser child (a signal, SIGXCPU, an OOM kill, no answer) is transient but counts against this
    # smaller budget, so a file that kills its parser cannot loop through all INGEST_MAX_ATTEMPTS attempts.
    # The version then fails with processing_failed, which the user may retry by hand (rate limited).
    INGEST_MAX_CRASH_ATTEMPTS: int = 2
    INGEST_RETRY_BASE_SECONDS: int = 30  # transient retry: base * 2^(attempt-1) + jitter
    INGEST_RETRY_JITTER_SECONDS: int = 15
    INGEST_BUSY_MIN_SECONDS: int = 15  # a busy slot sends the job back for 15-45 s (attempt refunded)
    INGEST_BUSY_MAX_SECONDS: int = 45
    INGEST_SHUTDOWN_GRACE_SECONDS: int = 20  # SIGTERM: time to finish before the job is released
    INGEST_FINISHED_JOB_RETENTION_DAYS: int = 7
    WORKER_POLL_SECONDS: float = 5.0  # idle poll interval (with jitter)
    SWEEPER_INTERVAL_SECONDS: int = 60
    SWEEPER_MISSING_JOB_AFTER_SECONDS: int = 120  # a queued version without a job row after this gets one
    DOC_TOMBSTONE_RETENTION_DAYS: int = 30
    # Sandboxed document parser (app/services/document_parse_child.py). Memory and CPU: Linux only.
    DOC_PARSE_TIMEOUT_SECONDS: int = 120
    # The child's JSON answer, as UTF-8. The real maximum (2,000,000 characters of 4 bytes in 100,000 blocks)
    # is parsed_document.MAX_ANSWER_BYTES = 12.4 MiB; 14 MiB leaves a margin. Raise it only with
    # DOC_MAX_TEXT_CHARS (the 2,000,000 character cap is also a hard ceiling in the parent).
    DOC_PARSE_MAX_OUTPUT_BYTES: int = 14 * 1024 * 1024
    DOC_PARSE_MEMORY_LIMIT_MB: int = 1024  # 768 on 2 GB hosts
    DOC_PARSE_CPU_SECONDS: int = 120
    DOC_MAX_TEXT_CHARS: int = 2_000_000  # extracted characters per version (text_too_large); hard ceiling too

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

