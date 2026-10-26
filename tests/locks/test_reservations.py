import threading
from datetime import timedelta

import pytest

from cpm.db.models import Event, utcnow
from cpm.locks.types import ReservationConflict, ResourceRequest


def p(key, mode="excl", label=None):
    return ResourceRequest("path", key, mode, label or key)


def named(t, key=None, mode="excl"):
    return ResourceRequest(t, key or t, mode, key or t)


def test_conflicting_path_reservations_rejected(env):  # scenario 6
    a, b = env.task(), env.task()
    env.res.acquire(a.id, env.pid, [p("assets/game/ui/")])
    with pytest.raises(ReservationConflict) as e:
        env.res.acquire(b.id, env.pid, [p("assets/game/ui/menu.prefab")])
    assert e.value.blockers[0].holder_task_id == a.id
    assert "blocked by" in str(e.value) and f"task #{a.id}" in str(e.value)
    assert env.res.active(task_id=b.id) == []


def test_all_or_nothing(env):
    a, b = env.task(), env.task()
    env.res.acquire(a.id, env.pid, [p("x/shared.txt")])
    with pytest.raises(ReservationConflict):
        env.res.acquire(b.id, env.pid, [p("y/free.txt"), p("x/shared.txt")])
    assert env.res.active(task_id=b.id) == []  # the free part was not kept


def test_disjoint_and_shared_are_compatible(env):
    a, b, c = env.task(), env.task(), env.task()
    env.res.acquire(a.id, env.pid, [p("a/"), p("lib/", "shared")])
    env.res.acquire(b.id, env.pid, [p("b/"), p("lib/", "shared")])
    with pytest.raises(ReservationConflict):
        env.res.acquire(c.id, env.pid, [p("lib/core.py")])  # exclusive vs shared


def test_projects_are_isolated_for_paths_but_not_for_machine_resources(env):
    other = env.other_project()
    a, b = env.task(), env.task(project_id=other)
    env.res.acquire(a.id, env.pid, [p("same/path.txt")])
    env.res.acquire(b.id, other, [p("same/path.txt")])  # different repo: fine
    c, d = env.task(), env.task(project_id=other)
    env.res.acquire(c.id, env.pid, [named("unity_editor")])
    with pytest.raises(ReservationConflict):
        env.res.acquire(d.id, other, [named("unity_editor")])


def test_only_one_heavy_task_at_a_time(env):
    a, b, c = env.task(), env.task(), env.task()
    env.res.acquire(a.id, env.pid, [named("unity_editor")])
    with pytest.raises(ReservationConflict) as e:
        env.res.acquire(b.id, env.pid, [named("blender")])  # different heavy type, same class
    assert e.value.blockers[0].kind == "heavy"
    env.res.acquire(c.id, env.pid, [p("docs/")])  # light work is unaffected


def test_reacquire_replaces_own_set_and_release_is_idempotent(env):
    a = env.task()
    env.res.acquire(a.id, env.pid, [p("a/")])
    env.res.acquire(a.id, env.pid, [p("a/"), p("b/")])
    assert len(env.res.active(task_id=a.id)) == 2
    assert env.res.release(a.id) == 2
    assert env.res.release(a.id) == 0
    b = env.task()
    env.res.acquire(b.id, env.pid, [p("a/")])


def test_expired_lease_of_dead_holder_is_reclaimed(env):
    a, b = env.task(), env.task()
    env.res.acquire(a.id, env.pid, [p("a/")], lease_seconds=1)
    env.res.clock = lambda: utcnow() + timedelta(seconds=5)
    env.res.acquire(b.id, env.pid, [p("a/")])  # holder dead: lease reclaimed
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    with Session(env.engine) as s:
        kinds = [e.kind for e in s.scalars(select(Event).where(Event.task_id == a.id))]
    assert "reservation.expired" in kinds


def test_expired_lease_of_live_holder_is_renewed_not_taken(env):
    a, b = env.task(), env.task()
    env.res.acquire(a.id, env.pid, [p("a/")], lease_seconds=1)
    env.live.add(a.id)
    env.res.clock = lambda: utcnow() + timedelta(seconds=5)
    with pytest.raises(ReservationConflict):
        env.res.acquire(b.id, env.pid, [p("a/")])
    assert env.res.check(b.id, env.pid, [p("a/")])  # dry run agrees


def test_heartbeat_extends_lease(env):
    a = env.task()
    env.res.acquire(a.id, env.pid, [p("a/")], lease_seconds=1)
    assert env.res.heartbeat(a.id, lease_seconds=300) == 1
    assert env.res.heartbeat(env.task().id) == 0
    env.res.clock = lambda: utcnow() + timedelta(seconds=60)
    env.res.sweep()
    assert env.res.active(task_id=a.id)


def test_request_count_is_bounded(env):
    a = env.task()
    with pytest.raises(ValueError):
        env.res.acquire(a.id, env.pid, [p(f"f{i}.txt") for i in range(201)])


def test_stress_50_threads_overlapping_sets_never_double_grant(env):
    """Overlapping sets from 50 threads: every granted pair is disjoint, and the winners hold
    exactly what they asked for (no half grants)."""
    sets = [
        [p(f"zone{i % 7}/"), p(f"zone{(i + 1) % 7}/sub/f{i}.txt"), p(f"solo{i}.txt")]
        for i in range(50)
    ]
    tasks = [env.task() for _ in range(50)]
    results: dict[int, bool] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(50)

    def run(i: int) -> None:
        try:
            barrier.wait()
            env.res.acquire(tasks[i].id, env.pid, sets[i])
            results[i] = True
        except ReservationConflict:
            results[i] = False
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(50)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert not errors
    assert len(results) == 50
    winners = [i for i, ok in results.items() if ok]
    assert winners  # someone always wins
    from cpm.locks.normalize import overlaps

    for x in winners:
        assert len(env.res.active(task_id=tasks[x].id)) == 3
        for y in winners:
            if x < y:
                assert not any(overlaps(a.key, b.key) for a in sets[x] for b in sets[y]), (x, y)
    for i, ok in results.items():
        if not ok:
            assert env.res.active(task_id=tasks[i].id) == []
