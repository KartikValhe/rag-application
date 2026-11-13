"""
app/core/body_limit.py
──────────────────────
Pure ASGI middleware that stops oversized uploads BEFORE Starlette spools the multipart body.

Applies to multipart/form-data requests and to the timetable import/template routes:
- a declared Content-Length above the limit is answered with 413 immediately;
- the bytes actually received are counted, so chunked requests without Content-Length
  (or with a lying Content-Length) are cut off as soon as the limit is crossed;
- the number of multipart parts is capped by counting boundary delimiters in the stream.

The reverse proxy must still cap the body size too (see README).
"""

import json
import re
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.upload_security import MAX_UPLOAD_SIZE

# One file plus multipart framing overhead
MAX_REQUEST_BODY_BYTES = MAX_UPLOAD_SIZE + 64 * 1024
MAX_MULTIPART_PARTS = 5

# Routes limited even when they are not multipart (the import and template endpoints)
PROTECTED_PATH_RE = re.compile(r"/timetables/(import-preview|save-imported-draft|template\.(csv|xlsx))$")
BOUNDARY_RE = re.compile(r'boundary="?([^";]+)"?', re.IGNORECASE)


class RequestBodyTooLarge(Exception):
    def __init__(self, code: str = "file_too_large") -> None:
        super().__init__(code)
        self.code = code


_MESSAGES = {
    "file_too_large": "The request body exceeds the maximum allowed size.",
    "too_many_parts": "The multipart request contains too many parts.",
}


class BodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int = MAX_REQUEST_BODY_BYTES,
        max_parts: int = MAX_MULTIPART_PARTS,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_parts = max_parts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        content_type = headers.get(b"content-type", b"").decode("latin-1")
        is_multipart = content_type.lower().startswith("multipart/")
        if not is_multipart and not PROTECTED_PATH_RE.search(scope.get("path", "")):
            await self.app(scope, receive, send)
            return

        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_big = int(declared) > self.max_body_bytes
            except ValueError:
                too_big = True
            if too_big:
                await self._reject(send, "file_too_large")
                return

        delimiter = b""
        if is_multipart:
            m = BOUNDARY_RE.search(content_type)
            if m:
                delimiter = b"--" + m.group(1).encode("latin-1")

        total = 0
        parts = 0
        tail = b""
        response_started = False
        rejected = False

        async def limited_receive() -> Message:
            nonlocal total, parts, tail, rejected
            message = await receive()
            if message["type"] == "http.request":
                body: bytes = message.get("body", b"")
                total += len(body)
                if total > self.max_body_bytes:
                    rejected = await self._reject_now(send, response_started, "file_too_large")
                    raise RequestBodyTooLarge("file_too_large")
                if delimiter:
                    window = tail + body
                    parts += window.count(delimiter)
                    # Keep a tail shorter than the delimiter so a match is never counted twice
                    tail = window[-(len(delimiter) - 1) :] if len(delimiter) > 1 else b""
                    # N parts produce N opening delimiters plus one closing delimiter
                    if parts > self.max_parts + 1:
                        rejected = await self._reject_now(send, response_started, "too_many_parts")
                        raise RequestBodyTooLarge("too_many_parts")
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if rejected:
                return  # the 413 is already on the wire; framework error responses are dropped
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except RequestBodyTooLarge as exc:
            # FastAPI turns exceptions raised while reading the body into a 400, so the 413 is
            # normally sent from limited_receive; this covers a raise that escapes unchanged.
            if not rejected and not response_started:
                await self._reject(send, exc.code)

    @staticmethod
    async def _reject_now(send: Send, response_started: bool, code: str) -> bool:
        """Send the 413 unless a response already started. Returns True when it was sent."""
        if response_started:
            return False
        await BodyLimitMiddleware._reject(send, code)
        return True

    @staticmethod
    async def _reject(send: Send, code: str) -> None:
        payload: dict[str, Any] = {"detail": {"code": code, "message": _MESSAGES[code]}}
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"connection", b"close"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
