"""Unity validation validators (WP14): NUnit test results, `.meta` integrity, editor log errors.

They judge files produced by the lane's fixed `Unity -batchmode -runTests` command; none starts
Unity. Results and logs are untrusted: XML with a DOCTYPE/ENTITY is refused (no entity tricks),
sizes are capped, and a missing or unparseable result is an error, never a pass.
"""

from __future__ import annotations

import fnmatch
import re
import xml.etree.ElementTree as ET  # noqa: S405  (DOCTYPE/ENTITY refused before parsing)
from pathlib import Path
from typing import Any

from cpm.lanes.registry import Finding, ValidationContext, register_validator
from cpm.lanes.validators import _safe_file

MAX_XML_BYTES = 20_000_000
MAX_LOG_BYTES = 50_000_000
MAX_META_SCAN = 200_000
_GUID = re.compile(r"^guid:\s*([0-9a-f]{32})\s*$", re.MULTILINE)
_CS_ERROR = re.compile(
    r"(?P<f>[^\s(]+\.cs)\((?P<l>\d+),\d+\): error (?P<c>CS\d{4}): (?P<m>.{0,160})"
)
_LOG_BAD = (
    ("compile_errors", re.compile(r"Scripts have compiler errors|Compilation failed")),
    (
        "missing_script",
        re.compile(
            r"referenced script .{0,80} is missing|Missing Script|"
            r"The associated script can not be loaded"
        ),
    ),
    (
        "missing_guid",
        re.compile(
            r"Could not find (?:script|asset|file) .{0,40}guid|"
            r"GUID \[?[0-9a-f]{32}\]? .{0,80}(?:not found|missing)"
        ),
    ),
    ("fatal", re.compile(r"Aborting batchmode due to failure|Fatal Error|Crash!!!")),
)


def _text(ctx: ValidationContext, rel: str, cap: int) -> str | None:
    f = _safe_file(ctx, rel)
    if f is None or f.stat().st_size > cap:
        return None
    try:
        return f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def unity_results(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{path: "Logs/cpm-validation/results.xml", min_total: 1, allow_skipped: true}`: an NUnit 3
    `test-run` with no failed or inconclusive test (and at least `min_total` tests)."""
    rel = str(params.get("path", ""))
    if not rel:
        raise ValueError("path is required")
    text = _text(ctx, rel, MAX_XML_BYTES)
    if text is None:
        return [Finding("results_missing", "test result file missing or too big", rel)]
    if re.search(r"<!(?:DOCTYPE|ENTITY)", text, re.IGNORECASE):
        return [Finding("results_refused", "result XML contains a DOCTYPE/ENTITY", rel)]
    try:
        root = ET.fromstring(text)  # noqa: S314
    except ET.ParseError:
        return [Finding("results_invalid", "result file is not valid XML", rel)]
    if root.tag != "test-run":
        return [Finding("results_invalid", f"expected <test-run>, found <{root.tag}>", rel)]

    def n(attr: str) -> int:
        try:
            return int(root.get(attr, "0"))
        except ValueError:
            return 0

    total, failed, inc, skipped = n("total"), n("failed"), n("inconclusive"), n("skipped")
    out: list[Finding] = []
    least = int(params.get("min_total", 1))
    if total < least:
        out.append(Finding("too_few_tests", f"{total} test(s) ran, minimum {least}", rel))
    if failed or root.get("result", "").startswith("Failed"):
        names = [
            tc.get("fullname", "?")
            for tc in root.iter("test-case")
            if tc.get("result", "").startswith("Failed")
        ][:10]
        out.append(Finding("tests_failed", f"{failed} failed: {', '.join(names)}", rel))
    if inc:
        out.append(Finding("tests_inconclusive", f"{inc} inconclusive", rel))
    if skipped and not params.get("allow_skipped", True):
        out.append(Finding("tests_skipped", f"{skipped} skipped", rel))
    return out


def unity_log(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{path: "Logs/cpm-validation/editor.log"}`: no C# compile error, missing script/GUID,
    batchmode abort or crash in the editor log."""
    rel = str(params.get("path", ""))
    if not rel:
        raise ValueError("path is required")
    text = _text(ctx, rel, MAX_LOG_BYTES)
    if text is None:
        return [Finding("log_missing", "editor log missing or too big", rel)]
    out: list[Finding] = []
    seen: set[str] = set()
    for m in _CS_ERROR.finditer(text):
        key = f"{m['f']}:{m['l']}:{m['c']}"
        if key not in seen and len(seen) < 20:
            seen.add(key)
            msg = f"{m['c']} {m['m'].strip()}"
            out.append(Finding("compile_error", msg, f"{m['f']}:{m['l']}"))
    for code, rx in _LOG_BAD:
        if rx.search(text) and not (code == "compile_errors" and seen):
            out.append(Finding(code, f"editor log reports: {code.replace('_', ' ')}", rel))
    return out


def _is_hidden(parts: tuple[str, ...]) -> bool:
    # Unity ignores names starting with '.', ending with '~', and `*.tmp` entries.
    return any(p.startswith(".") or p.endswith("~") or p.casefold().endswith(".tmp") for p in parts)


def unity_meta(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{roots: ["Assets/", "Packages/"]}`: every changed asset has a `.meta` with a valid GUID,
    no changed `.meta` is orphaned, and no GUID is shared by two `.meta` files."""
    roots = [str(r).strip("/") for r in params.get("roots", ["Assets/"])]
    wt = ctx.worktree.resolve()

    def under(path: str) -> bool:
        return any(path == r or path.startswith(r + "/") for r in roots)

    out: list[Finding] = []
    changed_guids: dict[str, list[str]] = {}
    for c in ctx.changed:
        if not under(c.path) or _is_hidden(Path(c.path).parts):
            continue
        if c.path.endswith(".meta"):
            asset = c.path[: -len(".meta")]
            if c.status == "D":
                continue
            if not ((wt / asset).exists() or (wt / asset).is_symlink()):
                out.append(Finding("orphan_meta", "`.meta` without its asset", c.path))
            m = _GUID.search(_text(ctx, c.path, 1_000_000) or "")
            if not m:
                out.append(Finding("bad_meta", "no valid `guid:` line in `.meta`", c.path))
            else:
                changed_guids.setdefault(m.group(1), []).append(c.path)
        elif c.status != "D":
            if not (wt / (c.path + ".meta")).is_file():
                out.append(Finding("missing_meta", "asset has no `.meta` file", c.path))
        elif (wt / (c.path + ".meta")).is_file():  # asset deleted: its `.meta` must go too
            out.append(Finding("orphan_meta", "asset deleted but `.meta` kept", c.path + ".meta"))
    for guid, paths in changed_guids.items():
        if len(paths) > 1:
            out += [
                Finding("duplicate_guid", f"GUID {guid} used by {len(paths)} files", q)
                for q in paths
            ]
    if changed_guids:
        scanned = 0
        for root in roots:
            base = wt / root
            if not base.is_dir():
                continue
            for p in base.rglob("*.meta"):
                scanned += 1
                if scanned > MAX_META_SCAN:
                    out.append(Finding("meta_scan_capped", "too many .meta files", None, "warn"))
                    return out
                if p.is_symlink() or not p.is_file():
                    continue
                rel = p.relative_to(wt).as_posix()
                try:
                    m = _GUID.search(p.read_text(encoding="utf-8", errors="replace")[:2000])
                except OSError:
                    continue
                if m and m.group(1) in changed_guids and rel not in changed_guids[m.group(1)]:
                    other = changed_guids[m.group(1)][0]
                    out.append(Finding("duplicate_guid", f"GUID {m.group(1)} also in {other}", rel))
    return out


def unity_no_stray_files(ctx: ValidationContext, params: dict[str, Any]) -> list[Finding]:
    """`{globs: ["Library/**", "Temp/**", "Logs/**"]}`: Unity's generated folders must not be
    committed by a validation task."""
    globs = [str(g) for g in params.get("globs", ["Library/**", "Temp/**", "obj/**"])]
    return [
        Finding("generated_file", "Unity-generated file must not be committed", c.path)
        for c in ctx.present
        if any(fnmatch.fnmatchcase(c.path, g) for g in globs)
    ]


for _kind, _fn in {
    "unity_results": unity_results,
    "unity_log": unity_log,
    "unity_meta": unity_meta,
    "unity_no_stray_files": unity_no_stray_files,
}.items():
    register_validator(_kind, _fn)
