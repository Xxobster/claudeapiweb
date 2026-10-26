"""One-time WebSocket tickets (docs/THREATS.md T-27, decided in WP15).

Browsers cannot set headers on a WebSocket, so the install token cannot be sent the way HTTP
calls send it. The page asks `POST /api/ws-ticket` (token + CSRF, so only a page that already holds
the token can do it) for a ticket bound to one WebSocket path, and opens the socket with
`?ticket=...`. A ticket is random, valid for a few seconds, usable once and useless for any other
path, so a copy in an access log or the browser history grants nothing. The install token itself
never goes into a URL.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable

TTL_S = 30.0
MAX_OUTSTANDING = 64


class TicketStore:
    def __init__(self, ttl_s: float = TTL_S, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._tickets: dict[str, tuple[str, float]] = {}

    def _purge(self, now: float) -> None:
        for key in [k for k, (_, exp) in self._tickets.items() if exp <= now]:
            del self._tickets[key]

    def issue(self, path: str) -> str | None:
        """A fresh ticket for `path`, or None when too many are outstanding."""
        with self._lock:
            now = self._clock()
            self._purge(now)
            if len(self._tickets) >= MAX_OUTSTANDING:
                return None
            ticket = secrets.token_urlsafe(24)
            self._tickets[ticket] = (path, now + self._ttl)
        return ticket

    def consume(self, ticket: str | None, path: str) -> bool:
        """True once for a live ticket issued for exactly `path`. A wrong path burns the ticket."""
        if not ticket:
            return False
        with self._lock:
            entry = self._tickets.pop(ticket, None)
            if entry is None:
                return False
            bound, expires = entry
            return bound == path and expires > self._clock()
