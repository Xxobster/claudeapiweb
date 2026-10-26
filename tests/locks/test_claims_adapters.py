import os
import sys
import time

import pytest

from cpm.locks.adapters.devlock import DevLock, wrap_command
from cpm.locks.adapters.jobboard import JobBoard, parse
from cpm.locks.adapters.lockfiles import LockDir, LockFile
from cpm.locks.claims import ClaimError, DesktopClaims
from cpm.locks.types import ReservationConflict, ResourceRequest
from tests.fixtures.repos import write


def p(key, mode="excl"):
    return ResourceRequest("path", key, mode, key)


# ------------------------------------------------------------------ Desktop claims
def test_claim_blocks_overlapping_exclusive_request_with_explanation(env):
    claims = DesktopClaims(env.engine)
    claims.add(env.pid, "Assets\\Game\\UI\\", note="menu rework")
    t = env.task()
    with pytest.raises(ReservationConflict) as e:
        env.res.acquire(t.id, env.pid, [p("assets/game/ui/menu.prefab")])
    assert "Desktop claim on `Assets/Game/UI/`" in str(e.value)
    assert "menu rework" in str(e.value)
    env.res.acquire(t.id, env.pid, [p("assets/game/ui/menu.prefab", "shared")])  # reading is fine
    env.res.acquire(t.id, env.pid, [p("assets/game/audio/")])  # disjoint


def test_claim_release_and_expiry(env):
    claims = DesktopClaims(env.engine)
    c = claims.add(env.pid, "docs/", expires_in_s=1)
    d = claims.add(env.pid, "src/")
    assert {x.id for x in claims.list(env.pid)} == {c.id, d.id}
    claims.release(d.id)
    claims.release(d.id)  # idempotent
    assert {x.id for x in claims.list(env.pid)} == {c.id}
    assert {x.id for x in claims.list(env.pid, include_inactive=True)} == {c.id, d.id}
    t = env.task()
    env.res.acquire(t.id, env.pid, [p("src/a.py")])  # released claim no longer blocks


def test_claim_reports_tasks_already_holding_the_path(env):
    t = env.task()
    env.res.acquire(t.id, env.pid, [p("src/a.py")])
    c = DesktopClaims(env.engine).add(env.pid, "src/")
    assert c.affected_tasks == [t.id]


def test_claim_validation(env):
    claims = DesktopClaims(env.engine)
    for bad in ["../x", "/etc", "C:\\x", "CON"]:
        with pytest.raises(ClaimError):
            claims.add(env.pid, bad)
    with pytest.raises(ClaimError):
        claims.add(env.pid, "ok/", expires_in_s=0)
    with pytest.raises(ClaimError) as e:
        claims.add(9999, "ok/")
    assert e.value.status == 404
    with pytest.raises(ClaimError):
        claims.release(9999)


def test_claim_note_is_redacted(env):
    c = DesktopClaims(env.engine).add(env.pid, "a/", note="key sk-ant-api03-" + "A" * 40)
    assert "sk-ant" not in c.note


# ------------------------------------------------------------------ lock files
def test_lock_file_blocks_heavy_only(tmp_path):
    f = tmp_path / "BUILD_RUNNING.lock"
    a = LockFile("build_running", f)
    assert a.blockers([], heavy=True) == []
    f.write_text("secret text that must never be echoed")
    msgs = [b.message for b in a.blockers([], heavy=True)]
    assert msgs == ["blocked by: `BUILD_RUNNING.lock` exists (no heavy work while it does)"]
    assert a.blockers([], heavy=False) == []
    assert a.status()["active"] is True
    assert "secret" not in str(a.status())


def test_lock_dir(tmp_path):
    d = tmp_path / "gpu-locks"
    a = LockDir("gpu_locks", d)
    assert a.blockers([], heavy=True) == []  # folder absent: nothing holds the GPU
    d.mkdir()
    (d / "note.txt").write_text("x")
    assert a.blockers([], heavy=True) == []
    (d / "voice-123.lock").write_text("")
    assert a.blockers([], heavy=True) and not a.blockers([], heavy=False)
    assert a.status()["files"] == ["voice-123.lock"]


# ------------------------------------------------------------------ job board
BOARD = """\
# Job board

| Job | Owner | Status | Files |
|---|---|---|---|
| Menu | Thread A | active | `Assets/Game/UI/`, Assets/Game/Hero.prefab |
| Old | Thread B | done | `src/old.py` |
| Mystery | Thread C | ??? | `docs/plans/x.md` |
| Mine | CPM worker | active | `tools/mine.py` |
| Hostile | Thread D | active | `../../etc/passwd`, `C:\\Windows\\x`, `rm -rf /` |
| Globbed | Thread E | claimed | `Assets/Maps/**` |
"""


def test_job_board_parse_active_unknown_and_hostile():
    keys = {c.label: c for c in parse(BOARD)}
    assert "Assets/Game/UI/" in keys and "Assets/Game/Hero.prefab" in keys
    assert "docs/plans/x.md" in keys  # unknown status fails closed
    assert "Assets/Maps/" in keys  # glob becomes a prefix
    assert "src/old.py" not in keys  # done
    assert "tools/mine.py" not in keys  # CPM's own claim
    assert not any("passwd" in k or "Windows" in k or "rm" in k for k in keys)


def test_job_board_blocks_overlapping_exclusive_paths(env):
    write(env.root, "docs/JOB_BOARD.md", BOARD)
    jb = JobBoard(env.root, "docs/JOB_BOARD.md")
    out = jb.blockers([p("assets/game/ui/menu.prefab")], heavy=False)
    assert out and "job board claim on `Assets/Game/UI/` by Thread A" in out[0].message
    assert jb.blockers([p("assets/game/ui/menu.prefab", "shared")], heavy=False) == []
    assert jb.blockers([p("src/free.py")], heavy=False) == []


def test_job_board_missing_is_free_but_oversize_fails_closed(env):
    jb = JobBoard(env.root, "docs/JOB_BOARD.md")
    assert jb.blockers([p("a.py")], heavy=False) == []
    write(env.root, "docs/JOB_BOARD.md", "x" * 1_100_000)
    out = jb.blockers([p("a.py")], heavy=False)
    assert out and "larger than 1 MB" in out[0].message


def test_job_board_path_cannot_leave_project(env):
    jb = JobBoard(env.root, "../outside.md")
    assert "outside the project" in jb.blockers([p("a.py")], heavy=False)[0].message


# ------------------------------------------------------------------ devlock
def test_devlock_wrap_command_is_a_structured_argv():
    argv = wrap_command(
        "tools/devices/devlock.py", device="emulator-5554", who="CPM worker", job="smoke",
        prio=3, argv=["adb", "devices"],
    )  # fmt: skip
    assert argv[0] == sys.executable and argv[1] == "-I"
    assert argv[2:4] == ["tools/devices/devlock.py", "run"]
    assert argv[-3:] == ["--", "adb", "devices"]
    for bad in [
        dict(device="a;rm", who="w", job="j", prio=3, argv=["x"]),
        dict(device="a", who="w\nx", job="j", prio=3, argv=["x"]),
        dict(device="a", who="w", job="j", prio=9, argv=["x"]),
        dict(device="a", who="w", job="j", prio=3, argv=[]),
    ]:
        with pytest.raises(ValueError):
            wrap_command("d.py", **bad)  # type: ignore[arg-type]


def test_devlock_lists_files_without_interpreting_them(tmp_path):
    d = tmp_path / ".device-locks"
    d.mkdir()
    (d / "emulator-5554.lock").write_text("whatever")
    st = DevLock(d).status()
    assert st["active"] is True and st["held"][0]["file"] == "emulator-5554.lock"  # type: ignore[index]
    assert DevLock(d).blockers([], heavy=True) == []
    assert DevLock(tmp_path / "nope").status()["active"] is False
    assert os.path.basename(str(d)) == ".device-locks" and time.time() > 0
