"""Fixture repositories built at test time (a nested `.git` cannot be committed)."""

from __future__ import annotations

import subprocess  # noqa: S404
from pathlib import Path

_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *_ID, "-C", str(root), *args], check=True, capture_output=True)  # noqa: S603, S607


def write(root: Path, rel: str, text: str = "x\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def make_repo(root: Path, remote: str | None = "https://github.com/example/demo") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    write(root, "README.md", "# demo\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    if remote:
        git(root, "remote", "add", "origin", remote)
    return root


def make_claude_repo(root: Path) -> Path:
    make_repo(root, remote=None)
    write(root, "CLAUDE.md", "# rules\n")
    write(root, "AGENTS.md")
    write(root, "MEMORY.md", "- a fact\n")
    write(root, ".claude/rules/style.md")
    write(root, ".claude/settings.json", '{"hooks": {}}')
    write(root, "docs/TODO.md", "- [ ] thing\n")
    return root
