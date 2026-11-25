"""
app/core/config.py
──────────────────
Centralised settings loaded from environment variables (or a .env file).
Nothing in this codebase ever reads os.environ directly — always use `settings`.
"""

from functools import lru_cache

import email_validator
from pydantic import Field, SecretStr, field_validator, model_validator
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
    # Test search (POST /api/v1/documents/test-search, step 5d-2, design A5): searches per user per window
    DOC_TEST_SEARCH_RATE_USER: int = Field(default=60, ge=1, le=10_000)
    DOC_TEST_SEARCH_RATE_WINDOW_SECONDS: int = Field(default=600, ge=1, le=86_400)

    # ── Ingestion worker (design A3, step 5b-2) ─────────────────────────────
    INGEST_GLOBAL_CONCURRENCY: int = 2  # jobs running at once on the whole platform
    INGEST_TENANT_CONCURRENCY: int = 1  # jobs running at once per tenant
    # Timing for a 1,000-chunk upload (about 5 minutes of embedding, measured at 2.7-3.5 texts/s with the real
    # e5-small): the lease is renewed every INGEST_HEARTBEAT_SECONDS and after every embedding batch, so it only has
    # to outlast a few missed renewals; a dead worker's job is free again after INGEST_LEASE_SECONDS. The cap on
    # one attempt covers the 2,000,000 character ceiling (about 4,500 Hindi chunks, about 30 minutes) with a margin.
    INGEST_LEASE_SECONDS: int = 180
    INGEST_HEARTBEAT_SECONDS: int = 30  # how often the lease and both slots are renewed
    INGEST_MAX_JOB_SECONDS: int = 3600  # hard cap on one attempt (slot max lifetime)
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

    # ── Chunking, publishing and archiving (step 5c-2, design A2 and A3) ─────
    # Token counts come from the embedder's own tokenizer. The e5-small limit of 512 includes the 'passage: ' prefix,
    # two special tokens and the heading path, which is why the content maximum is 460 (see app/services/chunker.py).
    CHUNK_TARGET_TOKENS: int = Field(default=380, ge=1)
    CHUNK_MAX_TOKENS: int = Field(default=460, ge=1)
    CHUNK_OVERLAP_TOKENS: int = Field(default=60, ge=0)
    # A ready version without injection flags is published by the worker itself. The scanner (5d-2) now writes
    # document_versions.flagged_chunk_count, but this stays OFF by default (plan Q5): the scanner is a tripwire, so
    # "nothing flagged" only means "nothing detected"; its false-positive and false-negative rates on real school
    # files are not measured yet (5d-3); and concurrent deletes during the lock upgrade are unmeasured (B76).
    # Versions indexed before 5d-2 (flagged_chunk_count NULL) are never auto-published and need acknowledgement.
    DOC_AUTO_PUBLISH_ENABLED: bool = False
    DOC_ARCHIVE_FILE_RETENTION_DAYS: int = Field(default=30, ge=0)  # an archived version's file waits this long
    DOC_MAX_ARCHIVED_VERSIONS: int = Field(default=5, ge=0)  # older archived versions are pruned

    # ── Embedding model (app/ml, step 5c-1) ─────────────────────────────────
    # Local, verified model files only: nothing is ever downloaded at runtime (python -m app.scripts.fetch_model
    # is the one operator command that does). Empty means "not configured": the embedder refuses to load.
    EMBEDDING_MODEL_DIR: str = ""  # holds <dir>/<model name>/<files of models.lock.json>
    EMBEDDING_MODEL_ID: str = "e5-small"  # a name from app/ml/models.lock.json (or its repo@revision key)
    # Texts per model run. Measured (laptop, real e5-small, one thread): batch 1 = 3.5 texts/s at 1,094 MB peak, 8 = 3.2
    # at 1,369 MB, 16 = 2.7 at 1,447 MB, 32 = 3.0 at 1,818 MB: a bigger batch is not faster and costs memory.
    EMBEDDING_BATCH_SIZE: int = Field(default=8, ge=1, le=256)
    EMBEDDING_THREADS: int = Field(default=1, ge=1, le=64)  # onnxruntime intra-op threads
    # Load the query model when the app starts (B96: the first search after a restart takes seconds otherwise).
    # Unset: off in development, on in every other environment. A load failure is logged and does not stop the app.
    # Read it through `embedding_warm_at_startup`.
    EMBEDDING_WARM_AT_STARTUP: bool | None = None

    # ── Retrieval (step 5d, design A5, docs/phase5d-plan.md section 8) ──────
    RETRIEVAL_VECTOR_K: int = Field(default=20, ge=1, le=200)  # chunks taken from the vector side
    RETRIEVAL_KEYWORD_K: int = Field(default=20, ge=1, le=200)  # chunks taken from the keyword side
    RETRIEVAL_FINAL_K: int = Field(default=6, ge=1, le=50)  # chunks kept after fusion
    RETRIEVAL_RRF_K: int = Field(default=60, ge=1, le=1000)  # reciprocal rank fusion constant
    RETRIEVAL_MAX_QUERY_CHARS: int = Field(default=500, ge=1, le=4000)  # measured after cleaning
    # Cosine similarity of the best vector hit needed to call the material sufficient. CALIBRATED in step 5d-5
    # (2026-11-20) for RETRIEVAL_CALIBRATED_MODEL_KEY on a golden set of 30 should-answer / 10 should-not-answer
    # questions: 0.84 answers 97% and refuses 70% (docs/api/phase5.md section 12). It belongs to that model: another
    # model needs a new sweep, and until then every response says threshold_calibrated = false.
    RETRIEVAL_MIN_SIMILARITY: float = Field(default=0.84, ge=0.0, le=1.0)
    # The embedding model RETRIEVAL_MIN_SIMILARITY was measured for. threshold_calibrated is true only when the
    # active model key equals this one exactly; empty means no model is calibrated.
    RETRIEVAL_CALIBRATED_MODEL_KEY: str = "intfloat/multilingual-e5-small@614241f622f53c4eeff9890bdc4f31cfecc418b3"
    # Share of the query's words found in one keyword hit that also counts as sufficient. 0 switches it off.
    # Above 0 it needs the keyword side (RETRIEVAL_RRF_KEYWORD_WEIGHT above 0), or nothing would measure it.
    RETRIEVAL_MIN_KEYWORD_COVERAGE: float = Field(default=0.0, ge=0.0, le=1.0)
    # Up to this many candidate chunks the vector side is an exact scan; above it the HNSW index is used.
    RETRIEVAL_EXACT_MAX_CANDIDATES: int = Field(default=50_000, ge=0)
    RETRIEVAL_HNSW_EF_SEARCH: int = Field(default=100, ge=1, le=1000)  # must be >= RETRIEVAL_VECTOR_K
    # statement_timeout for every statement of one search (transaction-local; over it: 503 search_timeout)
    RETRIEVAL_STATEMENT_TIMEOUT_MS: int = Field(default=5000, ge=1, le=600_000)
    # The keyword side ranks at most this many matching chunks (the lowest ids); must be >= RETRIEVAL_KEYWORD_K
    RETRIEVAL_KEYWORD_MAX_CANDIDATES: int = Field(default=2000, ge=1, le=1_000_000)
    # Step 5d-4 (found by the first real evaluation: the keyword side added noise); step 5d-5 switched the keyword
    # side OFF by default, because hybrid lost to vector only on the golden set (hit@1 53-73% against 83%). The
    # keyword code stays, switched off, for later (exact-code search); `eval_retrieval --compare` still tries it.
    # Keyword words that are in MORE than this share of the candidate chunks are left out of the keyword query
    # (language independent, no stopword list). 1.0 = off. If every word is left out, only the vector side runs.
    RETRIEVAL_KEYWORD_MAX_DF: float = Field(default=1.0, ge=0.0, le=1.0)
    # Reciprocal rank fusion: score = sum of weight / (RETRIEVAL_RRF_K + rank) over the two sides. The vector
    # weight must stay above 0 (the sufficiency decision reads the vector side). Keyword weight 0 (the default) =
    # vector only: the keyword statement is then not run at all. 1.0 / 1.0 is the hybrid of 5d-1 to 5d-4.
    RETRIEVAL_RRF_VECTOR_WEIGHT: float = Field(default=1.0, gt=0.0, le=100.0)
    RETRIEVAL_RRF_KEYWORD_WEIGHT: float = Field(default=0.0, ge=0.0, le=100.0)
    # A chunk is ranked by the keyword side only if it matches at least this many of the (pruned) query words.
    RETRIEVAL_KEYWORD_MIN_MATCHED_TERMS: int = Field(default=1, ge=1, le=64)
    # Query embeddings running at once in one API process (a semaphore, and the size of their own thread pool).
    # A search over the cap waits up to RETRIEVAL_EMBED_QUEUE_SECONDS for a place, then gets 503 search_busy.
    RETRIEVAL_EMBED_CONCURRENCY: int = Field(default=2, ge=1, le=64)
    RETRIEVAL_EMBED_QUEUE_SECONDS: float = Field(default=5.0, ge=0.0, le=60.0)

    # ── LLM client (step 6a-2, docs/phase6-plan.md 7.2, 7.4, 7.6) ──────────────────────────────────────────
    # Comma-separated model keys ("provider:model-id") tried in order for answers. Every key must be in
    # app/llm/models.json (checked at start-up by app/llm/policy.py). Empty = no provider is ever called and
    # chat gives retrieval-only answers. Read it through `llm_answer_chain`.
    LLM_ANSWER_CHAIN: str = ""
    # Step 6a-4: the same for the condense call (rewriting a follow-up into a standalone question). Every key must
    # be a "condense" model in models.json. Empty = no condense: the caller uses the raw question (not an error).
    # Read it through `llm_condense_chain`.
    LLM_CONDENSE_CHAIN: str = ""
    # The operator checked the Groq console and zero data retention is on. Groq models outside development
    # refuse to start until this is true (owner action, step 6a-6).
    LLM_ZDR_CONFIRMED: bool = False
    # A model that trains on inputs (Gemini free tier) starts only when this is true AND ENVIRONMENT=development.
    LLM_ALLOW_TRAINING_PROVIDERS: bool = False
    # Timeouts in seconds (plan 7.4): TCP + TLS, request sent to first token, gap between tokens, whole answer.
    LLM_TIMEOUT_CONNECT: float = Field(default=5.0, gt=0, le=600)
    LLM_TIMEOUT_FIRST_TOKEN: float = Field(default=10.0, gt=0, le=600)
    LLM_TIMEOUT_IDLE: float = Field(default=15.0, gt=0, le=600)
    LLM_TIMEOUT_TOTAL: float = Field(default=45.0, gt=0, le=600)
    # Output tokens asked of the provider for one chat answer (plan 5.6).
    CHAT_MAX_OUTPUT_TOKENS: int = Field(default=600, ge=1, le=4096)
    # ── Daily token budgets (step 6a-5, plan 7.7), counted in Redis per UTC day ──
    # PLACEHOLDERS, set after Q1: both numbers come from the first draft and are not decisions. They are fixed
    # only after the owner answers Q1 (paid tier and answer model) and the 6a-6 token measurement. Do not
    # change them in code: set them in the environment.
    # Tokens one tenant may use per UTC day when its tenant_settings.chat_daily_token_budget is NULL (set after Q1).
    LLM_TENANT_DAILY_TOKENS: int = Field(default=300_000, ge=1, le=1_000_000_000)
    # Tokens all tenants together may use per UTC day; also the ceiling of a tenant override (set after Q1).
    LLM_PLATFORM_DAILY_TOKENS: int = Field(default=2_000_000, ge=1, le=10_000_000_000)
    # Token estimate when a provider reports no usage: characters per token. UNVERIFIED ratios (design B1):
    # step 6a-6 measures them against Groq's reported usage and sets them here, with no code change.
    LLM_ESTIMATE_CHARS_PER_TOKEN_LATIN: float = Field(default=3.0, gt=0, le=20)
    LLM_ESTIMATE_CHARS_PER_TOKEN_DEVANAGARI: float = Field(default=2.0, gt=0, le=20)
    # Provider keys: from the environment or a secret manager only, never in a file of the repository.
    # SecretStr keeps them out of repr(), str(), model_dump() and validation errors.
    GROQ_API_KEY: SecretStr = SecretStr("")
    OPENROUTER_API_KEY: SecretStr = SecretStr("")
    GEMINI_API_KEY: SecretStr = SecretStr("")

    # ── Derived helpers ─────────────────────────────────────────────────────
    @property
    def is_development(self) -> bool:
        return self.ENVIRONMENT.lower() == "development"

    @property
    def embedding_warm_at_startup(self) -> bool:
        """EMBEDDING_WARM_AT_STARTUP when set, else on outside development."""
        if self.EMBEDDING_WARM_AT_STARTUP is None:
            return not self.is_development
        return self.EMBEDDING_WARM_AT_STARTUP

    @property
    def llm_answer_chain(self) -> tuple[str, ...]:
        """LLM_ANSWER_CHAIN as a tuple of model keys (blank entries dropped, order kept)."""
        return tuple(part.strip() for part in self.LLM_ANSWER_CHAIN.split(",") if part.strip())

    @property
    def llm_condense_chain(self) -> tuple[str, ...]:
        """LLM_CONDENSE_CHAIN as a tuple of model keys (blank entries dropped, order kept)."""
        return tuple(part.strip() for part in self.LLM_CONDENSE_CHAIN.split(",") if part.strip())

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
    def _ingest_settings_must_fit_together(self) -> "Settings":
        if self.INGEST_LEASE_SECONDS < 4 * self.INGEST_HEARTBEAT_SECONDS:
            raise ValueError("INGEST_LEASE_SECONDS must be at least four times INGEST_HEARTBEAT_SECONDS")
        if self.INGEST_MAX_JOB_SECONDS < self.INGEST_LEASE_SECONDS:
            raise ValueError("INGEST_MAX_JOB_SECONDS must not be below INGEST_LEASE_SECONDS")
        from app.services.chunker import (
            ChunkingConfig,  # validates the token budget (raises ValueError)
        )

        ChunkingConfig(self.CHUNK_TARGET_TOKENS, self.CHUNK_MAX_TOKENS, self.CHUNK_OVERLAP_TOKENS)
        return self

    @model_validator(mode="after")
    def _retrieval_settings_must_fit_together(self) -> "Settings":
        # Kept here and in RetrievalConfig: retrieval.py imports this module, so it cannot be imported here
        if self.RETRIEVAL_HNSW_EF_SEARCH < self.RETRIEVAL_VECTOR_K:
            raise ValueError("RETRIEVAL_HNSW_EF_SEARCH must be at least RETRIEVAL_VECTOR_K")
        if self.RETRIEVAL_KEYWORD_MAX_CANDIDATES < self.RETRIEVAL_KEYWORD_K:
            raise ValueError("RETRIEVAL_KEYWORD_MAX_CANDIDATES must be at least RETRIEVAL_KEYWORD_K")
        if self.RETRIEVAL_MIN_KEYWORD_COVERAGE > 0 and self.RETRIEVAL_RRF_KEYWORD_WEIGHT == 0:
            raise ValueError(
                "RETRIEVAL_MIN_KEYWORD_COVERAGE above 0 needs the keyword side: set RETRIEVAL_RRF_KEYWORD_WEIGHT above 0"
            )
        return self

    @model_validator(mode="after")
    def _llm_timeouts_must_fit_together(self) -> "Settings":
        # A stage longer than the whole answer could never fire (plan 7.4)
        if self.LLM_TIMEOUT_FIRST_TOKEN > self.LLM_TIMEOUT_TOTAL:
            raise ValueError("LLM_TIMEOUT_FIRST_TOKEN must not be above LLM_TIMEOUT_TOTAL")
        if self.LLM_TIMEOUT_IDLE > self.LLM_TIMEOUT_TOTAL:
            raise ValueError("LLM_TIMEOUT_IDLE must not be above LLM_TIMEOUT_TOTAL")
        return self

    @model_validator(mode="after")
    def _token_budgets_must_fit_together(self) -> "Settings":
        # A tenant default above the whole platform could never be used (plan 7.7)
        if self.LLM_TENANT_DAILY_TOKENS > self.LLM_PLATFORM_DAILY_TOKENS:
            raise ValueError("LLM_TENANT_DAILY_TOKENS must not be above LLM_PLATFORM_DAILY_TOKENS")
        return self

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

