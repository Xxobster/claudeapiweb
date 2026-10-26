"""Discover instruction, memory and task sources inside a managed project (read-only).

Only files whose real path stays inside the project root are reported (junction/symlink escapes
are dropped). Contents are hashed, never returned: managed-repo text is untrusted (T-05) and CPM
never copies CLAUDE.md into prompts (the SDK loads it natively, see docs/SDK_NOTES.md).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from cpm.security.paths import PathPolicyError, resolve_within, validate_root

MAX_HASH_BYTES = 2_000_000
MAX_MATCHES_PER_PATTERN = 200

INSTRUCTION_PATTERNS = (
    "CLAUDE.md",
    "CLAUDE.local.md",
    "AGENTS.md",
    ".claude/rules/**/*.md",
    ".claude/skills/*/SKILL.md",
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".mcp.json",
)
MEMORY_PATTERNS = ("MEMORY.md",)
TASK_PATTERNS = ("TODO.md", "docs/TODO.md")

# Files that can carry hooks, MCP servers or scripts: inert until the workspace is trusted.
_TRUST_GATED = re.compile(r"^(\.claude/(settings(\.local)?\.json|skills/.*)|\.mcp\.json)$")
_KINDS = ("instruction", "task", "memory", "other")


@dataclass(frozen=True)
class FoundSource:
    kind: str
    rel_path: str
    content_hash: str | None
    size: int


def requires_trust(rel_path: str) -> bool:
    return bool(_TRUST_GATED.match(rel_path))


def check_pattern(pattern: str) -> str:
    """Reject glob patterns that could leave the project root."""
    p = pattern.replace("\\", "/")
    if not p or p.startswith("/") or re.match(r"^[A-Za-z]:", p) or ".." in p.split("/"):
        raise PathPolicyError(
            f"glob pattern must be relative and stay inside the project: {pattern!r}"
        )
    if "\x00" in p:
        raise PathPolicyError("NUL byte in pattern")
    return p


def _hash(path: Path) -> tuple[str | None, int]:
    size = path.stat().st_size
    if size > MAX_HASH_BYTES:
        return None, size
    return hashlib.sha256(path.read_bytes()).hexdigest(), size


def _expand(root: Path, pattern: str) -> list[Path]:
    out: list[Path] = []
    for hit in root.glob(check_pattern(pattern)):
        out.append(hit)
        if len(out) >= MAX_MATCHES_PER_PATTERN:
            break
    return sorted(out)


def scan(
    root: Path,
    *,
    instruction: tuple[str, ...] = (),
    memory: tuple[str, ...] = (),
    task: tuple[str, ...] = (),
    other: tuple[str, ...] = (),
) -> list[FoundSource]:
    """Scan defaults plus extra patterns (from a profile or the user). Missing files are skipped."""
    groups = (
        ("instruction", (*INSTRUCTION_PATTERNS, *instruction)),
        ("memory", (*MEMORY_PATTERNS, *memory)),
        ("task", (*TASK_PATTERNS, *task)),
        ("other", other),
    )
    real_root = validate_root(root)
    found: dict[str, FoundSource] = {}
    for kind, patterns in groups:
        for pattern in patterns:
            for hit in _expand(real_root, pattern):
                try:
                    real = resolve_within(root, hit, must_exist=True)
                except PathPolicyError:
                    continue  # escapes the root or has an illegal name
                if not real.is_file():
                    continue
                rel = real.relative_to(real_root).as_posix()
                if rel in found:
                    continue
                digest, size = _hash(real)
                found[rel] = FoundSource(kind, rel, digest, size)
    return sorted(found.values(), key=lambda s: (_KINDS.index(s.kind), s.rel_path))
