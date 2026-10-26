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
    allow_live_runs: bool = False
    live_run_max_microusd: int = 2_000_000
    scheduler_enabled: bool = False
    scheduler_interval_s: float = 2.0
    worktree_root: Path = _REPO_ROOT.parent / "cpm-worktrees"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cpm.db"

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

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


def _check_worktree_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if any("onedrive" in part.lower() for part in resolved.parts):
        raise ConfigError(f"CPM_WORKTREE_ROOT {str(resolved)!r} must not be inside OneDrive")
    return resolved


def load_settings(env: dict[str, str] | None = None) -> Settings:
    e = os.environ if env is None else env
    try:
        port = int(e.get("CPM_PORT", DEFAULT_PORT))
    except ValueError as exc:
        raise ConfigError("CPM_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ConfigError("CPM_PORT out of range")
    try:
        live_max = int(e.get("CPM_LIVE_RUN_MAX_MICROUSD", 2_000_000))
    except ValueError as exc:
        raise ConfigError("CPM_LIVE_RUN_MAX_MICROUSD must be an integer") from exc
    try:
        interval = float(e.get("CPM_SCHEDULER_INTERVAL_S", 2.0))
    except ValueError as exc:
        raise ConfigError("CPM_SCHEDULER_INTERVAL_S must be a number") from exc
    if not 0.2 <= interval <= 600:
        raise ConfigError("CPM_SCHEDULER_INTERVAL_S must be between 0.2 and 600")
    if live_max < 0:
        raise ConfigError("CPM_LIVE_RUN_MAX_MICROUSD cannot be negative")
    return Settings(
        host=_check_loopback(e.get("CPM_HOST", DEFAULT_HOST)),
        port=port,
        data_dir=_check_data_dir(Path(e.get("CPM_DATA_DIR", str(_REPO_ROOT / "data")))),
        log_level=e.get("CPM_LOG_LEVEL", "INFO").upper(),
        allow_live_key_test=e.get("CPM_ALLOW_LIVE_KEY_TEST") == "1",
        allow_live_runs=e.get("CPM_ALLOW_LIVE_RUNS") == "1",
        live_run_max_microusd=live_max,
        scheduler_enabled=e.get("CPM_SCHEDULER") == "1",
        scheduler_interval_s=interval,
        worktree_root=_check_worktree_root(
            Path(e.get("CPM_WORKTREE_ROOT", str(_REPO_ROOT.parent / "cpm-worktrees")))
        ),
    )
