"""Path validator (docs/THREATS.md T-03, T-26).

Every path that comes from the UI, a task, a lane or a managed repo goes through `resolve_within`
before it is opened, reserved or handed to a subprocess. It rejects traversal, UNC/device paths,
Windows reserved names, alternate data streams and symlink/junction escapes. Containment is checked
on the real (symlink-resolved) path, so a junction inside the root that points outside is refused.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
}
_BAD_CHARS = re.compile(r'[<>"|?*\x00-\x1f]')
_DRIVE = re.compile(r"^[A-Za-z]:(?=[\\/]|$)")


class PathPolicyError(ValueError):
    """The path violates the path policy. The message never contains file contents."""


def _check_component(part: str, original: str) -> None:
    if part == "..":
        raise PathPolicyError(f"path traversal ('..') is not allowed: {original!r}")
    if _BAD_CHARS.search(part) or ":" in part:
        raise PathPolicyError(f"illegal character in path component {part!r}")
    if part.endswith((".", " ")):
        raise PathPolicyError(f"path component {part!r} ends with a dot or space")
    if part.split(".", 1)[0].strip().upper() in _RESERVED:
        raise PathPolicyError(f"reserved device name in path: {part!r}")


def _check_text(raw: str) -> None:
    """Syntactic checks that do not touch the filesystem. Backslash counts as a separator."""
    if not raw or raw.strip() != raw:
        raise PathPolicyError("empty path or leading/trailing whitespace")
    if "\x00" in raw:
        raise PathPolicyError("NUL byte in path")
    uniform = raw.replace("\\", "/")
    if uniform.startswith("//"):
        raise PathPolicyError("UNC and device paths are not allowed")
    body = _DRIVE.sub("", uniform, count=1)
    for part in body.split("/"):
        if part in ("", "."):
            continue
        _check_component(part, raw)


def _real(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.realpath(path))


def _key(path: Path) -> str:
    return os.path.normcase(str(path))


def _is_within(root: Path, child: Path) -> bool:
    r, c = _key(root), _key(child)
    try:
        return os.path.commonpath([r, c]) == r
    except ValueError:  # different drives
        return False


def validate_root(path: str | os.PathLike[str]) -> Path:
    """Return the real path of an approved root; it must be an existing absolute directory."""
    raw = os.fspath(path).replace("\\", "/")
    _check_text(raw)
    if not Path(raw).is_absolute():
        raise PathPolicyError("a root must be an absolute path")
    real = _real(raw)
    if not real.is_dir():
        raise PathPolicyError("root does not exist or is not a directory")
    return real


def resolve_within(
    root: str | os.PathLike[str],
    candidate: str | os.PathLike[str],
    *,
    must_exist: bool = False,
) -> Path:
    """Resolve `candidate` (relative to `root`, or absolute) and prove it stays inside `root`."""
    real_root = validate_root(root)
    raw = os.fspath(candidate).replace("\\", "/")
    _check_text(raw)
    target = Path(raw)
    full = _real(target if target.is_absolute() else real_root / target)
    if not _is_within(real_root, full):
        raise PathPolicyError("path escapes the approved root")
    if must_exist and not full.exists():
        raise PathPolicyError("path does not exist")
    return full


def resolve_in_any(
    roots: Iterable[str | os.PathLike[str]], candidate: str | os.PathLike[str]
) -> Path:
    """Resolve an absolute `candidate` against several approved roots; first match wins."""
    last: PathPolicyError | None = None
    for root in roots:
        try:
            return resolve_within(root, candidate)
        except PathPolicyError as exc:
            last = exc
    raise last or PathPolicyError("no approved root configured")
