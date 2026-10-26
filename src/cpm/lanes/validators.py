"""Built-in output validators. Lane types add more with `registry.register_validator`.

All of them read files in the task worktree only (never follow a symlink out) and never run a
program. Parameters come from the profile (trusted), file contents are untrusted data.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

from cpm.lanes.registry import Finding, ValidationContext, register_validator

MAX_JSON_BYTES = 2_000_000


def _glob(ctx: ValidationContext, pattern: object) -> list[str]:
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("a glob parameter is required")
    return [c.path for c in ctx.present if fnmatch.fnmatchcase(c.path, pattern)]


def _safe_file(ctx: ValidationContext, rel: str) -> Path | None:
    p = ctx.worktree / rel
    try:
        real = p.resolve(strict=True)
        real.relative_to(ctx.worktree.resolve())
    except (OSError, ValueError):
        return None
    return real if real.is_file() else None


def files_exist(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob: "assets/voice/*.wav", min_count: 1, max_count: 500}`."""
    found = _glob(ctx, params.get("glob"))
    lo, hi = int(params.get("min_count", 1)), params.get("max_count")
    out: list[Finding] = []
    if len(found) < lo:
        out.append(
            Finding(
                "too_few_files",
                f"expected at least {lo} file(s) matching {params['glob']!r}, found {len(found)}",
            )
        )
    if hi is not None and len(found) > int(hi):
        out.append(
            Finding(
                "too_many_files",
                f"expected at most {hi} file(s) matching {params['glob']!r}, found {len(found)}",
            )
        )
    return out


def file_size(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob: "*.wav", min_bytes: 1000, max_bytes: 10000000}` for every matching file."""
    lo, hi = int(params.get("min_bytes", 1)), params.get("max_bytes")
    out: list[Finding] = []
    for c in ctx.present:
        if not fnmatch.fnmatchcase(c.path, str(params.get("glob", "*"))):
            continue
        if c.size < lo:
            out.append(Finding("file_too_small", f"{c.size} bytes, minimum {lo}", c.path))
        if hi is not None and c.size > int(hi):
            out.append(Finding("file_too_big", f"{c.size} bytes, maximum {hi}", c.path))
    return out


def name_pattern(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob: "assets/**", regex: "^[a-z0-9_/]+\\.glb$"}`: naming convention check."""
    import re

    rx = re.compile(str(params["regex"]))
    return [
        Finding("bad_name", f"name does not match {rx.pattern}", p)
        for p in _glob(ctx, params.get("glob", "*"))
        if not rx.search(p)
    ]


def json_keys(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob: "out/*.json", keys: ["id", "seed"]}`: valid JSON object with these top-level keys."""
    keys = [str(k) for k in params.get("keys", [])]
    out: list[Finding] = []
    for rel in _glob(ctx, params.get("glob")):
        f = _safe_file(ctx, rel)
        if f is None or f.stat().st_size > MAX_JSON_BYTES:
            out.append(Finding("json_unreadable", "file missing, linked out, or too big", rel))
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            out.append(Finding("json_invalid", "not valid JSON", rel))
            continue
        if not isinstance(data, dict):
            out.append(Finding("json_shape", "top level must be an object", rel))
            continue
        out += [Finding("json_key", f"missing key {k!r}", rel) for k in keys if k not in data]
    return out


for _kind, _fn in {
    "files_exist": files_exist,
    "file_size": file_size,
    "name_pattern": name_pattern,
    "json_keys": json_keys,
}.items():
    register_validator(_kind, _fn)
