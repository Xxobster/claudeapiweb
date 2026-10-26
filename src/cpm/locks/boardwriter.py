"""Pure text edits of the managed project's `docs/JOB_BOARD.md` (WP11; Nirandyr rules 13 and 40).

CPM never edits the board in place: `boardpr` turns the text produced here into a docs-only pull
request. The real board format is not verified yet (needs the PC), so the writer is conservative
and fails closed: it only touches a Markdown table that has a files, a status and a job column, it
changes nothing else in the file, and it refuses when no such table exists.

A CPM row is recognised by its marker `CPM #<task id>:` in the job cell and `cpm` in the owner cell,
the same convention `locks.adapters.jobboard` uses to ignore CPM's own claims.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from cpm.locks import normalize
from cpm.locks.adapters import jobboard
from cpm.security.paths import PathPolicyError, validate_relative

CLAIMED = "claimed"
RELEASED = "released"
MAX_FILES = 50
_BAD_CELL = re.compile(r"[\x00-\x1f\x7f|`]")
_SEP = re.compile(r":?-{2,}:?")
_DATE_HEADERS = ("date", "since", "updated", "claimed")


class BoardError(Exception):
    """The board cannot be edited safely. The message is short and shown to the person."""


@dataclass(frozen=True)
class Edit:
    text: str
    changed: bool
    row: str


def marker(task_id: int) -> str:
    return f"CPM #{task_id}:"


def clean_cell(value: str, limit: int) -> str:
    return " ".join(_BAD_CELL.sub(" ", value).split())[:limit]


def clean_files(paths: list[str]) -> list[str]:
    out: list[str] = []
    for raw in paths[:MAX_FILES]:
        try:
            rel = validate_relative(raw)
        except PathPolicyError as exc:
            raise BoardError(f"scope path {raw!r} is not allowed: {exc}") from exc
        if _BAD_CELL.search(rel) or rel in ("", "/", "."):
            raise BoardError(f"scope path {raw!r} cannot be written to the board")
        out.append(rel)
    if not out:
        raise BoardError("the task has no declared scope: there is nothing to claim")
    return out


def detect_eol(raw: bytes) -> str:
    return "\r\n" if raw.count(b"\r\n") * 2 > raw.count(b"\n") else "\n"


def decode(raw: bytes) -> tuple[str, str]:
    """(text with LF newlines, original newline style); strict UTF-8 (a BOM is kept)."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BoardError("the job board is not valid UTF-8; not editing it") from exc
    return text.replace("\r\n", "\n"), detect_eol(raw)


def encode(text: str, eol: str) -> bytes:
    return (text.replace("\n", eol) if eol != "\n" else text).encode("utf-8")


@dataclass
class _Table:
    header: list[str]
    first: int  # index of the first data line
    last: int  # index of the last line of the table
    cols: dict[str, int]


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _find(headers: list[str], names: tuple[str, ...]) -> int | None:
    return jobboard._first(headers, names)


def _tables(lines: list[str]) -> list[_Table]:
    out: list[_Table] = []
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("|"):
            i += 1
            continue
        j = i
        while j < len(lines) and lines[j].lstrip().startswith("|"):
            j += 1
        block = lines[i:j]
        if len(block) >= 2 and all(_SEP.fullmatch(c) for c in _cells(block[1]) if c):
            headers = [c.lower() for c in _cells(block[0])]
            cols = {
                "files": _find(headers, jobboard._FILES_HEADERS),
                "status": _find(headers, ("status", "state")),
                "job": _find(headers, ("job", "task", "title")),
                "owner": _find(headers, ("owner", "thread", "who")),
            }
            if cols["files"] is not None and cols["status"] is not None and cols["job"] is not None:
                found = {k: v for k, v in cols.items() if v is not None}
                out.append(_Table(headers, i + 2, j - 1, found))
        i = j
    return out


def _row(table: _Table, values: dict[str, str], today: str) -> str:
    cells = [""] * len(table.header)
    for key, idx in table.cols.items():
        if key in values:
            cells[idx] = values[key]
    for idx, name in enumerate(table.header):
        if not cells[idx] and any(name.startswith(d) for d in _DATE_HEADERS):
            cells[idx] = today
    return "| " + " | ".join(cells) + " |"


def _cpm_rows(lines: list[str], table: _Table, task_id: int) -> list[int]:
    want = marker(task_id)
    hits = []
    for n in range(table.first, table.last + 1):
        cells = _cells(lines[n])
        job, owner = table.cols["job"], table.cols.get("owner")
        if job >= len(cells) or want not in cells[job]:
            continue
        if owner is not None and (owner >= len(cells) or "cpm" not in cells[owner].lower()):
            continue
        hits.append(n)
    return hits


def _status(lines: list[str], table: _Table, n: int) -> str:
    cells = _cells(lines[n])
    idx = table.cols["status"]
    return cells[idx].strip("*_` ").lower() if idx < len(cells) else ""


def add_claim(text: str, *, task_id: int, title: str, files: list[str], today: str) -> Edit:
    """Append one CPM claim row. Idempotent per task; refuses a clash with another owner's claim."""
    files = clean_files(files)
    lines = text.split("\n")
    tables = _tables(lines)
    if not tables:
        raise BoardError(
            "no table with files, status and job columns found in the job board; not editing it"
        )
    for t in tables:
        for n in _cpm_rows(lines, t, task_id):
            if _status(lines, t, n) not in jobboard.RELEASED:
                return Edit(text, False, lines[n])
    for c in jobboard.parse(text):  # CPM's own rows are skipped by the parser
        for f in files:
            key = normalize.canonical(f)
            if normalize.overlaps(key, c.key):
                who = f" ({c.owner})" if c.owner else ""
                raise BoardError(f"`{f}` overlaps an active claim on `{c.label}`{who}")
    t = tables[0]
    job = f"{marker(task_id)} {clean_cell(title, 80)}".strip()
    row = _row(
        t,
        {
            "job": job,
            "files": ", ".join(f"`{f}`" for f in files),
            "status": CLAIMED,
            "owner": "cpm",
        },
        today,
    )
    lines.insert(t.last + 1, row)
    return Edit("\n".join(lines), True, row)


def release_claim(text: str, *, task_id: int) -> Edit:
    """Mark every active CPM row of the task as released (the row stays, as history)."""
    lines = text.split("\n")
    seen = False
    changed = False
    last = ""
    for t in _tables(lines):
        for n in _cpm_rows(lines, t, task_id):
            seen = True
            last = lines[n]
            if _status(lines, t, n) in jobboard.RELEASED:
                continue
            cells = _cells(lines[n])
            cells += [""] * (len(t.header) - len(cells))
            cells[t.cols["status"]] = RELEASED
            lines[n] = "| " + " | ".join(cells) + " |"
            last, changed = lines[n], True
    if not seen:
        raise BoardError(f"no CPM claim row for task #{task_id} on the board (is its PR merged?)")
    return Edit("\n".join(lines), changed, last)
