"""Windows-only: Job Object tree-kill. Needs Micah's PC (marker `windows`, skipped in CI)."""

import subprocess
import sys

import pytest

from cpm.workers import proctree

pytestmark = pytest.mark.windows


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects exist only on Windows")
def test_kill_takes_the_whole_tree(tmp_path):
    code = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "print(p.pid,flush=True);time.sleep(60)"
    )
    tree = proctree.spawn([sys.executable, "-c", code], {"SYSTEMROOT": "C:\\Windows"}, tmp_path)
    grand = int(tree.popen.stdout.readline())
    tree.kill()
    tree.popen.wait(10)
    out = subprocess.run(["tasklist", "/FI", f"PID eq {grand}"], capture_output=True, text=True)  # noqa: S603, S607
    assert str(grand) not in out.stdout
    tree.close()
