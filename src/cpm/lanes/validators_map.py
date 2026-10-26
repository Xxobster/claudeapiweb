"""Map lane validators (WP14): grid maps stored as JSON, plus the "frozen map" guard.

The maps lane runs the project's existing generator in verify-only mode (decision 413: the world
map is frozen, golden files are the truth). Validators never run the generator; they judge files.
A map file is a JSON object with a 2D array under `grid_key` (default `tiles`); the optional
`width`/`height` keys must agree with it. Values listed in `walkable` (default `[0]`) are floor.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from collections import deque
from typing import Any

from cpm.lanes.registry import Finding, ValidationContext, register_validator
from cpm.lanes.validators import _glob, _safe_file

MAX_MAP_BYTES = 20_000_000
MAX_CELLS = 4_000_000


def _load(ctx: ValidationContext, rel: str) -> dict[str, Any] | None:
    f = _safe_file(ctx, rel)
    if f is None or f.stat().st_size > MAX_MAP_BYTES:
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _grid(data: dict[str, Any], key: str) -> list[list[Any]] | None:
    g = data.get(key)
    if not isinstance(g, list) or not g or not all(isinstance(r, list) for r in g):
        return None
    if len(g) * max(len(r) for r in g) > MAX_CELLS:
        return None
    return g


def map_dimensions(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob, width, height, grid_key}`: the grid is rectangular with exactly these sizes
    (or `min_width`/`max_width`/`min_height`/`max_height`) and agrees with width/height keys."""
    key = str(params.get("grid_key", "tiles"))
    out: list[Finding] = []
    for rel in _glob(ctx, params.get("glob")):
        data = _load(ctx, rel)
        grid = _grid(data, key) if data is not None else None
        if data is None or grid is None:
            out.append(Finding("map_unreadable", f"no valid {key!r} grid in this file", rel))
            continue
        h, w = len(grid), len(grid[0])
        if any(len(r) != w for r in grid):
            out.append(Finding("map_not_rectangular", "rows have different lengths", rel))
            continue
        for dim, got in (("width", w), ("height", h)):
            exact = params.get(dim)
            lo, hi = params.get(f"min_{dim}"), params.get(f"max_{dim}")
            if exact is not None and got != int(exact):
                out.append(Finding("bad_dimensions", f"{dim} is {got}, expected {exact}", rel))
            if (lo is not None and got < int(lo)) or (hi is not None and got > int(hi)):
                out.append(Finding("bad_dimensions", f"{dim} {got} outside [{lo}, {hi}]", rel))
            if dim in data and data[dim] != got:
                out.append(Finding("dimension_mismatch", f"{dim} key {data[dim]} != {got}", rel))
    return out


def map_connectivity(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob, walkable: [0], grid_key, min_ratio: 1.0}`: walkable cells form one 4-connected
    region covering at least `min_ratio` of all walkable cells (default: all of them)."""
    key = str(params.get("grid_key", "tiles"))
    walk = params.get("walkable", [0])
    ratio = float(params.get("min_ratio", 1.0))
    out: list[Finding] = []
    for rel in _glob(ctx, params.get("glob")):
        data = _load(ctx, rel)
        grid = _grid(data, key) if data is not None else None
        if grid is None:
            out.append(Finding("map_unreadable", f"no valid {key!r} grid in this file", rel))
            continue
        cells = {(y, x) for y, row in enumerate(grid) for x, v in enumerate(row) if v in walk}
        if not cells:
            out.append(Finding("map_no_floor", "no walkable cell", rel))
            continue
        start = min(cells)
        seen = {start}
        queue = deque([start])
        while queue:
            y, x = queue.popleft()
            for n in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if n in cells and n not in seen:
                    seen.add(n)
                    queue.append(n)
        if len(seen) / len(cells) < ratio:
            out.append(
                Finding(
                    "map_disconnected",
                    f"{len(seen)} of {len(cells)} walkable cells reachable from {start}",
                    rel,
                )
            )
    return out


def _digest(ctx: ValidationContext, rel: str) -> str | None:
    f = _safe_file(ctx, rel)
    if f is None or f.stat().st_size > MAX_MAP_BYTES:
        return None
    try:
        return hashlib.sha256(f.read_bytes()).hexdigest()
    except OSError:
        return None


def map_seed_repro(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{glob: "out/*.run1.json", replace: ["run1", "run2"]}`: each file is byte-identical to
    its twin made by a second run with the same seed (reproducibility)."""
    rep = params.get("replace")
    if not (isinstance(rep, list) and len(rep) == 2 and all(isinstance(x, str) and x for x in rep)):
        raise ValueError("replace must be [from, to]")
    out: list[Finding] = []
    for rel in _glob(ctx, params.get("glob")):
        twin = rel.replace(rep[0], rep[1])
        if twin == rel:
            out.append(Finding("repro_name", f"{rep[0]!r} not in the file name", rel))
            continue
        a, b = _digest(ctx, rel), _digest(ctx, twin)
        if a is None or b is None:
            out.append(Finding("repro_missing", f"second run output {twin} missing", rel))
        elif a != b:
            out.append(Finding("not_reproducible", f"differs from {twin} with the same seed", rel))
    return out


def golden_unchanged(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{globs: ["Assets/Maps/golden/**"]}`: verify-only lanes must not touch frozen files, so
    ANY change (add, modify, delete, mode change) to a matching path is an error."""
    globs = params.get("globs")
    if not isinstance(globs, list) or not globs:
        raise ValueError("globs is required")
    return [
        Finding("golden_modified", "frozen golden file changed (the map is verify-only)", c.path)
        for c in ctx.changed
        if any(fnmatch.fnmatchcase(c.path, str(g)) for g in globs)
    ]


for _kind, _fn in {
    "map_dimensions": map_dimensions,
    "map_connectivity": map_connectivity,
    "map_seed_repro": map_seed_repro,
    "golden_unchanged": golden_unchanged,
}.items():
    register_validator(_kind, _fn)
