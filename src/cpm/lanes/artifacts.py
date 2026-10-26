"""Artifact policy: where a lane's outputs may be and which binaries may be committed.

Outputs go in the task worktree under the project's asset paths. Large or binary files follow
the project's Git LFS policy: with `binary_policy: ask` (default) a binary needs a person's
decision first (Nirandyr rules on binaries), `lfs_only` accepts it when `.gitattributes` routes
it to LFS, `deny` refuses it. CPM never changes `.gitattributes` or installs LFS.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from cpm.lanes.registry import ChangedFile, Finding
from cpm.lanes.spec import ArtifactPolicy

MAX_ATTR_BYTES = 200_000


def lfs_patterns(worktree: Path) -> list[str]:
    """Patterns of `.gitattributes` lines (root file only) that set `filter=lfs`."""
    f = worktree / ".gitattributes"
    try:
        if f.is_symlink() or not f.is_file() or f.stat().st_size > MAX_ATTR_BYTES:
            return []
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and not parts[0].startswith("#") and "filter=lfs" in parts[1:]:
            out.append(parts[0])
    return out


def _lfs_match(path: str, patterns: list[str]) -> bool:
    name = PurePosixPath(path).name
    for pat in patterns:
        p = pat.lstrip("/")
        if "/" in p:
            if fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(path, p.rstrip("/") + "/*"):
                return True
        elif fnmatch.fnmatchcase(name, p):
            return True
    return False


def _under(path: str, assets: list[str]) -> bool:
    for a in assets:
        e = a.replace("\\", "/").strip("/")
        if not e or path == e or path.startswith(e + "/"):
            return True
    return False


@dataclass
class ArtifactReport:
    findings: list[Finding] = field(default_factory=list)
    needs_decision: list[str] = field(default_factory=list)  # binaries waiting for a person


def check_artifacts(
    policy: ArtifactPolicy, assets: list[str], worktree: Path, changed: list[ChangedFile]
) -> ArtifactReport:
    rep = ArtifactReport()
    patterns = lfs_patterns(worktree)
    for c in changed:
        if c.status == "D":
            continue
        if assets and not _under(c.path, assets):
            rep.findings.append(
                Finding("outside_assets", "output is outside the lane's asset paths", c.path)
            )
            continue
        ext = PurePosixPath(c.path).suffix.casefold()
        if policy.extensions and ext not in policy.extensions:
            rep.findings.append(
                Finding("extension", f"extension {ext or '(none)'} is not allowed", c.path)
            )
            continue
        if c.size > policy.max_file_bytes:
            rep.findings.append(
                Finding(
                    "too_large",
                    f"{c.size} bytes exceeds the lane limit of {policy.max_file_bytes}",
                    c.path,
                )
            )
            continue
        if ext in policy.binary_extensions:
            _binary(policy, patterns, c, rep)
    return rep


def _binary(
    policy: ArtifactPolicy, patterns: list[str], c: ChangedFile, rep: ArtifactReport
) -> None:
    if policy.binary_policy == "deny":
        rep.findings.append(Finding("binary_denied", "binary files are not allowed", c.path))
    elif policy.binary_policy == "lfs_only":
        if not _lfs_match(c.path, patterns):
            rep.findings.append(
                Finding(
                    "binary_not_lfs",
                    "binary file is not covered by a Git LFS rule in .gitattributes",
                    c.path,
                )
            )
    else:  # ask
        rep.needs_decision.append(c.path)
