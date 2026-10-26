import pytest

from cpm.tasks import states
from cpm.tasks.service import IllegalTransition

GATE = {"acceptance_gate": "passed"}


def test_all_fourteen_statuses_have_rows():
    assert len(states.STATUSES) == 14
    assert set(states.TRANSITIONS) == set(states.STATUSES)
    for src, dsts in states.TRANSITIONS.items():
        assert dsts <= set(states.STATUSES), src


def test_terminal_states_have_no_exit():
    for t in states.TERMINAL:
        assert states.TRANSITIONS[t] == frozenset()


def test_only_reviewing_can_complete():
    for src, dsts in states.TRANSITIONS.items():
        assert ("COMPLETED" in dsts) == (src == "REVIEWING")


def test_every_active_state_can_be_cancelled_or_stopped():
    for src in states.ACTIVE:
        assert "CANCELLED" in states.TRANSITIONS[src]
        assert "FAILED" in states.TRANSITIONS[src]


def test_happy_path_and_events(svc):
    t = svc.create(svc.pid, "do it")
    assert t.status == "BACKLOG"
    for to in ("READY", "RESERVED", "RUNNING", "TESTING", "REVIEWING"):
        svc.transition(t.id, to, actor="scheduler")
    done = svc.transition(t.id, "COMPLETED", actor="gate", evidence=GATE)
    assert done.completed_at is not None
    svc.transition(t.id, "INTEGRATED", actor="git")
    kinds = [e["kind"] for e in svc.events(t.id)]
    assert kinds.count("task.transition") == 7 and "task.created" in kinds


@pytest.mark.parametrize(
    "path,bad",
    [
        (["READY"], "RUNNING"),  # must reserve first
        ([], "COMPLETED"),
        ([], "RUNNING"),
        (["CANCELLED"], "READY"),  # terminal
        (["READY", "RESERVED", "RUNNING"], "COMPLETED"),  # gate states skipped
    ],
)
def test_illegal_transitions_rejected(svc, path, bad):
    t = svc.create(svc.pid, "x")
    for to in path:
        svc.transition(t.id, to, actor="t", evidence=GATE)
    with pytest.raises(IllegalTransition):
        svc.transition(t.id, bad, actor="t", evidence=GATE)
    assert svc.get(t.id).status == (path[-1] if path else "BACKLOG")


def test_no_task_silently_completed(svc):
    """Invariant (scenario 20, partial): COMPLETED is unreachable without gate evidence."""
    t = svc.create(svc.pid, "x")
    for to in ("READY", "RESERVED", "RUNNING", "TESTING", "REVIEWING"):
        svc.transition(t.id, to, actor="t")
    for ev in (None, {}, {"acceptance_gate": "failed"}, {"acceptance_gate": True}):
        with pytest.raises(IllegalTransition):
            svc.transition(t.id, "COMPLETED", actor="t", evidence=ev)
    assert svc.get(t.id).status == "REVIEWING"
    svc.transition(t.id, "COMPLETED", actor="gate", evidence=GATE)


def test_failed_retry_counts_and_clears(svc):
    t = svc.create(svc.pid, "x")
    for to in ("READY", "RESERVED", "FAILED", "READY"):
        t = svc.transition(t.id, to, actor="t")
    assert t.retry_count == 1 and t.block_reason is None


def test_cas_prevents_double_win(svc, monkeypatch):
    """A transition decided on a stale status is refused instead of overwriting."""
    from sqlalchemy.orm import Session

    from cpm.db.models import Task

    t = svc.create(svc.pid, "x")
    orig = svc._deps_done

    def racing(s, tid):
        ok = orig(s, tid)
        with Session(svc.engine) as other:  # another caller wins between check and UPDATE
            other.get(Task, tid).status = "CANCELLED"
            other.commit()
        return ok

    monkeypatch.setattr(svc, "_deps_done", racing)
    with pytest.raises(IllegalTransition):
        svc.transition(t.id, "READY", actor="t")
    monkeypatch.undo()
    assert svc.get(t.id).status == "CANCELLED"
