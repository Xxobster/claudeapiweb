"""The isolated worker (`-I`) must write non-cp1252 characters without crashing (Windows)."""

import subprocess
import sys

_CODE = (
    "from cpm.workers.run import _utf8_stdio, emit; "
    "_utf8_stdio(); emit('text', text='a \\u2194 b')"
)


def test_isolated_worker_stdout_is_utf8_even_with_a_legacy_codepage() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", _CODE],
        capture_output=True,
        env={"SYSTEMROOT": "C:\\Windows", "PYTHONIOENCODING": "cp1252"},
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    assert "\u2194" in done.stdout.decode("utf-8")
