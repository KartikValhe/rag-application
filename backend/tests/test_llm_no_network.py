"""
Step 6a-2 (docs/phase6-plan.md 11.2, 11.5): no network anywhere in tests, and no vendor SDK anywhere in the app.

The guard refuses a connection or a name lookup for a provider host in EVERY test (conftest, all tiers), and
it fails the test even when the code under test catches the error.
"""

import ast
import contextlib
import socket
from pathlib import Path

import httpx
import pytest

from app.llm.providers import ALLOWED_PROVIDERS, PROVIDER_HOSTS
from tests.fixtures.network_guard import NetworkBlocked, SocketGuard, socket_guard

APP_DIR = Path(__file__).resolve().parent.parent / "app"
PROVIDER_HOST = "api.groq.com"


def test_every_provider_host_is_covered_and_nothing_else() -> None:
    assert {"api.groq.com", "openrouter.ai", "generativelanguage.googleapis.com"} == set(PROVIDER_HOSTS)
    assert {info.host for info in ALLOWED_PROVIDERS.values()} == set(PROVIDER_HOSTS)


# ── the global guard (installed around every test by conftest) ───────────────────────────────────────


def test_the_global_guard_is_active_in_every_test(provider_network_guard: SocketGuard) -> None:
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo(PROVIDER_HOST, 443)
    assert provider_network_guard.attempts  # recorded: a swallowed error would still fail the test
    provider_network_guard.attempts.clear()  # this test provoked it on purpose


def test_the_global_guard_leaves_other_hosts_alone(provider_network_guard: SocketGuard) -> None:
    assert socket.getaddrinfo("localhost", 80)
    assert provider_network_guard.attempts == []


# ── the guard itself ─────────────────────────────────────────────────────────────────────────────────


def test_a_swallowed_attempt_still_fails_the_test(monkeypatch: pytest.MonkeyPatch) -> None:
    with (
        pytest.raises(NetworkBlocked, match="network attempts"),
        socket_guard(monkeypatch, PROVIDER_HOSTS),
        contextlib.suppress(Exception),  # the code under test hides the error
    ):
        socket.create_connection((PROVIDER_HOST, 443), timeout=1)


@pytest.mark.parametrize("host", sorted(PROVIDER_HOSTS))
def test_every_way_to_reach_a_provider_is_refused(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket_guard(monkeypatch, PROVIDER_HOSTS) as guard:
        with pytest.raises(NetworkBlocked):
            socket.getaddrinfo(host, 443)
        with pytest.raises(NetworkBlocked):
            socket.gethostbyname(host)
        with pytest.raises(NetworkBlocked):
            socket.create_connection((host, 443), timeout=1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            with pytest.raises(NetworkBlocked):
                sock.connect((host, 443))
            with pytest.raises(NetworkBlocked):
                sock.connect_ex((host, 443))
        with pytest.raises(NetworkBlocked), httpx.Client(timeout=1) as client:
            client.get(f"https://{host}/v1/models")
        assert len(guard.attempts) >= 6
        guard.attempts.clear()


@pytest.mark.parametrize(
    "spelling",
    [PROVIDER_HOST, PROVIDER_HOST.upper(), PROVIDER_HOST + ".", PROVIDER_HOST.encode(), PROVIDER_HOST.upper().encode() + b"."],
)
def test_every_spelling_of_a_provider_host_is_refused(spelling: object, monkeypatch: pytest.MonkeyPatch) -> None:
    # Found the hard way: anyio resolves host names with BYTES, and the first guard only compared strings
    with socket_guard(monkeypatch, PROVIDER_HOSTS) as guard:
        with pytest.raises(NetworkBlocked):
            socket.getaddrinfo(spelling, 443)  # type: ignore[arg-type]
        guard.attempts.clear()


async def test_the_event_loop_cannot_connect_to_a_provider_either(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    with socket_guard(monkeypatch, PROVIDER_HOSTS) as guard:
        with pytest.raises(NetworkBlocked):
            await asyncio.get_running_loop().create_connection(asyncio.Protocol, PROVIDER_HOST, 443)
        with pytest.raises(NetworkBlocked):
            async with httpx.AsyncClient(timeout=1) as client:
                await client.get(f"https://{PROVIDER_HOST}/v1/models")
        guard.attempts.clear()


def test_the_strict_mode_refuses_every_host_but_this_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket_guard(monkeypatch, None) as guard:
        for host in ("example.com", "192.0.2.1", "10.0.0.5", "2001:db8::1", "api.groq.com", "openrouter.ai"):
            with pytest.raises(NetworkBlocked):
                socket.create_connection((host, 443), timeout=1)
            with pytest.raises(NetworkBlocked):
                socket.getaddrinfo(host, 443)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock, pytest.raises(NetworkBlocked):
            sock.sendto(b"x", ("192.0.2.1", 9))
        assert len(guard.attempts) == 13
        guard.attempts.clear()
        # this machine stays open: the event loop connects a socket pair over loopback when it starts
        for host in ("localhost", "127.0.0.1", "127.9.9.9", "::1"):
            assert not guard._refused(host)  # noqa: SLF001
        for host in ("127.0.0.1.evil.example", "0.0.0.0", "localhost.evil.example", "", None):
            assert guard._refused(host)  # noqa: SLF001


def test_the_guard_is_removed_when_its_monkeypatch_closes() -> None:
    original = socket.getaddrinfo  # (already the global guard's wrapper inside a test: only the inner one is compared)
    with pytest.MonkeyPatch.context() as inner, socket_guard(inner, PROVIDER_HOSTS):
        assert socket.getaddrinfo is not original
    assert socket.getaddrinfo is original


# ── no vendor SDK (plan 7.2: adapters use httpx, never an SDK) ───────────────────────────────────────

VENDOR_SDKS = {"openai", "groq", "anthropic", "google", "litellm", "langchain", "langchain_core", "openrouter", "cohere", "mistralai"}


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    return roots


def test_no_vendor_sdk_is_imported_anywhere_in_the_app() -> None:
    offenders = {
        path.relative_to(APP_DIR).as_posix(): sorted(_imported_roots(path) & VENDOR_SDKS)
        for path in APP_DIR.rglob("*.py")
        if _imported_roots(path) & VENDOR_SDKS
    }
    assert offenders == {}


def test_the_llm_package_of_this_step_makes_no_network_call() -> None:
    # 6a-2 builds the interface, the fake and the policy. Real adapters (httpx) arrive in 6a-3.
    network_modules = {"httpx", "socket", "ssl", "urllib", "requests", "aiohttp", "http", "asyncio.streams"}
    llm_dir = APP_DIR / "llm"
    offenders = {
        path.name: sorted(_imported_roots(path) & network_modules)
        for path in llm_dir.glob("*.py")
        if _imported_roots(path) & network_modules
    }
    assert offenders == {}
