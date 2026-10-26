"""Independent reviewer (WP10; MASTER_PROMPT section 13).

The reviewer sees ONLY the task text, the acceptance criteria, the changed-file list and the diff:
a fresh context, no tools, no worker transcript. The diff is untrusted data (it may contain text
that tries to steer the reviewer), so the verdict is parsed strictly and a criterion the reviewer
does not explicitly mark met is NOT met. `MockReviewer` is the only implementation that ships in
this work package; a live (cheap model, no tools) reviewer plugs into the same protocol and needs
Micah's go for its budget (docs/IMPACT_MAP.md, open items).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

MAX_ISSUES = 30
MAX_TEXT = 500
SEVERITIES = ("blocker", "warning")
CATEGORIES = ("correctness", "security", "scope", "other")


@dataclass(frozen=True)
class ReviewInput:
    title: str
    description: str
    criteria: list[str]
    files: list[str]
    diff: str
    diff_truncated: bool


@dataclass
class Issue:
    severity: str
    category: str
    text: str


@dataclass
class ReviewVerdict:
    criteria: list[dict[str, Any]] = field(default_factory=list)  # {text, met, note}
    issues: list[Issue] = field(default_factory=list)
    summary: str = ""

    @property
    def unmet(self) -> list[str]:
        return [str(c["text"]) for c in self.criteria if not c["met"]]

    @property
    def blockers(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "blocker"]

    @property
    def approved(self) -> bool:
        return not self.unmet and not self.blockers


class ReviewerUnavailable(RuntimeError):
    """No reviewer can serve this task (e.g. an API task before the live reviewer exists)."""


class Reviewer(Protocol):
    def review(self, data: ReviewInput) -> dict[str, Any]: ...


def _s(v: Any, n: int = MAX_TEXT) -> str:
    return str(v if v is not None else "").replace("\x00", "")[:n]


def parse_verdict(raw: Any, criteria: list[str]) -> ReviewVerdict:
    """Strict, defensive parse. Anything missing or malformed counts against the work."""
    out = ReviewVerdict()
    got: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict):
        for item in raw.get("criteria") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                got[item["text"]] = item
        for item in (raw.get("issues") or [])[:MAX_ISSUES]:
            if isinstance(item, dict):
                sev = str(item["severity"]) if item.get("severity") in SEVERITIES else "blocker"
                cat = str(item["category"]) if item.get("category") in CATEGORIES else "other"
                out.issues.append(Issue(sev, cat, _s(item.get("text"))))
        out.summary = _s(raw.get("summary"), 1000)
    else:
        out.issues.append(Issue("blocker", "other", "the reviewer returned no usable verdict"))
    for text in criteria:
        item = got.get(text)
        met = bool(item and item.get("met") is True)  # only a literal true counts
        note = _s(item.get("note")) if item else "the reviewer did not address this criterion"
        out.criteria.append({"text": text, "met": met, "note": note})
    return out


class MockReviewer:
    """Deterministic reviewer for mock tasks and tests. `script` may return any raw verdict."""

    def __init__(self, script: Callable[[ReviewInput], dict[str, Any]] | None = None) -> None:
        self.script = script
        self.calls: list[ReviewInput] = []

    def review(self, data: ReviewInput) -> dict[str, Any]:
        self.calls.append(data)
        if self.script is not None:
            return self.script(data)
        return {
            "criteria": [{"text": c, "met": True, "note": "mock: accepted"} for c in data.criteria],
            "issues": [],
            "summary": "mock review: no findings",
        }


class ReviewerSource:
    """Which reviewer serves a task. Mock tasks get the mock; API tasks need a live one."""

    def __init__(self, mock: Reviewer | None = None, live: Reviewer | None = None) -> None:
        self.mock = mock if mock is not None else MockReviewer()
        self.live = live

    def for_billing(self, billing_mode: str) -> Reviewer:
        if billing_mode == "mock":
            return self.mock
        if self.live is None:
            raise ReviewerUnavailable(
                "no live reviewer is configured; review the diff yourself and approve it"
            )
        return self.live
