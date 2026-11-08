"""
tests/test_throttle.py
───────────────────────
Login throttle tests (Phase 1 exit criterion).

5 failed attempts are allowed; the 6th must return HTTP 429.
After a successful login, the counter clears.
"""


import pytest
from httpx import AsyncClient

from app.core.config import settings
from app.core.redis_client import get_redis
from app.db.models.tenant import Tenant
from app.db.models.user import User

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def flush_redis() -> None:
    """Clear the Redis test DB before each test to avoid counter bleed."""
    redis = await get_redis()
    await redis.flushdb()


async def test_fifth_failed_attempt_still_returns_401(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
) -> None:
    """Attempts 1–5 should return 401 (bad credentials), not 429."""
    for _ in range(settings.LOGIN_THROTTLE_MAX_ATTEMPTS):
        resp = await client.post(
            "/api/v1/auth/login",
            json={
                "email": user_a.email,
                "password": "WrongPassword!",
                "tenant_slug": tenant_a.slug,
            },
        )
        assert resp.status_code == 401, f"Expected 401, got {resp.status_code}: {resp.text}"


async def test_sixth_failed_attempt_returns_429(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
) -> None:
    """The attempt after MAX_ATTEMPTS must return 429."""
    for _ in range(settings.LOGIN_THROTTLE_MAX_ATTEMPTS):
        await client.post(
            "/api/v1/auth/login",
            json={
                "email": user_a.email,
                "password": "WrongPassword!",
                "tenant_slug": tenant_a.slug,
            },
        )
    # 6th attempt.
    resp = await client.post(
        "/api/v1/auth/login",
        json={
            "email": user_a.email,
            "password": "WrongPassword!",
            "tenant_slug": tenant_a.slug,
        },
    )
    assert resp.status_code == 429
    assert resp.json()["detail"]["code"] == "login_throttled"
    assert "Retry-After" in resp.headers


async def test_successful_login_clears_throttle(
    client: AsyncClient,
    tenant_a: Tenant,
    user_a: User,
) -> None:
    """After a successful login, the throttle counter resets."""
    # Exhaust MAX_ATTEMPTS - 1 failures.
    for _ in range(settings.LOGIN_THROTTLE_MAX_ATTEMPTS - 1):
        await client.post(
            "/api/v1/auth/login",
            json={
                "email": user_a.email,
                "password": "WrongPassword!",
                "tenant_slug": tenant_a.slug,
            },
        )
    # Successful login.
    good = await client.post(
        "/api/v1/auth/login",
        json={
            "email": user_a.email,
            "password": "Password123!",
            "tenant_slug": tenant_a.slug,
        },
    )
    assert good.status_code == 200

    # Counter should be cleared; next bad attempt returns 401, not 429.
    bad = await client.post(
        "/api/v1/auth/login",
        json={
            "email": user_a.email,
            "password": "WrongPassword!",
            "tenant_slug": tenant_a.slug,
        },
    )
    assert bad.status_code == 401

