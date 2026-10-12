"""Protection of the local web interface against attacks through the browser.

The service listens on 127.0.0.1 only, but any open browser tab can reach localhost.
So on every request:

- `Host` must be exactly `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding);
- state-changing requests (anything but GET and HEAD) must carry the `Origin` of this
  same service and the CSRF token in the `X-CSRF-Token` header or the `csrf` form field;
- every response gets CSP, no-framing, no-referrer and same-origin CORP/COOP headers
  (other pages can't embed responses or hold a reference to the window).

An unhandled exception becomes a generic 500 response without the error text and is not
logged: the exception text may contain a file path or a meeting title.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable
from urllib.parse import parse_qs

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from transcribator.web.strings import t

FORM_LIMIT_BYTES = 64 * 1024
FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"

CSP = (
    "default-src 'self'; img-src 'self'; style-src 'self'; script-src 'self'; "
    "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
)
_SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-security-policy", CSP.encode()),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"cross-origin-opener-policy", b"same-origin"),
]
_SAFE_METHODS = frozenset({"GET", "HEAD"})

# String keys: the text is picked in the interface language when the response is sent.
_FORBIDDEN = "http_403"
_TOO_LARGE = "http_413"
_SERVER_ERROR = "http_500"


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return bytes(value).decode("latin-1")
    return None


async def _plain(send: Send, status: int, key: str) -> None:
    body = t(key).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
                *_SECURITY_HEADERS,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _TooLarge(Exception):
    pass


async def _read_capped(receive: Receive, limit: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            raise _TooLarge  # the client left: nothing to process
        chunk = bytes(message.get("body", b""))
        size += len(chunk)
        if size > limit:
            raise _TooLarge
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


class LocalOnlyMiddleware:
    """Host, Origin and CSRF checks plus security headers on every response."""

    def __init__(self, app: ASGIApp, *, port: int, csrf_token: str) -> None:
        self.app = app
        self.hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
        self.origins = frozenset({f"http://127.0.0.1:{port}", f"http://localhost:{port}"})
        self.csrf_token = csrf_token

    def _token_ok(self, candidates: Iterable[str]) -> bool:
        expected = self.csrf_token.encode()
        return any(hmac.compare_digest(value.encode(), expected) for value in candidates)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                await self.app(scope, receive, send)
            return  # websockets and the rest are not served

        if _header(scope, b"host") not in self.hosts:
            await _plain(send, 403, _FORBIDDEN)
            return

        method = str(scope.get("method", "GET")).upper()
        if method not in _SAFE_METHODS:
            if _header(scope, b"origin") not in self.origins:
                await _plain(send, 403, _FORBIDDEN)
                return
            header_token = _header(scope, b"x-csrf-token")
            if header_token is None or not self._token_ok([header_token]):
                replayed = await self._check_form_token(scope, receive, send)
                if replayed is None:
                    return
                receive = replayed

        is_static = str(scope.get("path", "")).startswith("/static/")
        started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {name for name, _ in _SECURITY_HEADERS}
                ]
                headers += _SECURITY_HEADERS
                if not is_static:
                    headers = [(k, v) for k, v in headers if k.lower() != b"cache-control"]
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:  # noqa: BLE001 - the exception text may contain content
            if not started:
                await _plain(send, 500, _SERVER_ERROR)

    async def _check_form_token(self, scope: Scope, receive: Receive, send: Send) -> Receive | None:
        """Token from the `csrf` form field. The body is read whole (up to 64 KB) and
        replayed to the app. Returns a new `receive`, or None if a response was sent."""
        content_type = (_header(scope, b"content-type") or "").split(";")[0].strip().lower()
        if content_type != FORM_CONTENT_TYPE:
            await _plain(send, 403, _FORBIDDEN)
            return None
        try:
            body = await _read_capped(receive, FORM_LIMIT_BYTES)
        except _TooLarge:
            await _plain(send, 413, _TOO_LARGE)
            return None
        try:
            fields = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=50)
        except (UnicodeDecodeError, ValueError):
            await _plain(send, 403, _FORBIDDEN)
            return None
        if not self._token_ok(fields.get("csrf", [])):
            await _plain(send, 403, _FORBIDDEN)
            return None

        replayed = False

        async def replay() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return replay
