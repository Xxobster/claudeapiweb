"""Detect build/test/lint commands by reading manifests. Detection only: nothing is executed.

Commands are returned as argument lists. Script bodies from package.json/Makefile are never
reported, only the fixed runner invocation (`npm run test`), so repo text cannot inject arguments.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_MAX_MANIFEST = 1_000_000
_MAKE_TARGET = re.compile(r"^(test|build|lint|check):(?!=)", re.M)


@dataclass(frozen=True)
class DetectedCommand:
    purpose: str  # test | build | lint
    argv: tuple[str, ...]
    source: str  # manifest it came from


def _read(root: Path, name: str) -> str | None:
    p = root / name
    try:
        if p.is_file() and not p.is_symlink() and p.stat().st_size <= _MAX_MANIFEST:
            return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return None


def detect_commands(root: Path) -> list[DetectedCommand]:
    out: list[DetectedCommand] = []
    pkg = _read(root, "package.json")
    if pkg is not None:
        try:
            scripts = json.loads(pkg).get("scripts", {})
        except (ValueError, AttributeError):
            scripts = {}
        if isinstance(scripts, dict):
            for name in ("test", "build", "lint"):
                if name in scripts:
                    out.append(DetectedCommand(name, ("npm", "run", name), "package.json"))
    pyproject = _read(root, "pyproject.toml")
    if pyproject is not None:
        if "[tool.pytest" in pyproject or (root / "tests").is_dir():
            out.append(DetectedCommand("test", ("python", "-m", "pytest"), "pyproject.toml"))
        if "[tool.ruff" in pyproject:
            out.append(DetectedCommand("lint", ("ruff", "check", "."), "pyproject.toml"))
    makefile = _read(root, "Makefile")
    if makefile is not None:
        for name in sorted(set(_MAKE_TARGET.findall(makefile))):
            purpose = "test" if name in ("test", "check") else name
            out.append(DetectedCommand(purpose, ("make", name), "Makefile"))
    if _read(root, "Cargo.toml") is not None:
        out += [
            DetectedCommand("test", ("cargo", "test"), "Cargo.toml"),
            DetectedCommand("build", ("cargo", "build"), "Cargo.toml"),
        ]
    if _read(root, "go.mod") is not None:
        out += [
            DetectedCommand("test", ("go", "test", "./..."), "go.mod"),
            DetectedCommand("build", ("go", "build", "./..."), "go.mod"),
        ]
    if next(root.glob("*.sln"), None) is not None:
        out.append(DetectedCommand("test", ("dotnet", "test"), "*.sln"))
    return out


def detect_unity(root: Path) -> str | None:
    """Unity editor version from ProjectSettings/ProjectVersion.txt (validation lane, WP14)."""
    text = _read(root, "ProjectSettings/ProjectVersion.txt")
    if text is None:
        return None
    m = re.search(r"^m_EditorVersion:\s*([\w.\-]+)\s*$", text, re.M)
    return m.group(1) if m else None
