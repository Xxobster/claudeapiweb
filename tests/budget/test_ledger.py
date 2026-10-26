import random
import threading
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cpm.budget.ledger import BudgetDenied, BudgetError, BudgetLedger, month_bounds, period_of
from cpm.db.engine import make_engine
from cpm.db.migrate import upgrade
from cpm.db.models import Event, Project, Run, Task, UsageEvent, utcnow
from cpm.db.settings_repo import set_setting

USD = 1_000_000


@pytest.fixture
def led(tmp_path):
    db = tmp_path / "d" / "cpm.db"
    db.parent.mkdir()
    upgrade(f"sqlite:///{db.as_posix()}")
    engine = make_engine(db)
    with Session(engine) as s:
        for i in (1, 2):
            s.add(Project(slug=f"p{i}", name=f"P{i}", path=str(tmp_path / f"p{i}")))
        s.commit()
        for i in range(1, 7):
            s.add(Task(project_id=1 if i <= 3 else 2, title=f"t{i}", status="READY"))
        s.commit()
    ledger = BudgetLedger(engine)
    ledger.engine_ = engine  # type: ignore[attr-defined]
    set_cfg(engine)
    yield ledger
    engine.dispose()


def set_cfg(engine, month=10, project=6, task=3):
    set_setting(
        engine, "wizard.config",
        {"global_budget_microusd": month * USD, "project_budget_microusd": project * USD,
         "task_budget_microusd": task * USD},
    )  # fmt: skip


def add_run(engine, task_id):
    with Session(engine) as s:
        n = s.scalar(select(func.count()).select_from(Run).where(Run.task_id == task_id))
        r = Run(task_id=task_id, attempt=(n or 0) + 1, billing_mode="mock", status="RUNNING")
        s.add(r)
        s.commit()
        return r.id


def spend(led, run_id, task_id, project_id, micro, mid):
    return led.record_usage(
        run_id=run_id, task_id=task_id, project_id=project_id, message_id=mid, model="m",
        input_tokens=1, output_tokens=1, cache_read=0, cache_write=0, cost_microusd=micro,
    )  # fmt: skip


def test_admit_holds_and_denies_with_an_explanation(led):
    r = led.admit(1, 1, 2 * USD)
    assert r.amount_microusd == 2 * USD
    with pytest.raises(BudgetDenied) as e:
        led.admit(1, 1, 2 * USD)  # task cap 3 USD, 2 already held
    assert e.value.detail["scope"] == "task" and e.value.detail["remaining_microusd"] == USD
    assert led.release(r.id) is True and led.release(r.id) is False  # released once
    led.admit(1, 1, 3 * USD)


def test_no_cap_means_no_admission(tmp_path):
    db = tmp_path / "d" / "cpm.db"
    db.parent.mkdir()
    upgrade(f"sqlite:///{db.as_posix()}")
    engine = make_engine(db)
    ledger = BudgetLedger(engine)
    assert ledger.configured() is False
    with Session(engine) as s:
        s.add(Project(slug="p", name="P", path="x"))
        s.commit()
        s.add(Task(project_id=1, title="t"))
        s.commit()
    with pytest.raises(BudgetDenied, match="no monthly budget"):
        ledger.admit(1, 1, 1)
    engine.dispose()


def test_spent_money_counts_against_every_scope(led):
    eng = led.engine
    run = add_run(eng, 1)
    spend(led, run, 1, 1, 2 * USD, "a")
    with pytest.raises(BudgetDenied) as e:
        led.admit(1, 1, 2 * USD)  # task: 2 spent + 2 > 3
    assert e.value.detail["scope"] == "task"
    led.admit(2, 1, 3 * USD)  # another task, same project: ok
    with pytest.raises(BudgetDenied) as e2:
        led.admit(3, 1, 3 * USD)  # project cap 6: 2 spent + 3 held + 3 > 6
    assert e2.value.detail["scope"] == "project"


def test_hold_and_spend_of_the_same_run_are_not_counted_twice(led):
    eng = led.engine
    set_cfg(eng, month=10, project=10, task=10)
    res = led.admit(1, 1, 3 * USD)
    run = add_run(eng, 1)
    led.attach_run(res.id, run)
    spend(led, run, 1, 1, 1 * USD, "a")
    # month 10: spent 1 + still-held 2 = 3, so 7 more fits exactly, 7.000001 does not
    led.admit(4, 2, 7 * USD)
    with pytest.raises(BudgetDenied):
        led.admit(5, 2, 1)


def test_dedup_counts_a_message_once(led):
    run = add_run(led.engine, 1)
    assert spend(led, run, 1, 1, 500, "same") == 500
    assert spend(led, run, 1, 1, 500, "same") is None
    assert led.task_spent(1) == 500


def test_resume_keeps_cumulative_spend_scenario_9(led):
    eng = led.engine
    r1 = led.admit(1, 1, 2 * USD)
    run1 = add_run(eng, 1)
    led.attach_run(r1.id, run1)
    spend(led, run1, 1, 1, 2 * USD, "a")
    led.release_run(run1)
    # second attempt: the first 2 USD still count, so only 1 USD of the 3 USD task cap is left
    with pytest.raises(BudgetDenied) as e:
        led.admit(1, 1, 2 * USD)
    assert e.value.detail["remaining_microusd"] == USD
    assert led.task_spent(1) == 2 * USD
    # the user adds 2 USD to the task: now 5 cap, 2 spent
    assert led.add_to_cap("task", "1", 2 * USD, project_id=1, task_id=1) == 5 * USD
    led.admit(1, 1, 3 * USD)
    assert led.task_spent(1) == 2 * USD  # nothing was reset


def test_global_cap_is_shared_across_projects_scenario_10(led):
    set_cfg(led.engine, month=1, project=1, task=1)
    led.admit(1, 1, 600_000)
    with pytest.raises(BudgetDenied) as e:
        led.admit(4, 2, 600_000)  # other project, same month
    assert e.value.detail["scope"] == "month"
    assert e.value.detail["remaining_microusd"] == 400_000
    led.admit(4, 2, 400_000)


@pytest.mark.parametrize("seed", range(5))
def test_property_holds_never_exceed_the_cap_under_concurrency(led, seed):
    """N threads race to reserve overlapping money; the sum held can never pass the cap."""
    rnd = random.Random(seed)  # noqa: S311
    set_cfg(led.engine, month=2, project=2, task=2)
    cap = 2 * USD
    granted: list[int] = []
    lock = threading.Lock()
    amounts = [rnd.randint(50_000, 700_000) for _ in range(24)]

    def worker(i: int, amt: int) -> None:
        try:
            r = led.admit(1 + i % 6, 1 + (i % 6) // 3, amt)
        except BudgetDenied:
            return
        with lock:
            granted.append(r.amount_microusd)

    threads = [threading.Thread(target=worker, args=(i, a)) for i, a in enumerate(amounts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    held = sum(r["amount_microusd"] for r in led.reservations())
    assert held == sum(granted) <= cap
    assert led.summary()["month"]["remaining_microusd"] == cap - held


def test_property_random_admit_release_spend_never_overcommits(led):
    rnd = random.Random(7)  # noqa: S311
    cap = 10 * USD
    runs: dict[int, int] = {}  # reservation id -> run id
    for step in range(120):
        op = rnd.choice(["admit", "admit", "spend", "release"])
        if op == "admit":
            t = rnd.randint(1, 6)
            try:
                r = led.admit(t, 1 if t <= 3 else 2, rnd.randint(1, 3 * USD))
            except BudgetDenied:
                continue
            run = add_run(led.engine, t)
            led.attach_run(r.id, run)
            runs[r.id] = run
        elif op == "spend" and runs:
            rid = rnd.choice(list(runs))
            held = {h["id"]: h for h in led.reservations()}.get(rid)
            if held:
                spend(led, runs[rid], held["task_id"], held["project_id"],
                      rnd.randint(1, 500_000), f"s{step}")  # fmt: skip
        elif op == "release" and runs:
            led.release(rnd.choice(list(runs)))
        m = led.summary()["month"]
        # spent can exceed a hold (a worker overshoots by one message), but a hold is never
        # granted on top of money that is not there
        assert m["spent_microusd"] + m["held_microusd"] <= cap + 3 * USD


def test_thresholds_fire_once_per_level_and_restart_after_top_up(led):
    run = add_run(led.engine, 1)
    set_cfg(led.engine, month=100, project=100, task=3)
    spend(led, run, 1, 1, 2_200_000, "a")  # 73 % of the 3 USD task cap
    got = led.check_thresholds(1, 1)
    assert [(a.scope, a.level) for a in got] == [("task", 70)]
    assert led.check_thresholds(1, 1) == []  # not repeated
    spend(led, run, 1, 1, 600_000, "b")  # 93 %
    got = led.check_thresholds(1, 1)
    assert [(a.level, a.checkpoint_requested) for a in got] == [(90, True)]
    spend(led, run, 1, 1, 300_000, "c")  # 103 %
    assert [a.level for a in led.check_thresholds(1, 1)] == [100]
    led.add_to_cap("task", "1", 10 * USD, project_id=1, task_id=1)
    spend(led, run, 1, 1, 9_000_000, "d")  # 12.1 of 13 -> 93 %: a new series
    assert [a.level for a in led.check_thresholds(1, 1)] == [90]
    with Session(led.engine) as s:
        kinds = [e.kind for e in s.scalars(select(Event))]
    assert kinds.count("budget.threshold") >= 4


def test_credit_balance_is_an_extra_ceiling_and_an_estimate(led):
    run = add_run(led.engine, 1)
    led.set_credit_balance(3 * USD)
    spend(led, run, 1, 1, USD, "a")
    with pytest.raises(BudgetDenied) as e:
        led.admit(2, 1, 3 * USD)  # 3 entered - 1 spent = 2 left
    assert e.value.detail["scope"] == "credit"
    led.admit(2, 1, 2 * USD)
    cb = led.summary()["credit_balance"]
    assert cb["estimated_remaining_microusd"] == 2 * USD and "estimate" in cb["label"]
    led.clear_credit_balance()
    assert led.summary()["credit_balance"] is None


def test_reconcile_corrects_the_month_and_marks_rows(led):
    set_cfg(led.engine, month=10, project=10, task=10)
    run = add_run(led.engine, 1)
    spend(led, run, 1, 1, 2 * USD, "a")
    s0 = led.summary()["month"]
    assert "estimate" in s0["label"] and s0["spent_microusd"] == 2 * USD
    rec = led.reconcile(period_of(), 5 * USD)  # the Console says 5 USD
    assert rec["adjustment_microusd"] == 3 * USD
    s1 = led.summary()["month"]
    assert s1["spent_microusd"] == 5 * USD and s1["reconciled_rows_microusd"] == 2 * USD
    assert "reconciled" in s1["label"]
    with pytest.raises(BudgetDenied):
        led.admit(2, 1, 6 * USD)  # only 5 of 10 left now, project cap 6 not the limit
    led.admit(2, 1, 5 * USD)


def test_topup_limits_and_validation(led):
    with pytest.raises(BudgetError):
        led.add_to_cap("task", "1", 0, project_id=1, task_id=1)
    with pytest.raises(BudgetError):
        led.add_to_cap("task", "1", 10**11, project_id=1, task_id=1)
    with pytest.raises(BudgetError):
        month_bounds("2026-13")
    assert month_bounds("2026-12")[1].year == 2027


def test_release_orphans_frees_dead_runs_only(led):
    a = led.admit(1, 1, USD)
    b = led.admit(2, 1, USD)
    run_a, run_b = add_run(led.engine, 1), add_run(led.engine, 2)
    led.attach_run(a.id, run_a)
    led.attach_run(b.id, run_b)
    assert led.release_orphans({run_a}) == 1
    assert [r["id"] for r in led.reservations()] == [a.id]


def test_usage_rows_are_priced_from_the_table_when_no_cost_given(led):
    run = add_run(led.engine, 1)
    c = led.record_usage(
        run_id=run, task_id=1, project_id=1, message_id="x", model="claude-sonnet-5-5",
        input_tokens=1000, output_tokens=500, cache_read=0, cache_write=0, cost_microusd=None,
    )  # fmt: skip
    assert c == 7000
    with Session(led.engine) as s:
        u = s.scalars(select(UsageEvent)).one()
        assert u.price_version == 1 and u.reconciled is False
        assert utcnow() - u.ts.replace(tzinfo=utcnow().tzinfo) < timedelta(minutes=1)
