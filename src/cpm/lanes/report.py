"""Check a worker's final report against the lane's report format.

A section is present when a Markdown heading (`#`..`######`) or a line `Name:` carries its name,
compared case-insensitively. A missing section is an error the worker can fix, so the gate sends
the task back for a correction instead of accepting an unverifiable claim.
"""

from __future__ import annotations

import re

from cpm.lanes.registry import Finding
from cpm.lanes.spec import ReportFormat


def check_report(fmt: ReportFormat, text: str | None) -> list[Finding]:
    if text is None or not text.strip():
        if not fmt.required_sections:
            return []  # no format to hold the worker to
        return [Finding("report_missing", "the worker gave no final report")]
    out: list[Finding] = []
    if len(text) > fmt.max_chars:
        out.append(Finding("report_long", f"report is longer than {fmt.max_chars} characters"))
    for name in fmt.required_sections:
        pat = re.compile(
            rf"^\s*(?:#{{1,6}}\s*|\*\*)?{re.escape(name)}(?:\*\*)?\s*:?\s*(?:\*\*)?\s*$"
            rf"|^\s*{re.escape(name)}\s*:\s*\S",
            re.IGNORECASE | re.MULTILINE,
        )
        if not pat.search(text):
            out.append(Finding("report_section", f"report section {name!r} is missing"))
    return out
