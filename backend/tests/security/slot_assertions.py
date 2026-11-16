"""
tests/security/slot_assertions.py
─────────────────────────────────
Shared assertion for the backend-recorded slot lease (review of 5b, finding 3), so
tests/security/test_prove_it_fails_worker.py can switch the protection off and show that the SAME helper
then raises.
"""

from collections.abc import Callable

from app.core.slots import SlotBackendUnavailable, SlotLease, SlotManager


async def assert_redis_lease_is_not_judged_from_memory(
    manager: SlotManager,
    lease: SlotLease,
    break_redis: Callable[[], None],
    restore_redis: Callable[[], None],
    *,
    ttl: float = 30.0,
) -> None:
    """
    A lease taken from Redis, then Redis fails or is slow (development mode, where a memory fallback
    exists): renew must RAISE SlotBackendUnavailable. Judging it from memory, where the token never was,
    says "lease lost" about a lease that is alive. Afterwards Redis recovers and the lease still renews.
    """
    break_redis()
    try:
        outcome = await manager.renew(lease, ttl)
    except SlotBackendUnavailable:
        outcome = None
    else:
        raise AssertionError(f"a Redis-held lease was renewed without Redis and answered {outcome!r}")
    finally:
        restore_redis()
    assert outcome is None
    assert await manager.renew(lease, ttl) is True, "the Redis-held lease was lost although Redis never lost it"

