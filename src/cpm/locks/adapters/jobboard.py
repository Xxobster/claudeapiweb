"""Read-only parser for the managed project's `docs/JOB_BOARD.md` (Nirandyr rule 13).

Rule 13: a file claimed by another active job is off limits. This adapter lists the files of
active rows and refuses exclusive path requests that overlap them. The real file format is not
verified yet (WP6 open item, needs the PC), so the parser is deliberately tolerant and fails closed:

* every Markdown table row is a row; the header names decide the columns (`status`, `files`/`path`/
  `claim`/`scope`, `owner`/`thread`, `job`/`task`);
* a row is released only when its status is clearly one of `done`, `released`, `closed`, ...;
  an unknown or missing status counts as active;
* only backtick-quoted or path-looking tokens of the files column become claims, and each goes
  through the path policy; anything else is ignored (never executed, never echoed back raw).
Rows whose owner mentions `cpm` are CPM's own claims (WP11) and never block CPM.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from cpm.locks import normalize
from cpm.locks.types import Blocker, ResourceRequest
from cpm.security.paths import PathPolicyError, validate_relative

MAX_BYTES = 1_000_000
MAX_CLAIMS = 2000
RELEASED = {
    "done",
    "released",
    "closed",
    "complete",
    "completed",
    "merged",
    "cancelled",
    "abandoned",
    "free",
    "finished",
}  # noqa: E501
_FILES_HEADERS = ("files", "file", "path", "paths", "claim", "scope")
_TOKEN = re.compile(r"`([^`]+)`|([^\s,;|<>]+)")


@dataclass(frozen=True)
class BoardClaim:
    key: str
    label: str
    owner: str
    job: str
    status: str


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _first(headers: list[str], names: tuple[str, ...]) -> int | None:
    for i, h in enumerate(headers):
        if any(n == h or h.startswith(n) for n in names):
            return i
    return None


def parse(text: str) -> list[BoardClaim]:
    claims: list[BoardClaim] = []
    headers: list[str] | None = None
    for line in text.splitlines():
        if not line.lstrip().startswith("|"):
            headers = None
            continue
        cells = _cells(line)
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue  # separator row
        if headers is None:
            headers = [c.lower() for c in cells]
            continue
        f_i = _first(headers, _FILES_HEADERS)
        if f_i is None or f_i >= len(cells):
            continue
        s_i = _first(headers, ("status", "state"))
        o_i = _first(headers, ("owner", "thread", "who"))
        j_i = _first(headers, ("job", "task", "title"))
        status = cells[s_i].lower() if s_i is not None and s_i < len(cells) else ""
        if status.strip("*_` ") in RELEASED:
            continue
        owner = cells[o_i] if o_i is not None and o_i < len(cells) else ""
        if "cpm" in owner.lower():
            continue
        job = cells[j_i] if j_i is not None and j_i < len(cells) else ""
        for m in _TOKEN.finditer(cells[f_i]):
            tok = (m.group(1) or m.group(2)).strip()
            if "/" not in tok and "\\" not in tok and "." not in tok:
                continue
            tok = tok.replace("\\", "/").lstrip("./") if tok.startswith(("./", ".\\")) else tok
            glob = tok.endswith(("/**", "/*"))
            if glob:
                tok = tok.rsplit("/", 1)[0] + "/"
            try:
                rel = validate_relative(tok)
            except PathPolicyError:
                continue
            key = normalize.canonical(rel)
            if key in ("", "/") and not glob and not rel.endswith("/"):
                continue
            claims.append(BoardClaim(key, rel, owner[:60], job[:80], status[:30]))
            if len(claims) >= MAX_CLAIMS:
                return claims
    return claims


class JobBoard:
    name = "job_board"

    def __init__(self, project_root: str | os.PathLike[str], rel_path: str) -> None:
        self.root = Path(project_root)
        self.rel = rel_path

    def claims(self) -> tuple[list[BoardClaim], str | None]:
        """Active claims, plus a problem string when the board could not be read."""
        from cpm.security.paths import resolve_within

        try:
            path = resolve_within(self.root, self.rel)
        except PathPolicyError:
            return [], "job board path is outside the project"
        try:
            if not path.is_file():
                return [], None  # no board: nothing is claimed
            if path.stat().st_size > MAX_BYTES:
                return [], "job board is larger than 1 MB; ignored"
            return parse(path.read_text(encoding="utf-8", errors="replace")), None
        except OSError:
            return [], "job board cannot be read"

    def blockers(self, requests: list[ResourceRequest], *, heavy: bool) -> list[Blocker]:
        claims, problem = self.claims()
        out: list[Blocker] = []
        for req in requests:
            if req.type != "path" or req.mode != "excl":
                continue
            for c in claims:
                if normalize.overlaps(req.key, c.key):
                    who = f" by {c.owner}" if c.owner else ""
                    out.append(
                        Blocker(
                            "job_board", req.label or req.key,
                            f"blocked by: job board claim on `{c.label}`{who}",
                        )
                    )  # fmt: skip
        if problem:
            out.append(Blocker("job_board", self.rel, f"blocked by: {problem}"))
        return out

    def status(self) -> dict[str, object]:
        claims, problem = self.claims()
        return {
            "name": self.name, "kind": "job_board", "active": bool(claims), "problem": problem,
            "claims": [
                {"path": c.label, "owner": c.owner, "job": c.job, "status": c.status}
                for c in claims[:100]
            ],
        }  # fmt: skip
