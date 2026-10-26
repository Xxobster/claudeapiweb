import time

from cpm.locks.external import ExternalWatcher
from cpm.locks.types import ResourceRequest
from tests.fixtures.repos import git, write


def p(key):
    return ResourceRequest("path", key, "excl", key)


def test_external_modification_of_reserved_path_is_detected(env):  # scenario 7
    t = env.task()
    other = env.task()
    env.res.acquire(t.id, env.pid, [p("src/")])
    env.res.acquire(other.id, env.pid, [p("docs/")])
    w = ExternalWatcher(env.engine)
    first = w.scan(env.pid)
    assert first.baseline and first.conflicts == []
    assert w.scan(env.pid).changed == []  # nothing happened

    write(env.root, "src/new.py", "print(1)\n")  # Desktop edits behind CPM's back
    res = w.scan(env.pid)
    assert res.changed == ["src/new.py"]
    assert [(c.task_id, c.paths) for c in res.conflicts] == [(t.id, ["src/new.py"])]
    events = {e["kind"] for e in env.tasks.events(t.id)}
    assert "external.conflict" in events
    assert not any(e["kind"] == "external.conflict" for e in env.tasks.events(other.id))

    assert w.scan(env.pid).changed == []  # reported once


def test_modification_outside_reservations_is_recorded_without_conflict(env):
    t = env.task()
    env.res.acquire(t.id, env.pid, [p("src/")])
    w = ExternalWatcher(env.engine)
    w.scan(env.pid)
    write(env.root, "README.md", "edited\n")
    res = w.scan(env.pid)
    assert res.changed == ["README.md"] and res.conflicts == []


def test_edit_of_an_already_dirty_file_and_revert_are_seen(env):
    write(env.root, "src/a.py", "1\n")
    w = ExternalWatcher(env.engine)
    t = env.task()
    env.res.acquire(t.id, env.pid, [p("src/a.py")])
    w.scan(env.pid)
    time.sleep(0.01)
    write(env.root, "src/a.py", "22\n")
    assert [c.task_id for c in w.scan(env.pid).conflicts] == [t.id]
    (env.root / "src/a.py").unlink()  # removed again
    assert w.scan(env.pid).changed == ["src/a.py"]


def test_head_move_flags_files_in_scope(env):
    t = env.task()
    env.res.acquire(t.id, env.pid, [p("src/")])
    w = ExternalWatcher(env.engine)
    w.scan(env.pid)
    write(env.root, "src/c.py", "c\n")
    git(env.root, "add", "-A")
    git(env.root, "commit", "-q", "-m", "desktop commit")
    res = w.scan(env.pid)
    assert "src/c.py" in res.changed
    assert [c.task_id for c in res.conflicts] == [t.id]


def test_case_and_meta_spellings_still_match(env):
    t = env.task()
    env.res.acquire(t.id, env.pid, [p("assets/game/hero.prefab")])  # canonical key
    w = ExternalWatcher(env.engine)
    w.scan(env.pid)
    write(env.root, "Assets/Game/Hero.prefab.meta", "guid: 1\n")
    assert [c.task_id for c in w.scan(env.pid).conflicts] == [t.id]
