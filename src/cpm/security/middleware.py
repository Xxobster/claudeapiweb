"""Local-server guard: Host allowlist, Origin allowlist, install token, CSRF token.

Pure ASGI so it also covers WebSockets. Fails closed: any path not in PUBLIC_PATHS needs the
token. No CORS is configured anywhere, so browsers cannot read responses cross-origin, and the
custom headers cannot be sent cross-origin without a preflight that nothing answers.
(docs/THREATS.md T-02.)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from cpm.security.tokens import csrf_for, same

Scope = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[MutableMapping[str, Any]]]
Send = Callable[[MutableMapping[str, Any]], Awaitable[None]]

PUBLIC_PATHS = frozenset({"/health"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
TOKEN_HEADER = b"x-cpm-token"
CSRF_HEADER = b"x-cpm-csrf"


HARDENING_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
)


def _with_hardening(send: Send) -> Send:
    """Add no-store / anti-framing / nosniff headers to every response (clickjacking, caching)."""

    async def wrapped(message: MutableMapping[str, Any]) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            present = {k.lower() for k, _ in headers}
            headers += [(k, v) for k, v in HARDENING_HEADERS if k not in present]
            message = {**message, "headers": headers}
        await send(message)

    return wrapped


def allowed_hosts(port: int) -> frozenset[str]:
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"})


class SecurityMiddleware:
    def __init__(self, app: Callable[..., Awaitable[None]], *, token: str, port: int) -> None:
        self.app = app
        self.token = token
        self.csrf = csrf_for(token)
        self.hosts = allowed_hosts(port)
        self.origins = frozenset(f"http://{h}" for h in self.hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in scope["headers"]}
        reason = self._check(scope, headers)
        if reason is None:
            out = _with_hardening(send) if scope["type"] == "http" else send
            await self.app(scope, receive, out)
            return
        status, code = reason
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        body = json.dumps({"error": code}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json"), *HARDENING_HEADERS],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _check(self, scope: Scope, headers: dict[bytes, bytes]) -> tuple[int, str] | None:
        host = headers.get(b"host", b"").decode("latin-1")
        if host not in self.hosts:
            return 403, "bad_host"  # DNS rebinding defence
        origin = headers.get(b"origin")
        if origin is not None and origin.decode("latin-1") not in self.origins:
            return 403, "bad_origin"
        is_ws = scope["type"] == "websocket"
        method = "GET" if is_ws else scope["method"].upper()
        if is_ws and origin is None:
            return 403, "origin_required"
        if scope["path"] in PUBLIC_PATHS and method in SAFE_METHODS:
            return None
        supplied = headers.get(TOKEN_HEADER)
        if not same(supplied.decode("latin-1") if supplied else None, self.token):
            return 401, "token_required"
        if method not in SAFE_METHODS:
            csrf = headers.get(CSRF_HEADER)
            if not same(csrf.decode("latin-1") if csrf else None, self.csrf):
                return 403, "csrf_required"
        return None
