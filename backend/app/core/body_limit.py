"""
app/core/body_limit.py
──────────────────────
Pure ASGI middleware that guards request bodies BEFORE any route reads them. FastAPI parses a
multipart form (and spools large parts to a temp file) before the route's auth dependencies run,
so every check that must happen before the body is read happens here.

Every request:
- a repeated Content-Type or Content-Length is refused with 400 `duplicate_header`. Starlette reads
  the FIRST value of a header, so a check that read another value could be fooled;
- a Content-Length that is not a plain non-negative integer is refused with 400
  `invalid_content_length`.

Upload routes (UPLOAD_ROUTES, keyed by method + full-path regex), in this order, before the body:
1. `Authorization: Bearer <tenant access token>` whose signature, type and expiry verify with the
   normal token function (no DB access, no tenant lookup), else 401. The route's own authorization
   (permissions, scope, tenant match, user status) still runs afterwards, unchanged;
2. multipart routes: content type multipart/form-data with a boundary, read with the same parser
   Starlette uses, else 415; body routes: Content-Length present, else 411, and within the route
   limit, else 413;
3. a per-IP in-flight cap (SlotManager, short TTL), else 429 with Retry-After. If Redis cannot be
   reached outside development the request is refused with 503 (fail closed, like the other slot
   users). Template downloads (GET) only need step 1.

Body limits: an upload route's own limit; else an area limit (every other request under
/api/v1/documents: 64 KB; every request under /api/v1/chat: 32 KB); else, for any other multipart request, the default 2 MB + 64 KB and
5 parts. A tenant's own (lower) file limit is enforced by the route while it streams the file.
The bytes actually received are counted, so a body longer than its Content-Length is cut off as
soon as the limit is crossed, and multipart parts are counted by boundary delimiters.

The reverse proxy must still cap the body size too (see README).
"""

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import HTTPException
from python_multipart.multipart import parse_options_header
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import slots
from app.core.security import decode_access_token, get_client_ip
from app.core.upload_security import DOC_UPLOAD_CEILING_BYTES, MAX_UPLOAD_SIZE

logger = logging.getLogger(__name__)

# One file plus multipart framing overhead
FRAMING_OVERHEAD_BYTES = 64 * 1024
MAX_REQUEST_BODY_BYTES = MAX_UPLOAD_SIZE + FRAMING_OVERHEAD_BYTES
MAX_MULTIPART_PARTS = 5


@dataclass(frozen=True)
class BodyLimit:
    max_body_bytes: int
    max_parts: int


DEFAULT_LIMIT = BodyLimit(MAX_REQUEST_BODY_BYTES, MAX_MULTIPART_PARTS)
DOCUMENTS_LIMIT = BodyLimit(DOC_UPLOAD_CEILING_BYTES + FRAMING_OVERHEAD_BYTES, 6)
SMALL_LIMIT = BodyLimit(64 * 1024, MAX_MULTIPART_PARTS)
# Chat (step 6a-9b, plan 8.5): JSON bodies only. A question is at most 4,000 characters by schema, up to 24 KB as
# \uXXXX escapes, plus the envelope.
CHAT_LIMIT = BodyLimit(32 * 1024, MAX_MULTIPART_PARTS)

# multipart: a form upload; json: a JSON body that still needs the pre-body checks;
# download: a GET that only needs the Bearer check.
RouteKind = Literal["multipart", "json", "download"]


@dataclass(frozen=True)
class UploadRoute:
    method: str
    pattern: re.Pattern[str]  # matched against the WHOLE path (anchored with ^ and $)
    limit: BodyLimit
    kind: RouteKind


# Any one path segment. Starlette matches a route before FastAPI validates the id, so a
# malformed id must still be covered by the pre-body checks.
_SEGMENT = r"[^/]+"
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"

# Every route that reads a form or an import payload. tests/test_upload_preauth.py checks that
# every form route of the real app is listed here. New upload routes get an entry here, never a
# change to the default.
UPLOAD_ROUTES: tuple[UploadRoute, ...] = (
    UploadRoute("POST", re.compile(r"^/api/v1/admin/branding/(?:logo|favicon)$"), DEFAULT_LIMIT, "multipart"),
    UploadRoute("POST", re.compile(r"^/api/v1/admin/users/import$"), DEFAULT_LIMIT, "multipart"),
    UploadRoute("POST", re.compile(rf"^/api/v1/classes/{_SEGMENT}/students/import$"), DEFAULT_LIMIT, "multipart"),
    UploadRoute(
        "POST", re.compile(rf"^/api/v1/classes/{_SEGMENT}/timetables/import-preview$"), DEFAULT_LIMIT, "multipart"
    ),
    UploadRoute(
        "POST", re.compile(rf"^/api/v1/classes/{_SEGMENT}/timetables/save-imported-draft$"), DEFAULT_LIMIT, "json"
    ),
    UploadRoute(
        "GET", re.compile(rf"^/api/v1/classes/{_SEGMENT}/timetables/template\.(?:csv|xlsx)$"), DEFAULT_LIMIT, "download"
    ),
    # Document uploads (step 5b): the platform ceiling, checked again per tenant by the route
    UploadRoute("POST", re.compile(r"^/api/v1/documents$"), DOCUMENTS_LIMIT, "multipart"),
    UploadRoute("POST", re.compile(rf"^/api/v1/documents/{_UUID}/versions$"), DOCUMENTS_LIMIT, "multipart"),
)

# Path prefix -> limit for every OTHER request below it (any method, any content type).
AREA_LIMITS: tuple[tuple[str, BodyLimit], ...] = (
    ("/api/v1/documents", SMALL_LIMIT),
    # An area, not an upload route: chat needs no pre-body Bearer check and must not get the per-IP in-flight cap.
    ("/api/v1/chat", CHAT_LIMIT),
    # The admin chat settings (6a-12a): a small JSON body as well.
    ("/api/v1/admin/chat", CHAT_LIMIT),
)

# Per-IP in-flight cap for upload routes. The TTL only matters if a worker dies mid-request:
# the lease is released as soon as the response is finished.
UPLOAD_IP_MAX_IN_FLIGHT = 4
UPLOAD_IP_SLOT_TTL_SECONDS = 120.0
UPLOAD_IP_KEY_PREFIX = "concurrency:upload:ip:"
UPLOAD_IP_RETRY_AFTER_SECONDS = 5

_SINGLE_VALUE_HEADERS = (b"content-type", b"content-length")
_MAX_CONTENT_LENGTH_DIGITS = 18


def match_upload_route(
    method: str, path: str, routes: tuple[UploadRoute, ...] | None = None
) -> UploadRoute | None:
    """The registered upload route for this method and path, or None."""
    for rule in UPLOAD_ROUTES if routes is None else routes:
        if rule.method == method and rule.pattern.match(path):
            return rule
    return None


def _area_limit(path: str) -> BodyLimit | None:
    for prefix, limit in AREA_LIMITS:
        if path == prefix or path.startswith(prefix + "/"):
            return limit
    return None


def limit_for(method: str, path: str) -> BodyLimit | None:
    """The registered limit for a request, or None when only the multipart default applies."""
    rule = match_upload_route(method, path)
    return rule.limit if rule is not None else _area_limit(path)


def find_duplicate_header(raw_headers: Iterable[tuple[bytes, bytes]]) -> bytes | None:
    """The name of the first single-value header (Content-Type, Content-Length) that is repeated."""
    seen: set[bytes] = set()
    for name, _ in raw_headers:
        lname = name.lower()
        if lname in _SINGLE_VALUE_HEADERS:
            if lname in seen:
                return lname
            seen.add(lname)
    return None


def _first_header(raw_headers: Iterable[tuple[bytes, bytes]], name: bytes) -> bytes | None:
    """The first value of a header, as Starlette's Headers.get() returns it."""
    for key, value in raw_headers:
        if key.lower() == name:
            return value
    return None


def parse_content_length(value: bytes) -> int | None:
    """A plain non-negative decimal integer (ASCII digits only), else None."""
    digits = value.strip(b" \t")
    if not digits or len(digits) > _MAX_CONTENT_LENGTH_DIGITS or not digits.isdigit():
        return None
    return int(digits)


def parse_content_type(value: bytes | None) -> tuple[bytes, bytes]:
    """(media type, boundary), parsed by python-multipart exactly as Starlette's form parser does."""
    if not value:
        return b"", b""
    try:
        media_type, options = parse_options_header(value)
    except Exception:
        return b"", b""
    return media_type, options.get(b"boundary", b"")


def bearer_problem(raw_headers: Iterable[tuple[bytes, bytes]]) -> str | None:
    """
    None when the request carries a tenant access token with a valid signature, type and expiry;
    otherwise the error code. Only the signature and claims are checked: no DB, no tenant lookup.
    """
    value = _first_header(raw_headers, b"authorization")
    if value is None:
        return "missing_token"
    scheme, _, token = value.decode("latin-1").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return "missing_token"
    try:
        payload = decode_access_token(token)
    except Exception:
        return "invalid_token"
    if not payload.get("tenant_id") or not payload.get("sub"):
        return "invalid_token"
    return None


class RequestBodyTooLarge(Exception):
    def __init__(self, code: str = "file_too_large") -> None:
        super().__init__(code)
        self.code = code


# Fixed messages: no header values, paths or library text are ever echoed
_RESPONSES: dict[str, tuple[int, str]] = {
    "file_too_large": (413, "The request body exceeds the maximum allowed size."),
    "too_many_parts": (413, "The multipart request contains too many parts."),
    "duplicate_header": (400, "The request repeats a header that may appear only once."),
    "invalid_content_length": (400, "The Content-Length header is not a valid number."),
    "missing_token": (401, "Authentication required."),
    "invalid_token": (401, "The access token is invalid or has expired."),
    "length_required": (411, "The upload must include a Content-Length header."),
    "unsupported_media_type": (415, "Uploads must be sent as multipart/form-data."),
    "too_many_concurrent_uploads": (
        429,
        "Too many uploads are in progress from this address. Please try again shortly.",
    ),
    "service_unavailable": (503, "Uploads are temporarily unavailable. Please try again in a few minutes."),
}


class BodyLimitMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        max_body_bytes: int = MAX_REQUEST_BODY_BYTES,
        max_parts: int = MAX_MULTIPART_PARTS,
    ) -> None:
        self.app = app
        # Used for multipart requests that match no registered route or area
        self.default_limit = BodyLimit(max_body_bytes, max_parts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # The registry and the helpers are module globals read on every request, so tests can
        # switch a single protection off without rebuilding the app.
        raw_headers: list[tuple[bytes, bytes]] = list(scope.get("headers", []))
        if find_duplicate_header(raw_headers) is not None:
            await self._reject(send, "duplicate_header")
            return

        declared: int | None = None
        raw_length = _first_header(raw_headers, b"content-length")
        if raw_length is not None:
            declared = parse_content_length(raw_length)
            if declared is None:
                await self._reject(send, "invalid_content_length")
                return

        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        media_type, boundary = parse_content_type(_first_header(raw_headers, b"content-type"))
        is_multipart = media_type.lower().startswith(b"multipart/")

        rule = match_upload_route(method, path)
        if rule is not None:
            problem = bearer_problem(raw_headers)
            if problem is not None:
                await self._reject(send, problem, headers=[(b"www-authenticate", b"Bearer")])
                return
            if rule.kind == "multipart" and (media_type != b"multipart/form-data" or not boundary):
                await self._reject(send, "unsupported_media_type")
                return
            if rule.kind != "download" and declared is None:
                await self._reject(send, "length_required")
                return

        limit = rule.limit if rule is not None else _area_limit(path)
        if limit is None:
            if not is_multipart:
                await self.app(scope, receive, send)
                return
            limit = self.default_limit

        if declared is not None and declared > limit.max_body_bytes:
            await self._reject(send, "file_too_large")
            return

        delimiter = b"--" + boundary if is_multipart and boundary else b""

        if rule is None or rule.kind == "download":
            await self._run_counted(scope, receive, send, limit, delimiter)
            return

        ip = get_client_ip(Request(scope)) or "unknown"
        manager = slots.default_manager
        try:
            lease = await manager.acquire(
                [slots.SlotLimit(UPLOAD_IP_KEY_PREFIX + ip[:64], UPLOAD_IP_MAX_IN_FLIGHT, UPLOAD_IP_SLOT_TTL_SECONDS)],
                unavailable_message=_RESPONSES["service_unavailable"][1],
            )
        except slots.SlotRejected:
            retry = str(UPLOAD_IP_RETRY_AFTER_SECONDS).encode()
            await self._reject(send, "too_many_concurrent_uploads", headers=[(b"retry-after", retry)])
            return
        except HTTPException:
            # Redis unreachable outside development: fail closed (SlotManager already logged it)
            await self._reject(send, "service_unavailable")
            return
        try:
            await self._run_counted(scope, receive, send, limit, delimiter)
        finally:
            await manager.release(lease)

    async def _run_counted(
        self, scope: Scope, receive: Receive, send: Send, limit: BodyLimit, delimiter: bytes
    ) -> None:
        """Pass the request on while counting body bytes and multipart parts as they arrive."""
        max_body_bytes, max_parts = limit.max_body_bytes, limit.max_parts
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
                if total > max_body_bytes:
                    rejected = await self._reject_now(send, response_started, "file_too_large")
                    raise RequestBodyTooLarge("file_too_large")
                if delimiter:
                    window = tail + body
                    parts += window.count(delimiter)
                    # Keep a tail shorter than the delimiter so a match is never counted twice
                    tail = window[-(len(delimiter) - 1) :] if len(delimiter) > 1 else b""
                    # N parts produce N opening delimiters plus one closing delimiter
                    if parts > max_parts + 1:
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
        """Send the refusal unless a response already started. Returns True when it was sent."""
        if response_started:
            return False
        await BodyLimitMiddleware._reject(send, code)
        return True

    @staticmethod
    async def _reject(send: Send, code: str, headers: list[tuple[bytes, bytes]] | None = None) -> None:
        status, message = _RESPONSES[code]
        payload: dict[str, Any] = {"detail": {"code": code, "message": message}}
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    # The body was not (fully) read: the connection must not be reused
                    (b"connection", b"close"),
                    *(headers or []),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
