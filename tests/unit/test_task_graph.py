import pytest

from cpm.tasks.service import CycleError, IllegalTransition, InvalidTask, NotFound

GATE = {"acceptance_gate": "passed"}


def finish(svc, tid):
    if svc.get(tid).status != "READY":
        svc.transition(tid, "READY", actor="t")
    for to in ("RESERVED", "RUNNING", "TESTING", "REVIEWING"):
        svc.transition(tid, to, actor="t")
    svc.transition(tid, "COMPLETED", actor="gate", evidence=GATE)


def mk(svc, title, **kw):
    return svc.create(svc.pid, title, **kw)


def test_self_and_direct_and_long_cycles_rejected(svc):
    a, b, c = mk(svc, "a"), mk(svc, "b"), mk(svc, "c")
    with pytest.raises(CycleError):
        svc.add_dependency(a.id, a.id, actor="u")
    svc.add_dependency(b.id, a.id, actor="u")
    svc.add_dependency(c.id, b.id, actor="u")
    with pytest.raises(CycleError):
        svc.add_dependency(a.id, b.id, actor="u")
    with pytest.raises(CycleError):
        svc.add_dependency(a.id, c.id, actor="u")
    assert svc.get(a.id).depends_on == []


def test_cycle_via_create_depends_on_impossible_and_missing_dep(svc):
    with pytest.raises(NotFound):
        mk(svc, "x", depends_on=[999])


def test_diamond_is_not_a_cycle(svc):
    a = mk(svc, "a")
    b = mk(svc, "b", depends_on=[a.id])
    c = mk(svc, "c", depends_on=[a.id])
    d = mk(svc, "d", depends_on=[b.id, c.id])
    assert svc.get(d.id).depends_on == sorted([b.id, c.id])


def test_deep_chain_no_recursion_limit(svc):
    prev = mk(svc, "t0")
    first = prev
    for i in range(1, 300):
        prev = mk(svc, f"t{i}", depends_on=[prev.id])
    with pytest.raises(CycleError):
        svc.add_dependency(first.id, prev.id, actor="u")


def test_cross_project_dependency_rejected(svc):
    from sqlalchemy.orm import Session

    from cpm.db.models import Project

    with Session(svc.engine) as s:
        p2 = Project(slug="other", name="O", path="/x")
        s.add(p2)
        s.commit()
        pid2 = p2.id
    other = svc.create(pid2, "o")
    with pytest.raises(InvalidTask):
        mk(svc, "a", depends_on=[other.id])


def test_ready_queue_order_and_dependency_gating(svc):
    a = mk(svc, "a", priority=50)
    b = mk(svc, "b", priority=10, depends_on=[a.id])
    c = mk(svc, "c", priority=10)
    d = mk(svc, "d", priority=10)
    for t in (a, c, d):
        svc.mark_ready(t.id, actor="u")
    assert svc.mark_ready(b.id, actor="u").status == "BLOCKED"
    assert [t.id for t in svc.ready_queue(svc.pid)] == [c.id, d.id, a.id]  # priority, then id
    assert svc.get(b.id).block_reason.startswith("waiting")
    finish(svc, a.id)
    assert svc.unblock_ready(svc.pid) == [b.id]
    assert [t.id for t in svc.ready_queue(svc.pid)][0] == b.id  # prio 10, lowest id of prio 10


def test_ready_requires_deps_done(svc):
    a = mk(svc, "a")
    b = mk(svc, "b", depends_on=[a.id])
    with pytest.raises(IllegalTransition):
        svc.transition(b.id, "READY", actor="u")


def test_cancelling_a_dependency_blocks_dependents(svc):
    a = mk(svc, "a")
    b = mk(svc, "b", depends_on=[a.id])
    svc.cancel(a.id, actor="u", reason="nope")
    assert svc.get(b.id).status == "BLOCKED"
    assert "cancelled" in svc.get(b.id).block_reason
    assert svc.mark_ready(b.id, actor="u").status == "BLOCKED"  # stays blocked, no error


def test_add_dependency_demotes_ready_task(svc):
    a, b = mk(svc, "a"), mk(svc, "b")
    svc.mark_ready(b.id, actor="u")
    assert svc.add_dependency(b.id, a.id, actor="u").status == "BLOCKED"
    assert svc.ready_queue(svc.pid) == []


def test_dependencies_locked_while_running(svc):
    a, b = mk(svc, "a"), mk(svc, "b")
    for to in ("READY", "RESERVED", "RUNNING"):
        svc.transition(b.id, to, actor="t")
    with pytest.raises(IllegalTransition):
        svc.add_dependency(b.id, a.id, actor="u")


def test_remove_dependency(svc):
    a = mk(svc, "a")
    b = mk(svc, "b", depends_on=[a.id])
    assert svc.remove_dependency(b.id, a.id, actor="u").depends_on == []


# ---- edit / reprioritize / reassign
def test_edit_rules(svc):
    t = mk(svc, "t", declared_scope=["src/a.py"])
    t = svc.edit(t.id, actor="u", title="  new  ", priority=5, declared_scope=["src/b/"])
    assert (t.title, t.priority, t.declared_scope) == ("new", 5, ["src/b/"])
    assert svc.reprioritize(t.id, 7, actor="u").priority == 7
    assert svc.reassign(t.id, actor="u", model="claude-haiku-5-5", effort="low").effort == "low"
    for to in ("READY", "RESERVED", "RUNNING"):
        svc.transition(t.id, to, actor="t")
    svc.edit(t.id, actor="u", title="still ok", description="d")
    for locked in ({"declared_scope": ["x"]}, {"model": "m"}, {"billing_mode": "mock"}):
        with pytest.raises(IllegalTransition):
            svc.edit(t.id, actor="u", **locked)
    with pytest.raises(InvalidTask):
        svc.edit(t.id, actor="u", status="COMPLETED")  # status is not editable


def test_edit_closed_tasks_refused(svc):
    t = mk(svc, "t")
    svc.cancel(t.id, actor="u")
    with pytest.raises(IllegalTransition):
        svc.edit(t.id, actor="u", title="x")


@pytest.mark.parametrize("kw", [{"priority": -1}, {"priority": 10_001}, {"effort": "turbo"}])
def test_validation(svc, kw):
    with pytest.raises(InvalidTask):
        mk(svc, "t", **kw)


def test_title_cleaning(svc):
    assert mk(svc, "a\x00b\x07c\nd").title == "abc d"
    with pytest.raises(InvalidTask):
        mk(svc, " \x00 ")
    with pytest.raises(InvalidTask):
        mk(svc, "x" * 301)


# ---- split / merge
def test_split_rewires_graph(svc):
    d = mk(svc, "dep")
    t = mk(svc, "big", depends_on=[d.id], declared_scope=["src/"], priority=3)
    after = mk(svc, "after", depends_on=[t.id])
    parts = svc.split(
        t.id, [{"title": "p1"}, {"title": "p2", "declared_scope": ["src/x.py"]}], actor="u"
    )
    assert [p.parent_id for p in parts] == [t.id, t.id]
    assert all(p.depends_on == [d.id] and p.priority == 3 for p in parts)
    assert parts[0].declared_scope == ["src/"] and parts[1].declared_scope == ["src/x.py"]
    assert svc.get(t.id).status == "CANCELLED"
    assert svc.get(after.id).depends_on == sorted(p.id for p in parts)


def test_split_needs_two_parts_and_idle_task(svc):
    t = mk(svc, "t")
    with pytest.raises(InvalidTask):
        svc.split(t.id, [{"title": "only"}], actor="u")
    for to in ("READY", "RESERVED", "RUNNING"):
        svc.transition(t.id, to, actor="t")
    with pytest.raises(IllegalTransition):
        svc.split(t.id, [{"title": "a"}, {"title": "b"}], actor="u")


def test_split_bad_scope_rolls_back(svc):
    t = mk(svc, "t")
    with pytest.raises(InvalidTask):
        svc.split(t.id, [{"title": "a"}, {"title": "b", "declared_scope": ["../x"]}], actor="u")
    assert svc.get(t.id).status == "BACKLOG"
    assert len(svc.list_tasks(svc.pid)) == 1


def test_merge(svc):
    d = mk(svc, "d")
    a = mk(svc, "a", depends_on=[d.id], declared_scope=["a/"], acceptance=["x"], priority=9)
    b = mk(svc, "b", declared_scope=["b/", "a/"], acceptance=["x", "y"], priority=4)
    after = mk(svc, "after", depends_on=[b.id])
    m = svc.merge([a.id, b.id], "ab", actor="u", project_id=svc.pid)
    assert m.declared_scope == ["a/", "b/"] and m.acceptance == ["x", "y"] and m.priority == 4
    assert m.depends_on == [d.id]
    assert svc.get(after.id).depends_on == [m.id]
    assert svc.get(a.id).status == svc.get(b.id).status == "CANCELLED"


def test_merge_that_would_cycle_rolls_back(svc):
    x = mk(svc, "x")
    a = mk(svc, "a", depends_on=[x.id])
    b = mk(svc, "b")
    svc.add_dependency(x.id, b.id, actor="u")  # b <- x <- a ; merging a,b makes x<->ab
    with pytest.raises(CycleError):
        svc.merge([a.id, b.id], "ab", actor="u", project_id=svc.pid)
    assert svc.get(a.id).status == "BACKLOG" and svc.get(b.id).status == "BACKLOG"
    assert len(svc.list_tasks(svc.pid)) == 3


def test_merge_validation(svc):
    a = mk(svc, "a")
    with pytest.raises(InvalidTask):
        svc.merge([a.id, a.id], "x", actor="u", project_id=svc.pid)
