"""The gate's test runner must give the child a usable environment (PATH, SYSTEMROOT on Windows)."""

import sys
from pathlib import Path

from cpm.review.testrun import SubprocessTestRunner


def test_test_child_keeps_the_system_variables_it_needs(tmp_path: Path) -> None:
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, sys\n"
        "need = ['SYSTEMROOT'] if os.name == 'nt' else ['PATH']\n"
        "missing = [n for n in need if n not in {k.upper() for k in os.environ}]\n"
        "sys.exit(1 if missing else 0)\n",
        encoding="utf-8",
    )
    out = SubprocessTestRunner().run([sys.executable, str(probe)], tmp_path, timeout_s=30)
    assert out.exit_code == 0
