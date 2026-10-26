import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cpm.db.models import Event, Project
from cpm.locks.claims import DesktopClaims
from cpm.locks.types import ReservationConflict, ResourceRequest
from cpm.tasks.service import IllegalTransition


def heavy(name="unity_editor"):
    return ResourceRequest(name, name, "excl", name)


def blocked_events(env, task_id):
    with Session(env.engine) as s:
        return [
            e for e in s.scalars(select(Event).where(Event.task_id == task_id))
            if e.kind == "reservation.blocked"
        ]  # fmt: skip


def use_profile(env, name="nirandyr"):
    with Session(env.engine) as s:
        s.get(Project, env.pid).profile = name
        s.commit()


def test_reserve_moves_ready_to_reserved_and_holds_scope(env):
    t = env.task("src/a/", "docs/x.md", ready=True)
    out = env.adm.reserve(t.id)
    assert out.status == "RESERVED"
    keys = {r.key for r in env.res.active(task_id=t.id)}
    assert keys == {"src/a/", "docs/x.md"}
    env.adm.release(t.id)
    assert env.res.active(task_id=t.id) == []


def test_conflict_leaves_task_ready_with_one_visible_reason(env):
    a = env.task("src/", ready=True)
    b = env.task("src/deep/f.py", ready=True)
    env.adm.reserve(a.id)
    for _ in range(3):  # three scheduler ticks
        with pytest.raises(ReservationConflict):
            env.adm.reserve(b.id)
    assert env.tasks.get(b.id).status == "READY"
    assert len(blocked_events(env, b.id)) == 1  # not flooded
    reason = env.adm.last_blockers(b.id)
    assert reason and f"task #{a.id}" in reason[0]
    env.adm.release(a.id)
    assert env.adm.reserve(b.id).status == "RESERVED"
    assert env.adm.last_blockers(b.id) == []


def test_desktop_claim_blocks_admission_and_is_explained(env):
    DesktopClaims(env.engine).add(env.pid, "Assets/Game/UI/")
    t = env.task("Assets/Game/UI/menu.prefab", ready=True)
    assert "Desktop claim on `Assets/Game/UI/`" in env.adm.evaluate(t.id)[0].message
    with pytest.raises(ReservationConflict):
        env.adm.reserve(t.id)


def test_only_ready_tasks_can_be_reserved(env):
    t = env.task("a.txt")
    with pytest.raises(IllegalTransition):
        env.adm.reserve(t.id)


def test_invalid_scope_is_blocked_not_crashed(env):
    from cpm.db.models import Task

    t = env.task(ready=True)
    with Session(env.engine) as s:
        s.get(Task, t.id).declared_scope = json.dumps(["../../etc"])
        s.commit()
    with pytest.raises(ReservationConflict) as e:
        env.adm.reserve(t.id)
    assert "invalid scope" in str(e.value)
    assert env.tasks.get(t.id).status == "READY"


def test_build_lock_blocks_heavy_task_but_not_light_one(env, tmp_path, monkeypatch):
    use_profile(env)
    from cpm import profiles

    lock = tmp_path / "BUILD_RUNNING.lock"
    prof = profiles.load_profile("nirandyr").model_copy(
        update={"lock_files": {"build_running": str(lock)}, "lock_dirs": {}}
    )
    monkeypatch.setattr("cpm.locks.admission.load_profile", lambda _n: prof)
    lock.write_text("")
    h = env.task("a.txt", ready=True)
    light = env.task("b.txt", ready=True)
    with pytest.raises(ReservationConflict) as e:
        env.adm.reserve(h.id, [heavy()])
    assert "BUILD_RUNNING.lock" in str(e.value)
    assert env.tasks.get(h.id).status == "READY"
    assert env.adm.reserve(light.id).status == "RESERVED"  # light work continues
    lock.unlink()
    assert env.adm.reserve(h.id, [heavy()]).status == "RESERVED"


def test_gpu_lock_folder_blocks_heavy(env, tmp_path, monkeypatch):
    use_profile(env)
    from cpm import profiles

    d = tmp_path / "gpu-locks"
    d.mkdir()
    (d / "voice-42.lock").write_text("")
    prof = profiles.load_profile("nirandyr").model_copy(
        update={"lock_files": {}, "lock_dirs": {"gpu_locks": str(d)}}
    )
    monkeypatch.setattr("cpm.locks.admission.load_profile", lambda _n: prof)
    t = env.task(ready=True)
    with pytest.raises(ReservationConflict):
        env.adm.reserve(t.id, [heavy("gpu")])


def test_job_board_claim_blocks_admission(env, monkeypatch):
    from tests.fixtures.repos import write

    use_profile(env)
    from cpm import profiles

    prof = profiles.load_profile("nirandyr").model_copy(update={"lock_files": {}, "lock_dirs": {}})
    monkeypatch.setattr("cpm.locks.admission.load_profile", lambda _n: prof)
    write(
        env.root,
        "docs/JOB_BOARD.md",
        "| Job | Owner | Status | Files |\n|--|--|--|--|\n| x | T | active | `src/` |\n",
    )
    t = env.task("src/a.py", ready=True)
    with pytest.raises(ReservationConflict) as e:
        env.adm.reserve(t.id)
    assert "job board claim" in str(e.value)


def test_failed_transition_releases_the_reservation(env):
    t = env.task("a.txt", ready=True)

    def boom(_grant):
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        env.adm.reserve(t.id, on_grant=boom)
    assert env.res.active(task_id=t.id) == []
    assert env.tasks.get(t.id).status == "READY"


def test_shipped_nirandyr_profile_has_the_locks(env):
    from cpm.profiles import load_profile

    prof = load_profile("nirandyr")
    assert set(prof.lock_files) == {"build_running", "blender_running"}
    assert "gpu_locks" in prof.lock_dirs
    assert {"unity_editor", "gpu", "blender", "device"} <= set(prof.heavy_resources)
