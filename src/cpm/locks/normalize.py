"""Canonical resource keys (docs/THREATS.md T-24).

Two spellings of the same file must give the same key or two tasks collide unseen:
case (Windows is case-insensitive), `\\` vs `/`, `.` and `..`, symlink/junction aliases, 8.3 short
names, Unicode forms, and Unity's `.meta` pairing (`a.prefab` and `a.prefab.meta` are one asset).

Path keys are project-relative, lower-case, '/'-separated. A key ending in '/' is a directory
(prefix) scope: it overlaps every key below it. `Assets/Game/UI`, `Assets\\Game\\UI\\`,
`assets/game/ui/**` all mean the same directory scope only when written with a trailing '/', '/*'
or '/**'; a bare name is a file, but a file and a directory with the same name still overlap.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from cpm.security.paths import PathPolicyError, resolve_within, validate_relative

_SHORT_NAME = re.compile(r"~\d")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_.:\-]{0,63}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_./\-]{0,199}$")
_GLOB_TAIL = ("/**", "/*")

# Named resources. `path` and `git_branch` are per project; the others are machine-wide.
PER_PROJECT_TYPES = frozenset({"path", "git_branch"})
NAMED_TYPES = frozenset(
    {"git_branch", "unity_editor", "gpu", "blender", "device", "build", "test_env", "custom"}
)
HEAVY_CLASS = ("class", "heavy")


def canonical(rel: str) -> str:
    """Canonical key of an already-validated relative path (no filesystem access)."""
    text = unicodedata.normalize("NFC", rel).replace("\\", "/").casefold()
    is_dir = text.endswith("/") or text in ("", ".")
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if parts and parts[-1].endswith(".meta") and len(parts[-1]) > len(".meta"):
        parts[-1] = parts[-1][: -len(".meta")]
    key = "/".join(parts)
    return key + "/" if is_dir and parts else ("/" if is_dir else key)


def normalize_scope(project_root: str | os.PathLike[str], raw: str) -> tuple[str, str]:
    """Return `(key, label)` for a project-relative scope entry.

    Rejects traversal, absolute paths and Windows-illegal names (`security.paths`) and 8.3 short
    names (ambiguous alias). Symlinks and junctions are followed: the key is built from the real
    path, so a link inside the project cannot hide its target and a link out of it is refused.
    """
    text = raw.strip() if isinstance(raw, str) else raw
    if not isinstance(text, str) or not text:
        raise PathPolicyError("empty scope entry")
    is_dir = text.endswith(("/", "\\")) or text in (".", "./", ".\\")
    for tail in _GLOB_TAIL:
        if text.replace("\\", "/").endswith(tail):
            text = text[: -len(tail)] + "/"
            is_dir = True
            break
    rel = validate_relative(text)
    if any(_SHORT_NAME.search(part) for part in rel.split("/")):
        raise PathPolicyError("8.3 short names are not allowed in a scope")
    real_root = Path(os.path.realpath(project_root))
    real = resolve_within(real_root, rel)
    try:
        resolved_rel = real.relative_to(real_root).as_posix()
    except ValueError as exc:  # pragma: no cover - resolve_within already proved containment
        raise PathPolicyError("path escapes the project") from exc
    if resolved_rel == ".":
        resolved_rel = ""
    key = canonical(resolved_rel + ("/" if is_dir else ""))
    return key, rel


def normalize_git_path(rel: str) -> str:
    """Key for a path reported by git (already relative; never rejected, only canonicalised)."""
    return canonical(rel)


def normalize_name(kind: str, name: str | None = None) -> str:
    """Key of a named resource: a machine-wide singleton (`unity_editor`) or `<kind>:<name>`."""
    if kind not in NAMED_TYPES:
        raise PathPolicyError(f"unknown resource type {kind!r}")
    if name is None:
        return kind
    if kind == "git_branch":
        if not _BRANCH.match(name) or ".." in name or name.endswith(("/", ".lock", ".")):
            raise PathPolicyError("invalid branch name")
        return name.casefold()
    key = name.casefold()
    if not _NAME.match(key):
        raise PathPolicyError(f"invalid resource name {name!r}")
    return key


def overlaps(a: str, b: str) -> bool:
    """Prefix semantics: a directory key overlaps everything below it; equal names overlap."""
    sa, sb = a.rstrip("/"), b.rstrip("/")
    if sa == sb:
        return True
    if a.endswith("/") and (sa == "" or sb.startswith(sa + "/")):
        return True
    return b.endswith("/") and (sb == "" or sa.startswith(sb + "/"))
