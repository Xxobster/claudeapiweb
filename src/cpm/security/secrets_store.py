"""Secret store (docs/THREATS.md T-01). The API key lives only here, never in SQLite or files.

`KeyringSecretStore` uses the OS vault (Windows Credential Manager / DPAPI via `keyring`). It
fails closed: if no secure backend exists it raises `SecretStoreUnavailable`, it never falls back
to a plaintext file. `MemorySecretStore` is for tests.
"""

from __future__ import annotations

from typing import Any, Protocol

from cpm.security.redaction import register_secret

SERVICE = "ClaudeProjectManager"
ANTHROPIC_KEY = "anthropic_api_key"


class SecretStoreUnavailable(RuntimeError):
    pass


class SecretStore(Protocol):
    def get(self, name: str) -> str | None: ...
    def set(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> None: ...


class MemorySecretStore:
    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def get(self, name: str) -> str | None:
        return self._data.get(name)

    def set(self, name: str, value: str) -> None:
        register_secret(value)
        self._data[name] = value

    def delete(self, name: str) -> None:
        self._data.pop(name, None)


class KeyringSecretStore:
    def __init__(self, service: str = SERVICE) -> None:
        self.service = service

    def _kr(self) -> Any:
        try:
            import keyring
            from keyring.backends import fail
        except ImportError as exc:  # pragma: no cover
            raise SecretStoreUnavailable("keyring is not installed") from exc
        backend = keyring.get_keyring()
        if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 1) < 1:
            raise SecretStoreUnavailable("no secure credential backend on this machine")
        return keyring

    def get(self, name: str) -> str | None:
        value: str | None = self._kr().get_password(self.service, name)
        if value:
            register_secret(value)
        return value

    def set(self, name: str, value: str) -> None:
        register_secret(value)
        self._kr().set_password(self.service, name, value)

    def delete(self, name: str) -> None:
        kr = self._kr()
        try:
            kr.delete_password(self.service, name)
        except kr.errors.PasswordDeleteError:
            pass
