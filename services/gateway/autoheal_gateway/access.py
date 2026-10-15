"""Edge access control for /api/*: body-size guard, API-key auth, rate limiting.

Implemented as pure ASGI middleware so the X-RateLimit-* headers are added to
every response, including ones proxied verbatim from a backend.
"""

from __future__ import annotations

import hashlib
import json
import math
import secrets

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autoheal_gateway.config import GatewaySettings
from autoheal_gateway.metrics import AUTH_FAILURES
from autoheal_gateway.rate_limit import SlidingWindowRateLimiter


def client_ip(scope: Scope, trust_forwarded_for: bool) -> str:
    if trust_forwarded_for:
        for name, value in scope["headers"]:
            if name == b"x-forwarded-for":
                return value.decode("latin-1").split(",")[0].strip()
    client = scope.get("client")
    return client[0] if client else "unknown"


def _header(scope: Scope, wanted: bytes) -> str | None:
    for name, value in scope["headers"]:
        if name == wanted:
            return value.decode("latin-1")
    return None


async def _reject(send: Send, status: int, detail: str, headers: dict[str, str] | None = None) -> None:
    body = json.dumps({"detail": detail}).encode()
    raw = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    raw += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    await send({"type": "http.response.start", "status": status, "headers": raw})
    await send({"type": "http.response.body", "body": body})


class AccessControlMiddleware:
    def __init__(self, app: ASGIApp, settings: GatewaySettings, limiter: SlidingWindowRateLimiter | None) -> None:
        self.app = app
        self.settings = settings
        self.limiter = limiter
        self.keys = settings.api_key_set

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return

        length = _header(scope, b"content-length")
        if length and length.isdigit() and int(length) > self.settings.max_body_bytes:
            await _reject(send, 413, f"request body exceeds {self.settings.max_body_bytes} bytes")
            return

        api_key = _header(scope, b"x-api-key")
        if self.keys and (not api_key or not any(secrets.compare_digest(api_key, k) for k in self.keys)):
            AUTH_FAILURES.inc()
            await _reject(send, 401, "missing or invalid X-API-Key", {"WWW-Authenticate": "ApiKey"})
            return

        if self.limiter is None:
            await self.app(scope, receive, send)
            return

        # Never put raw keys in Redis key names.
        identity = (
            "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
            if api_key
            else "ip:" + client_ip(scope, self.settings.trust_forwarded_for)
        )
        result = await self.limiter.hit(identity)
        rate_headers = {
            "X-RateLimit-Limit": str(result.limit),
            "X-RateLimit-Remaining": str(result.remaining),
            "X-RateLimit-Reset": str(math.ceil(result.reset_after)),
        }
        if result.degraded:
            rate_headers["X-RateLimit-Degraded"] = "true"
        if not result.allowed:
            rate_headers["Retry-After"] = str(max(1, math.ceil(result.reset_after)))
            await _reject(send, 429, "rate limit exceeded", rate_headers)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for k, v in rate_headers.items():
                    headers[k] = v
            await send(message)

        await self.app(scope, receive, send_with_headers)
