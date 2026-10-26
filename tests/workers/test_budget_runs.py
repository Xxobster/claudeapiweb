"""WP8 acceptance through the real supervisor: scenarios 8, 9 and 10 with mock workers."""

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from cpm.db.models import Event, Project
from cpm.db.settings_repo import get_setting, set_setting
from cpm.workers.supervisor import BudgetRefused, RunRefused
from tests.fixtures.repos import make_repo

USD = 1_000_000


def cfg(env, month=10, project=10, task=10):
    set_setting(
        env.engine, "wizard.config",
        {"global_budget_microusd": month * USD, "project_budget_microusd": project * USD,
         "task_budget_microusd": task * USD},
    )  # fmt: skip


def second_project(env, tmp_path):
    repo = make_repo(tmp_path / "other repo")
    roots = get_setting(env.engine, "approved_roots", [])
    set_setting(env.engine, "approved_roots", [*roots, str(repo)])
    with Session(env.engine) as s:
        p = Project(slug="other", name="Other", path=str(repo))
        s.add(p)
        s.commit()
        return p.id


def test_scenario_8_task_budget_exhausted_pauses_and_releases_the_hold(env):
    cfg(env, task=1)
    t = env.new_task()
    run = env.sup.wait(
        env.sup.start(
            t.id,
            scenario="budget_burst",
            scenario_args={"step_microusd": 400_000, "steps": 50},
            max_budget_microusd=USD,
        ).id  # fmt: skip
    )
    assert run.status == "BUDGET_STOPPED" and env.tasks.get(t.id).status == "PAUSED_BUDGET"
    assert env.sup.ledger.reservations() == []  # the hold ended with the run
    with Session(env.engine) as s:
        kinds = [e.kind for e in s.scalars(select(Event).where(Event.task_id == t.id))]
    assert "budget.exhausted" in kinds and "budget.threshold" in kinds


def test_scenario_9_resume_does_not_reset_cumulative_spend(env):
    cfg(env, task=2)
    t = env.new_task()
    run1 = env.sup.wait(
        env.sup.start(
            t.id,
            scenario="budget_burst",
            scenario_args={"step_microusd": 600_000, "steps": 50},
            max_budget_microusd=USD,
        ).id  # fmt: skip
    )
    assert run1.status == "BUDGET_STOPPED"
    spent1 = env.sup.ledger.task_spent(t.id)
    assert spent1 == 1_200_000
    # resuming asks for a new hold; the old spend still counts against the 2 USD task cap
    with pytest.raises(BudgetRefused) as e:
        env.sup.start(t.id, max_budget_microusd=USD)
    assert e.value.detail["scope"] == "task" and e.value.detail["remaining_microusd"] == 800_000
    assert env.tasks.get(t.id).status == "PAUSED_BUDGET"  # a refusal moves nothing
    # a smaller hold fits; the run continues the same task and the total keeps growing
    run2 = env.sup.wait(env.sup.start(t.id, max_budget_microusd=800_000).id)
    assert run2.status == "SUCCEEDED"
    assert env.sup.ledger.task_spent(t.id) == spent1 + 1_000


def test_scenario_10_two_projects_share_one_global_budget(env, tmp_path):
    cfg(env, month=1, project=1, task=1)
    other = second_project(env, tmp_path)
    ta = env.new_task("A")
    tb = env.tasks.mark_ready(env.tasks.create(other, "B", billing_mode="mock").id, actor="test")
    a = env.sup.start(
        ta.id, scenario="slow", scenario_args={"steps": 500}, max_budget_microusd=600_000
    )
    with pytest.raises(BudgetRefused) as e:  # two concurrent workers, one wallet
        env.sup.start(tb.id, max_budget_microusd=600_000)
    assert e.value.detail["scope"] == "month" and e.value.detail["remaining_microusd"] == 400_000
    assert env.tasks.get(tb.id).status == "READY"
    env.sup.stop(a.id, "cancel")
    env.sup.wait(a.id)
    assert env.sup.ledger.reservations() == []
    env.sup.wait(env.sup.start(tb.id, max_budget_microusd=600_000).id)  # the hold was released


def test_mock_run_without_a_cap_or_config_skips_admission(env):
    t = env.new_task()
    assert env.sup.wait(env.sup.start(t.id).id).status == "SUCCEEDED"
    cfg(env)
    t2 = env.new_task("two")
    assert env.sup.wait(env.sup.start(t2.id).id).status == "SUCCEEDED"  # no cap given: free mock


def test_api_run_without_a_budget_is_refused_before_anything_moves(env):
    import dataclasses

    cfg(env)
    env.sup.settings = dataclasses.replace(env.settings, allow_live_runs=True)
    env.store.set("anthropic_api_key", "sk-ant-api03-" + "a" * 40)
    t = env.new_task("live", billing_mode="api")
    with pytest.raises(RunRefused, match="explicit per-run budget"):
        env.sup.start(t.id)
    assert env.sup.ledger.reservations() == []


def test_api_runs_are_priced_from_the_table_and_ignore_a_worker_stated_cost(env):
    from types import SimpleNamespace

    live = SimpleNamespace(
        billing_mode="api", run_id=0, task_id=0, project_id=0, cost_microusd=0,
        cap_microusd=None, intent=None, log=SimpleNamespace(append=lambda *a, **k: None),
    )  # fmt: skip
    t = env.new_task()
    run = env.sup.wait(env.sup.start(t.id).id)
    live.run_id, live.task_id, live.project_id = run.id, t.id, env.pid
    env.sup._record_usage(
        live,
        {"message_id": "api-1", "model": "claude-sonnet-5-5", "input_tokens": 1000,
         "output_tokens": 500, "cost_microusd": 1},  # a lying worker
    )  # fmt: skip
    assert live.cost_microusd == 7000  # priced by CPM, not by the worker
    env.sup._record_usage(live, {"message_id": "api-2", "input_tokens": -5})
    env.sup._record_usage(live, {"message_id": "", "input_tokens": 5})
    assert live.cost_microusd == 7000  # malformed events are ignored
