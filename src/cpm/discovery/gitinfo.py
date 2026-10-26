"""Read-only git inspection of a managed repository (docs/THREATS.md T-04, T-05).

Every call uses an argument list (never a shell string) and a hardened command line:
`core.fsmonitor` and `core.hooksPath` are overridden so a hostile `.git/config` cannot make a
read-only command execute a program; prompts and optional lock files are disabled; no network
command is ever issued. Remote URLs are stripped of embedded credentials before they leave here.
"""

from __future__ import annotations

import os
import re
import subprocess  # noqa: S404 (argument lists only, see tests/security/test_injection.py)
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

TIMEOUT_S = 20
MAX_LIST = 200
_SAFE_ENV = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SystemDrive", "COMSPEC", "WINDIR", "TEMP", "TMP",
    "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL",
)  # fmt: skip  # noqa: E501
_HARDEN = [
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=" + os.devnull,
    "-c",
    "core.pager=cat",
]
_SCP_LIKE = re.compile(r"^[\w.-]+@[\w.-]+:[^\s]+$")


class GitUnavailableError(RuntimeError):
    """git is missing, timed out or refused; the project is registered without git details."""


class RemoteUrlError(ValueError):
    """The remote URL is not an acceptable https/ssh remote."""


def _env() -> dict[str, str]:
    wanted = {n.upper() for n in _SAFE_ENV}
    env = {k: v for k, v in os.environ.items() if k.upper() in wanted}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1")
    return env


def _git(root: Path, *args: str) -> str:
    cmd = ["git", *_HARDEN, "-C", str(root), *args]
    try:
        done = subprocess.run(  # noqa: S603
            cmd, capture_output=True, text=True, timeout=TIMEOUT_S, env=_env(), check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitUnavailableError(type(exc).__name__) from exc
    if done.returncode != 0:
        raise GitUnavailableError(f"git {args[0]} exited with {done.returncode}")
    return done.stdout


def strip_credentials(url: str) -> str:
    """Drop `user:token@` from an https/ssh URL so tokens never reach the DB, logs or the UI."""
    if "://" not in url:
        return url
    parts = urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def validate_remote_url(url: str) -> str:
    """Accept https://, ssh:// and scp-like `git@host:path`; reject option-like or odd input."""
    if not url or url.startswith("-") or any(c.isspace() or ord(c) < 32 for c in url):
        raise RemoteUrlError("remote URL is empty, option-like or contains whitespace")
    clean = strip_credentials(url)
    if clean.startswith(("https://", "ssh://")):
        if not urlsplit(clean).hostname:
            raise RemoteUrlError("remote URL has no host")
        return clean
    if _SCP_LIKE.match(clean):
        return clean
    raise RemoteUrlError("remote must be an https://, ssh:// or git@host:path URL")


@dataclass
class GitInfo:
    is_repo: bool = False
    head: str | None = None
    branch: str | None = None
    dirty_count: int = 0
    dirty_sample: list[str] = field(default_factory=list)
    remotes: dict[str, str] = field(default_factory=dict)
    branches: list[str] = field(default_factory=list)
    error: str | None = None


def is_repo_root(root: Path) -> bool:
    return (root / ".git").exists()


def detect_remote(root: Path) -> str | None:
    """Preferred remote URL (origin, else the first one), credentials stripped; None if absent."""
    remotes = read_remotes(root)
    url = remotes.get("origin") or next(iter(remotes.values()), None)
    if url is None:
        return None
    try:
        return validate_remote_url(url)
    except RemoteUrlError:
        return None


def read_remotes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in _git(root, "remote").split():
        if name.startswith("-"):
            continue
        out[name] = strip_credentials(_git(root, "remote", "get-url", "--", name).strip())
    return out


def inspect(root: Path) -> GitInfo:
    if not is_repo_root(root):
        return GitInfo(is_repo=False)
    info = GitInfo(is_repo=True)
    try:
        info.head = _git(root, "rev-parse", "--verify", "-q", "HEAD").strip() or None
    except GitUnavailableError:
        info.head = None  # fresh repository without commits
    try:
        info.branch = _git(root, "symbolic-ref", "--short", "-q", "HEAD").strip() or None
        status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=normal")
        entries = [e for e in status.split("\0") if e]
        info.dirty_count = len(entries)
        info.dirty_sample = [e[3:] for e in entries[:20]]
        info.remotes = read_remotes(root)
        refs = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads", "refs/remotes")
        info.branches = [r for r in refs.split() if r][:MAX_LIST]
    except GitUnavailableError as exc:
        info.error = str(exc)
    return info
