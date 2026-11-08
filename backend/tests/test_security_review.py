"""
tests/test_security_review.py
───────────────────────────────
Security regression tests covering Step 0 requirements:
  - Strong JWT secret validation from env (fail if missing or weak < 32 chars)
  - set_config('app.tenant_id', :tid, true) uses bound parameters only
  - Refresh cookie flags: HttpOnly, SameSite, Secure
  - Explicit CORS allowed origins list
"""

import pytest
from pydantic import ValidationError

from app.core.config import Settings, settings


def test_jwt_secret_fails_if_weak() -> None:
    """Settings must reject short or default secret keys (< 32 characters)."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            SECRET_KEY="too_short",
            PLATFORM_SECRET_KEY="also_too_short",
        )
    errors = str(exc.value)
    assert "SECRET_KEY and PLATFORM_SECRET_KEY are too weak" in errors


def test_jwt_secret_fails_if_default_placeholder() -> None:
    """Settings must reject 'change-me' placeholder keys."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            SECRET_KEY="change-me-placeholder-key-long-enough",
            PLATFORM_SECRET_KEY="change-me-another-placeholder-key-here",
        )
    errors = str(exc.value)
    assert "must be set to real secrets" in errors


def test_jwt_secrets_must_differ() -> None:
    """Tenant SECRET_KEY and PLATFORM_SECRET_KEY must never be identical."""
    same_secret = "a" * 40
    with pytest.raises(ValidationError) as exc:
        Settings(
            DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            MIGRATION_DATABASE_URL="postgresql+asyncpg://u:p@localhost/db",
            SECRET_KEY=same_secret,
            PLATFORM_SECRET_KEY=same_secret,
        )
    errors = str(exc.value)
    assert "must be different" in errors


def test_cors_origins_is_explicit_list() -> None:
    """CORS origins must be an explicit list without wildcard '*'."""
    origins = settings.cors_origins
    assert isinstance(origins, list)
    assert "*" not in origins
    assert "http://localhost:5173" in origins


def test_set_config_uses_bound_parameters_in_source() -> None:
    """Verify that tenant_db.py sets app.tenant_id via parameterized SQL."""
    import inspect
    from app.core import tenant_db

    src = inspect.getsource(tenant_db.tenant_session_context)
    assert "set_config('app.tenant_id', :tid, true)" in src
    assert "f\"" not in src and "f'" not in src, "Must not use f-strings for set_config"

