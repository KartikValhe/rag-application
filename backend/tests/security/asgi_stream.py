"""
A tiny ASGI-level client for upload tests.

httpx hides how much of the request body the server actually pulled. This client calls the ASGI
app directly, hands the body over in chunks and records how many chunks the app consumed, so a
test can prove that a request was refused BEFORE its body was read. Raw header lists are passed
through unchanged, so repeated headers can be sent.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

Header = tuple[str, str]


@dataclass
class AsgiResult:
    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes
    chunks_consumed: int
    chunks_total: int
    extra: dict[str, Any] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body)

    def header(self, name: str) -> str | None:
        for k, v in self.headers:
            if k.decode("latin-1").lower() == name.lower():
                return v.decode("latin-1")
        return None


async def call_asgi(
    app: Callable[..., Awaitable[None]],
    method: str,
    path: str,
    headers: list[Header],
    chunks: list[bytes],
    *,
    query: str = "",
    client_ip: str = "127.0.0.1",
    hold: asyncio.Event | None = None,
    started: asyncio.Event | None = None,
) -> AsgiResult:
    """
    Run one request through `app`. `headers` are sent exactly as given (no Host or Content-Length
    is added). When `hold` is given, the first chunk is delivered, `started` is set and the next
    chunk waits until `hold` is set: this keeps a request in flight.
    """
    raw_headers = [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers]
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": raw_headers,
        "client": (client_ip, 50000),
        "server": ("testserver", 80),
    }
    pending = list(chunks)
    consumed = 0
    response_done = asyncio.Event()
    status = 0
    resp_headers: list[tuple[bytes, bytes]] = []
    body = bytearray()

    empty_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal consumed, empty_sent
        if pending:
            if hold is not None and consumed == 1:
                if started is not None:
                    started.set()
                await hold.wait()
            chunk = pending.pop(0)
            consumed += 1
            return {"type": "http.request", "body": chunk, "more_body": bool(pending)}
        if not chunks and not empty_sent:
            empty_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        # Body finished: behave like a real server and only report a disconnect at the end
        await response_done.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, resp_headers
        if message["type"] == "http.response.start":
            status = message["status"]
            resp_headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if not message.get("more_body"):
                response_done.set()

    if hold is not None and len(chunks) < 2:
        raise ValueError("hold needs at least two chunks")
    await app(scope, receive, send)
    response_done.set()
    return AsgiResult(status, resp_headers, bytes(body), consumed, len(chunks))


def multipart_body(
    boundary: str, filename: str, content: bytes, content_type: str = "text/csv", field_name: str = "file"
) -> bytes:
    return (
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()


def split(data: bytes, size: int = 64 * 1024) -> list[bytes]:
    return [data[i : i + size] for i in range(0, len(data), size)] or [b""]
