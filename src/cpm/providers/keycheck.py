"""API key validation. Mock by default (CLAUDE.md rule 7); live only behind an explicit flag.

The live check calls the read-only `GET /v1/models` endpoint, which is not billed per token.
It is disabled unless `CPM_ALLOW_LIVE_KEY_TEST=1` (Micah's go) and the caller asks for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx

KEY_FORMAT_HINT = "expected a standard Anthropic API key (sk-ant-api...)"


@dataclass(frozen=True)
class KeyCheck:
    ok: bool
    message: str


class KeyValidator(Protocol):
    def validate(self, api_key: str) -> KeyCheck: ...


def check_format(api_key: str) -> KeyCheck:
    if not api_key.startswith("sk-ant-") or len(api_key) < 24:
        return KeyCheck(False, KEY_FORMAT_HINT)
    if api_key.startswith("sk-ant-admin"):
        return KeyCheck(False, "admin keys are not accepted (SDK_NOTES section 6)")
    if any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in api_key):
        return KeyCheck(False, "key contains whitespace or non-ASCII characters")
    return KeyCheck(True, "format ok")


class MockKeyValidator:
    """Offline: accepts any well-formed key except ones containing 'invalid'."""

    def validate(self, api_key: str) -> KeyCheck:
        fmt = check_format(api_key)
        if not fmt.ok:
            return fmt
        if "invalid" in api_key:
            return KeyCheck(False, "mock: key rejected")
        return KeyCheck(True, "mock: key accepted (no network call was made)")


class HttpKeyValidator:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def validate(self, api_key: str) -> KeyCheck:
        fmt = check_format(api_key)
        if not fmt.ok:
            return fmt
        client = self._client or httpx.Client(timeout=10.0)
        try:
            r = client.get(
                "https://api.anthropic.com/v1/models",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                params={"limit": 1},
            )
        except httpx.HTTPError:
            return KeyCheck(False, "network error while contacting the API")
        finally:
            if self._client is None:
                client.close()
        if r.status_code == 200:
            return KeyCheck(True, "key accepted by the API")
        if r.status_code in (401, 403):
            return KeyCheck(False, "key rejected by the API")
        return KeyCheck(False, f"unexpected API status {r.status_code}")
