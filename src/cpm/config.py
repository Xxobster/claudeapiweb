"""Configuration loader. Environment variables (prefix CPM_) override defaults.

Rules enforced here (docs/THREATS.md T-02, T-23): the server binds loopback only, and the data
directory (SQLite DB, logs) must not live inside OneDrive.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(ValueError):
    """Raised for an unsafe or invalid configuration."""


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    data_dir: Path
    log_level: str
    allow_live_key_test: bool = False

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cpm.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"


def _check_loopback(host: str) -> str:
    if host == "localhost":
        return DEFAULT_HOST
    try:
        if ip_address(host).is_loopback:
            return host
    except ValueError:
        pass
    raise ConfigError(f"CPM_HOST must be a loopback address, got {host!r}")


def _check_data_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("onedrive" in part.lower() for part in resolved.parts):
        raise ConfigError(
            f"CPM_DATA_DIR {str(resolved)!r} is inside OneDrive; SQLite WAL files get corrupted "
            "there. Use a local folder such as D:\\projects\\ClaudeProjectManager\\data."
        )
    return resolved


def load_settings(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    try:
        port = int(e.get("CPM_PORT", DEFAULT_PORT))
    except ValueError as exc:
        raise ConfigError("CPM_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("CPM_PORT out of range")
    return Settings(
        host=_check_loopback(e.get("CPM_HOST", DEFAULT_HOST)),
        port=port,
        data_dir=_check_data_dir(Path(e.get("CPM_DATA_DIR", str(_REPO_ROOT / "data")))),
        log_level=e.get("CPM_LOG_LEVEL", "INFO").upper(),
        allow_live_key_test=e.get("CPM_ALLOW_LIVE_KEY_TEST") == "1",
    )
