"""
tests/fixtures/network_guard.py
───────────────────────────────
A guard that stops a test from reaching the network, and FAILS the test even when the code under test
catches the error (the guard records every attempt and the context manager raises when it exits).

Two modes:
  * provider hosts only (installed around EVERY test by conftest): a connection or a name lookup for a
    real LLM provider host is refused. Other sockets (Postgres, Redis) are untouched.
  * everything (`hosts=None`, used by the LLM tests): any connect, name lookup or datagram send to a host
    other than this machine (loopback) is refused.

It patches the Python socket layer and the asyncio loop's connect calls. It does not open a socket itself.
"""

from __future__ import annotations

import asyncio.base_events
import ipaddress
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest


class NetworkBlocked(AssertionError):
    """Raised at the point of the attempt, and again when the guard closes, so it cannot be swallowed."""


def host_text(host: object) -> str | None:
    """A host as lower-case text, without a trailing dot. anyio hands host names to getaddrinfo as BYTES, so
    bytes are decoded too; anything else (None, an int) has no name."""
    if isinstance(host, (bytes, bytearray)):
        host = bytes(host).decode("ascii", errors="replace")
    if not isinstance(host, str):
        return None
    return host.lower().rstrip(".")


def is_loopback(host: object) -> bool:
    """This machine only: 'localhost' and the 127.0.0.0/8 and ::1 addresses."""
    text = host_text(host)
    if text is None:
        return False
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


class SocketGuard:
    def __init__(self, hosts: frozenset[str] | None) -> None:
        # None = every host except this machine is refused. Loopback stays open because the event loop
        # itself connects a socket pair over it (Windows) when the session's loop is created.
        self.hosts = None if hosts is None else frozenset(h.lower() for h in hosts)
        self.attempts: list[str] = []

    def _refused(self, host: object) -> bool:
        if self.hosts is None:
            return not is_loopback(host)
        return host_text(host) in self.hosts

    def check(self, host: object, what: str) -> None:
        if self._refused(host):
            self.attempts.append(f"{what} {host!r}")
            raise NetworkBlocked(f"a test tried to reach the network: {what} {host!r}")

    def check_address(self, address: object, what: str) -> None:
        host = address[0] if isinstance(address, tuple) and address else address
        self.check(host, what)


def _install(monkeypatch: pytest.MonkeyPatch, guard: SocketGuard) -> None:
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_create_connection = socket.create_connection
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_loop_create_connection = asyncio.base_events.BaseEventLoop.create_connection
    real_loop_create_datagram = asyncio.base_events.BaseEventLoop.create_datagram_endpoint

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        guard.check(host, "name lookup")
        return real_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(host: Any) -> Any:
        guard.check(host, "name lookup")
        return real_gethostbyname(host)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        guard.check_address(address, "connect")
        return real_create_connection(address, *args, **kwargs)

    def connect(self: socket.socket, address: Any) -> Any:
        guard.check_address(address, "connect")
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        guard.check_address(address, "connect")
        return real_connect_ex(self, address)

    def sendto(self: socket.socket, data: Any, *args: Any) -> Any:
        guard.check_address(args[-1] if args else None, "send")
        return real_sendto(self, data, *args)

    async def loop_create_connection(self: Any, protocol_factory: Any, host: Any = None, *args: Any, **kwargs: Any) -> Any:
        if host is not None:  # None: the caller passes a socket it already has
            guard.check(host, "connect")
        return await real_loop_create_connection(self, protocol_factory, host, *args, **kwargs)

    async def loop_create_datagram(self: Any, protocol_factory: Any, *args: Any, **kwargs: Any) -> Any:
        remote = kwargs.get("remote_addr")
        if guard.hosts is None or remote is not None:
            guard.check_address(remote, "send")
        return await real_loop_create_datagram(self, protocol_factory, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", gethostbyname)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "create_connection", loop_create_connection)
    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "create_datagram_endpoint", loop_create_datagram)


@contextmanager
def socket_guard(monkeypatch: pytest.MonkeyPatch, hosts: frozenset[str] | None) -> Iterator[SocketGuard]:
    """Install the guard; on exit, raise NetworkBlocked if anything tried to connect (even if caught)."""
    guard = SocketGuard(hosts)
    _install(monkeypatch, guard)
    yield guard
    if guard.attempts:
        raise NetworkBlocked("network attempts during the test: " + "; ".join(guard.attempts))
