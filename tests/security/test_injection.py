"""Command-injection guard (THREATS T-04): no shell strings anywhere in the package."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "cpm"


def test_no_shell_true_or_os_system():
    bad = re.compile(
        r"shell\s*=\s*True|os\.system\(|os\.popen\(|\bbash\s+-c|create_subprocess_shell"
    )
    hits = [str(p) for p in SRC.rglob("*.py") if bad.search(p.read_text(encoding="utf-8"))]
    assert not hits, hits
