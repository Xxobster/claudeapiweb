"""Lane command allowlist, evaluated by `workers.permissions._decide_bash` after the shell
operator filter (so no command here can chain, substitute or redirect).

A rule names the exact program, an exact token prefix, and the flags it accepts, each with a kind:
`none` (switch), `value` (one value matching the rule's pattern), `path` (an existing-or-new path
inside the worktree) or `out_path` (a path that must also lie inside the lane's asset paths).
Positional arguments are refused unless the rule allows them. Anything else is denied with the
reason. Paths are checked textually and again against the real filesystem (symlinks/junctions).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from cpm.lanes.spec import CommandRule, LaneSpec

if TYPE_CHECKING:
    from cpm.workers.permissions import Decision, ToolContext

BashHook = Callable[["ToolContext", list[str]], "Decision | None"]
_DRIVE = re.compile(r"^[A-Za-z]:")


def _in_assets(rel: str, assets: Sequence[str]) -> bool:
    if not assets:
        return True
    r = rel.strip("/")
    for a in assets:
        e = a.replace("\\", "/").strip("/")
        if not e or r == e or r.startswith(e + "/"):
            return True
    return False


def _check_path(ctx: ToolContext, tok: str, *, out: bool, assets: Sequence[str]) -> str | None:
    """Denial reason for a path argument, or None."""
    from cpm.workers.permissions import _inside, _protected, _real_inside, _rel

    if not _real_inside(ctx, tok):
        return f"path argument {tok!r} leaves the worktree"
    if not out:
        return None
    real = _inside(ctx, tok)
    if isinstance(real, str):
        return real
    rel = _rel(ctx, real)
    if any(_protected(p) for p in PurePosixPath(rel).parts):
        return f"{rel} is protected"
    if not _in_assets(rel, assets):
        return f"{rel} is outside the lane's asset paths"
    return None


def match_rule(
    rule: CommandRule, tokens: list[str], ctx: ToolContext, assets: Sequence[str]
) -> Decision | None:
    """None = this rule's program is not the one typed; Decision = allowed or why not."""
    from cpm.workers.permissions import Decision

    def no(why: str) -> Decision:
        return Decision(False, f"lane command {rule.name!r}: {why}")

    if tokens[0].casefold().removesuffix(".exe") != rule.program.casefold().removesuffix(".exe"):
        return None
    rest = tokens[1:]
    if rest[: len(rule.prefix)] != rule.prefix:
        return None  # another rule of the same program may match
    rest = rest[len(rule.prefix) :]
    if rule.needs_trust and not ctx.trusted:
        return no("needs a trusted workspace (it runs repository code)")
    pattern = re.compile(rule.value_pattern)
    positional = 0
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("-") and len(tok) > 1:
            name, eq, inline = tok.partition("=")
            kind = rule.flags.get(name)
            if kind is None:
                return no(f"option {name} is not allowed")
            if kind == "none":
                if eq:
                    return no(f"option {name} takes no value")
            else:
                if eq:
                    value = inline
                else:
                    i += 1
                    if i >= len(rest):
                        return no(f"option {name} needs a value")
                    value = rest[i]
                if kind == "value":
                    if value.startswith(("/", "~", "\\")) or _DRIVE.match(value):
                        return no(f"value of {name} must not be an absolute path")
                    if ".." in value.replace("\\", "/").split("/") or not pattern.match(value):
                        return no(f"value of {name} is not acceptable")
                else:
                    why = _check_path(ctx, value, out=kind == "out_path", assets=assets)
                    if why:
                        return no(why)
        else:
            if rule.positional == "none" or positional >= rule.max_positional:
                return no("unexpected argument")
            why = _check_path(ctx, tok, out=rule.positional == "out_path", assets=assets)
            if why:
                return no(why)
            positional += 1
        i += 1
    return Decision(True)


def make_hook(rules: Sequence[CommandRule], assets: Sequence[str]) -> BashHook:
    """The `ToolContext.bash_extra` callable for a set of rules (used inside the worker child)."""

    def hook(ctx: ToolContext, tokens: list[str]) -> Decision | None:
        first_denial: Decision | None = None
        for rule in rules:
            d = match_rule(rule, tokens, ctx, assets)
            if d is None:
                continue
            if d.allow:
                return d
            first_denial = first_denial or d
        return first_denial

    return hook


def serialise(spec: LaneSpec) -> dict[str, Any]:
    """JSON-safe form stored in the run spec (no secret: it is the profile's own text)."""
    return {
        "commands": [c.model_dump() for c in spec.commands],
        "assets": spec.assets(),
    }


def hook_from_json(data: dict[str, Any] | None) -> BashHook | None:
    if not data or not data.get("commands"):
        return None
    rules = [CommandRule.model_validate(c) for c in data["commands"]]
    return make_hook(rules, [str(a) for a in data.get("assets", [])])


def describe(rules: Sequence[CommandRule]) -> str:
    out = []
    for r in rules:
        flags = " ".join(f"{k}{'' if v == 'none' else ' <' + v + '>'}" for k, v in r.flags.items())
        shown = " ".join([r.program, *r.prefix, flags]).strip()
        extra = " [ARGS]" if r.positional != "none" else ""
        line = f"- {shown}{extra}"
        out.append(f"{line}: {r.description}" if r.description else line)
    return "\n".join(out)
