"""Pure ASGI middleware: security headers, cache headers, request logging, body size limit."""

from __future__ import annotations

import logging
import re
import secrets
import time
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("app.request")

CSP = "; ".join(
    [
        "default-src 'self'",
        "img-src 'self' data:",
        "style-src 'self'",
        "script-src 'self'",
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)


def apply_security_headers(headers: MutableHeaders) -> None:
    """For responses built outside the middleware stack (the 500 page)."""
    headers["content-security-policy"] = CSP
    headers["x-content-type-options"] = "nosniff"
    headers["x-frame-options"] = "DENY"
    headers["referrer-policy"] = "strict-origin-when-cross-origin"
    headers["cache-control"] = "no-store"


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path: str = scope["path"]
        query: bytes = scope.get("query_string", b"")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                names = {h[0].lower() for h in headers}

                def add(name: str, value: str) -> None:
                    if name.encode() not in names:
                        headers.append((name.encode(), value.encode()))

                add("x-content-type-options", "nosniff")
                add("referrer-policy", "strict-origin-when-cross-origin")
                add("x-frame-options", "DENY")
                add("permissions-policy", "camera=(), microphone=(), geolocation=(), payment=()")
                add("cross-origin-opener-policy", "same-origin")
                add("content-security-policy", CSP)
                if self.hsts:
                    add("strict-transport-security", "max-age=31536000; includeSubDomains")
                if path.startswith("/static/"):
                    if b"v=" in query:
                        add("cache-control", "public, max-age=31536000, immutable")
                    else:
                        add("cache-control", "public, max-age=86400")
                elif path.startswith("/media/"):
                    # Uploaded files get random names and are never overwritten.
                    add("cache-control", "public, max-age=2592000")
                else:
                    # Every page embeds the visitor's CSRF token: never in a shared cache.
                    add("cache-control", "no-store" if _private(path) else "private, no-cache")
                    add("vary", "HX-Request")
            await send(message)

        await self.app(scope, receive, send_wrapper)


def _private(path: str) -> bool:
    return path.startswith(
        ("/account", "/cart", "/checkout", "/seller", "/admin", "/orders", "/login", "/register")
    )


class AllowedHostsMiddleware:
    """Rejects unknown Host headers, except for /healthz (platform probes send their own host)."""

    def __init__(self, app: ASGIApp, hosts: list[str]) -> None:
        self.app = app
        self.checked = TrustedHostMiddleware(app, allowed_hosts=hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["path"] == "/healthz":
            await self.app(scope, receive, send)
        else:
            await self.checked(scope, receive, send)


class RequestLogMiddleware:
    """One line per request with a request id. Query strings and bodies are never logged."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        rid = secrets.token_hex(6)
        scope.setdefault("state", {})["request_id"] = rid
        start = time.perf_counter()
        status: dict[str, Any] = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope["path"]
            if not path.startswith(("/static/", "/media/")) and path != "/healthz":
                log.info(
                    "%s %s %s %.0fms rid=%s",
                    scope["method"],
                    path,
                    status["code"],
                    (time.perf_counter() - start) * 1000,
                    rid,
                )


class BodySizeLimitMiddleware:
    """Rejects oversized request bodies with 413.

    Ordinary forms get a small limit; only the image-upload routes get the large one, so an
    anonymous visitor cannot make the server parse tens of megabytes.
    """

    UPLOAD_PATHS = re.compile(r"^/(seller|admin)/products/(new|\d+/edit)$|^/admin/homepage$")

    def __init__(self, app: ASGIApp, max_bytes: int, upload_max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.upload_max_bytes = upload_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.upload_max_bytes if self.UPLOAD_PATHS.match(scope["path"]) else self.max_bytes
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > limit:
                await _too_large(send)
                return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send: Send) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": b"Upload too large."})
