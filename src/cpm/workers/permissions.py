"""Tool permission policy for workers (docs/THREATS.md T-04, T-05, T-06).

`decide` is pure: it gets the tool name and input and answers allow/deny with a reason, so it can
be tested without the SDK. The SDK provider wires it into `can_use_tool`. Nothing here trusts the
model or the managed repo: instructions found in a repo never widen what is allowed.

Design points:
- File tools only reach paths inside the task's worktree, and writes only inside the declared
  scope (when the task has one). `.git`, `.claude` and `.mcp.json` are never writable (a worker
  must not plant hooks or MCP servers).
- Bash accepts ONE simple command from a short allowlist. Any shell operator (semicolon, ampersand,
  pipe, redirection, dollar, backtick, parentheses, braces, backslash, newline) is refused,
  so a command can never chain or substitute another one.
- pytest, ruff and mypy run repository code, so they need a trusted workspace. git reads the
  repository's own config (fsmonitor, hooks, diff/filter drivers can run programs), so for an
  untrusted workspace the supervisor overrides those keys in the child environment
  (`childenv.git_config_env`); hooks only ever run in a trusted workspace.
- Everything else (web, subagents, MCP tools) is denied.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cpm.security.paths import PathPolicyError, resolve_within

READ_TOOLS = frozenset({"Read", "Grep", "Glob", "LS"})
WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
HARMLESS_TOOLS = frozenset({"TodoWrite"})
PROTECTED_PARTS = frozenset({".git", ".claude", ".mcp.json", ".gitmodules"})

_SHELL_META = re.compile(r"[;&|`$<>(){}\\\n\r\x00]")
_GIT_READ = frozenset({"status", "diff", "log", "show", "rev-parse", "ls-files", "grep", "branch"})
_GIT_WRITE = frozenset({"add", "commit"})
_GIT_BAD_OPTS = (
    "--output", "--ext-diff", "--textconv", "--no-index", "--exec", "--upload-pack",
    "--receive-pack", "--git-dir", "--work-tree", "--no-verify", "--amend", "-c", "-C",
    "--force", "-f", "--set-upstream-to",
    "--unset-upstream", "--edit-description", "-O", "--open-files-in-pager",
)  # fmt: skip
_PYTEST_OPTS = frozenset(
    {"-q", "-v", "-vv", "-x", "-s", "-k", "-m", "--maxfail", "--tb", "--no-header", "-rA",
     "-rf", "-rs", "--lf", "--ff", "--collect-only", "-W", "--durations"}
)  # fmt: skip
_OPTS_WITH_VALUE = frozenset({"-k", "-m", "--maxfail", "--tb", "-W", "--durations"})
_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ToolContext:
    worktree: Path
    scope: tuple[str, ...] = ()  # project-relative paths; '/' suffix = prefix; empty = whole tree
    trusted: bool = False
    # Extra Bash commands a lane allows (WP12, `cpm.lanes.commands.make_hook`): called with the
    # tokens of one simple command after the shell-operator filter; None = not one of its commands.
    bash_extra: Callable[[ToolContext, list[str]], Decision | None] | None = None


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""
    updated_command: str | None = None  # Bash command to run instead (hardened), if any


def _deny(reason: str) -> Decision:
    return Decision(False, reason)


_ALLOW = Decision(True)


def _inside(ctx: ToolContext, raw: object) -> Path | str:
    """The real path if inside the worktree, else a denial reason."""
    if not isinstance(raw, str) or not raw:
        return "missing path"
    if raw.startswith("~") or _DRIVE.match(raw):
        return "home-relative and drive paths are not allowed"
    try:
        return resolve_within(ctx.worktree, raw)
    except PathPolicyError as exc:
        return f"path refused: {exc}"


def _in_scope(ctx: ToolContext, rel: str) -> bool:
    if not ctx.scope:
        return True
    for entry in ctx.scope:
        e = entry.rstrip("/")
        if rel == e or rel.startswith(e + "/"):
            return True
    return False


def _rel(ctx: ToolContext, real: Path) -> str:
    return real.relative_to(Path(ctx.worktree).resolve()).as_posix()


def _decide_read(ctx: ToolContext, tool: str, inp: dict[str, Any]) -> Decision:
    for key in ("file_path", "path", "notebook_path"):
        if inp.get(key) is not None:
            res = _inside(ctx, inp[key])
            if isinstance(res, str):
                return _deny(res)
    # A Glob pattern or a Grep glob filter is a path pattern; a Grep pattern is a regex.
    for candidate in (inp.get("glob"), inp.get("pattern") if tool == "Glob" else None):
        if isinstance(candidate, str) and (
            ".." in candidate.replace("\\", "/").split("/")
            or candidate.startswith(("/", "\\", "~"))
            or _DRIVE.match(candidate)
        ):
            return _deny("glob must stay inside the worktree")
    return _ALLOW


_PROTECTED_FOLDED = frozenset(p.casefold() for p in PROTECTED_PARTS)
_SHORT_NAME = re.compile(r"^(git|claude|mcp|gitmo)[a-z]*~\d+(\..*)?$", re.IGNORECASE)


def _protected(part: str) -> bool:
    """Case-insensitive (NTFS/APFS) match, also for 8.3 short names such as CLAUDE~1."""
    name = part.rstrip(". ").casefold()
    return name in _PROTECTED_FOLDED or bool(_SHORT_NAME.match(name))


def _decide_write(ctx: ToolContext, inp: dict[str, Any]) -> Decision:
    res = _inside(ctx, inp.get("file_path", inp.get("notebook_path")))
    if isinstance(res, str):
        return _deny(res)
    rel = _rel(ctx, res)
    if any(_protected(part) for part in rel.split("/")):
        return _deny(f"{rel} is protected (git metadata, hooks and MCP settings)")
    if not _in_scope(ctx, rel):
        return _deny(f"{rel} is outside the task's declared scope")
    return _ALLOW


def _safe_arg(tok: str) -> bool:
    if tok.startswith(("/", "~", "\\")) or _DRIVE.match(tok):
        return False
    return ".." not in tok.split("/")


def _real_inside(ctx: ToolContext, tok: str) -> bool:
    """Textual check plus real-path check: a symlink/junction in the repo must not lead out."""
    if not _safe_arg(tok):
        return False
    return not isinstance(_inside(ctx, tok.split("::", 1)[0] or "."), str)


_DIFF_SUBS = frozenset({"diff", "show", "log"})


def harden_git(tokens: list[str]) -> list[str]:
    """Force `--no-ext-diff --no-textconv` on commands that may run a repo diff driver.

    A repository can map files to `diff.<name>.command|textconv` programs through
    `.gitattributes` plus its config; these flags make git ignore them (THREATS T-05).
    """
    if len(tokens) > 1 and tokens[1] in _DIFF_SUBS:
        return [*tokens[:2], "--no-ext-diff", "--no-textconv", *tokens[2:]]
    return tokens


def _decide_git(ctx: ToolContext, tokens: list[str]) -> Decision:
    if len(tokens) < 2 or tokens[1].startswith("-"):
        return _deny("git needs a subcommand as its first argument")
    sub, rest = tokens[1], tokens[2:]
    if sub not in _GIT_READ and sub not in _GIT_WRITE:
        return _deny(f"git {sub} is not allowed for workers")
    for tok in rest:
        if tok.startswith("-"):
            name = tok.split("=", 1)[0]
            if name in _GIT_BAD_OPTS or name.startswith("--output"):
                return _deny(f"git option {name} is not allowed")
        elif not _safe_arg(tok):
            return _deny("path argument leaves the worktree")
    if sub == "branch" and any(not t.startswith("-") for t in rest):
        return _deny("git branch may only list branches")
    hardened = harden_git(tokens)
    if hardened != tokens:
        return Decision(True, "", shlex.join(hardened))
    return _ALLOW


def _decide_python(ctx: ToolContext, tokens: list[str]) -> Decision:
    if len(tokens) < 3 or tokens[1] != "-m" or tokens[2] not in ("pytest", "ruff", "mypy"):
        return _deny("python may only run -m pytest, ruff or mypy")
    if not ctx.trusted:
        return _deny(
            "running tests and linters executes repository code: needs a trusted workspace"
        )
    args = tokens[3:]
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("-"):
            name = tok.split("=", 1)[0]
            if tokens[2] == "pytest" and name not in _PYTEST_OPTS:
                return _deny(f"pytest option {name} is not allowed")
            if tokens[2] != "pytest" and name in ("--config", "--config-file", "-c", "--plugin"):
                return _deny(f"option {name} is not allowed")
            if name in _OPTS_WITH_VALUE and "=" not in tok:
                i += 1  # the value is free text (an expression), checked by the shell filter
        elif not _real_inside(ctx, tok):
            return _deny("path argument leaves the worktree")
        i += 1
    return _ALLOW


def _decide_bash(ctx: ToolContext, inp: dict[str, Any]) -> Decision:
    cmd = inp.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        return _deny("empty command")
    if len(cmd) > 2000 or _SHELL_META.search(cmd):
        return _deny("only one simple command is allowed (no shell operators)")
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        return _deny("command could not be parsed")
    if not tokens:
        return _deny("empty command")
    if ctx.bash_extra is not None:  # a lane rule is more specific than the built-in allowlist
        extra = ctx.bash_extra(ctx, tokens)
        if extra is not None:
            return extra
    head = tokens[0].lower()
    if head == "git":
        return _decide_git(ctx, tokens)
    if head in ("python", "python3", "py"):
        return _decide_python(ctx, tokens)
    if head in ("ls", "dir", "pwd"):
        return (
            _ALLOW
            if all(t.startswith("-") or _real_inside(ctx, t) for t in tokens[1:])
            else _deny("path argument leaves the worktree")
        )
    return _deny(f"command {tokens[0]!r} is not on the worker allowlist")


def decide(tool: str, inp: dict[str, Any], ctx: ToolContext) -> Decision:
    if tool in READ_TOOLS:
        return _decide_read(ctx, tool, inp)
    if tool in WRITE_TOOLS:
        return _decide_write(ctx, inp)
    if tool == "Bash":
        return _decide_bash(ctx, inp)
    if tool in HARMLESS_TOOLS:
        return _ALLOW
    return _deny(f"tool {tool} is not allowed for workers")


def non_idempotent_step(tool: str, inp: dict[str, Any]) -> str | None:
    """Name of the step when an allowed call changes the repository in a way that cannot simply
    be repeated (a commit). Recovery pauses such a task instead of failing it (WP9)."""
    if tool != "Bash" or not isinstance(inp.get("command"), str):
        return None
    try:
        argv = shlex.split(inp["command"])
    except ValueError:
        return None
    if len(argv) >= 2 and argv[0] == "git" and argv[1] in _GIT_WRITE:
        return f"git {argv[1]}"
    return None
