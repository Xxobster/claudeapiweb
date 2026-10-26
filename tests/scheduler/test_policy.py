from datetime import UTC, datetime, timedelta

from cpm.scheduler.policy import (
    Candidate,
    FailedRun,
    SchedulerConfig,
    failure_fingerprint,
    order_key,
    retry_decision,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
CFG = SchedulerConfig(retry_delay_s=30)


def run(status="FAILED", reason="boom", ago=120):
    return FailedRun(status, reason, NOW - timedelta(seconds=ago))


def test_order_priority_then_id():
    cs = [
        Candidate(3, 1, 100, 0, False),
        Candidate(2, 1, 50, 0, False),
        Candidate(1, 1, 100, 0, False),
    ]
    assert [c.task_id for c in sorted(cs, key=order_key)] == [2, 1, 3]


def test_heavy_tasks_of_equal_priority_run_shortest_estimate_first():
    cs = [Candidate(1, 1, 100, 900, True), Candidate(2, 1, 100, 100, True)]
    assert [c.task_id for c in sorted(cs, key=order_key)] == [2, 1]
    light = [Candidate(1, 1, 100, 900, False), Candidate(2, 1, 100, 100, False)]
    assert [c.task_id for c in sorted(light, key=order_key)] == [1, 2]  # estimate ignored


def test_fingerprint_ignores_numbers():
    a = failure_fingerprint("CRASHED", "worker died (exit 3221225477)")
    b = failure_fingerprint("CRASHED", "Worker died (exit  139)")
    assert a == b and a != failure_fingerprint("HUNG", "worker died (exit 139)")


def test_retry_after_delay_only():
    assert retry_decision([run(ago=5)], 0, "mock", CFG, NOW).action == "wait"
    assert retry_decision([run(ago=60)], 0, "mock", CFG, NOW).action == "retry"


def test_retry_bounds():
    assert retry_decision([run()], 2, "mock", CFG, NOW).action == "give_up"
    assert retry_decision([], 0, "mock", CFG, NOW).action == "give_up"


def test_identical_failures_stop_retrying():
    same = [run(reason="exit 1"), run(reason="exit 2")]
    d = retry_decision(same, 1, "mock", CFG, NOW)
    assert d.action == "give_up" and "same failure" in d.why
    differ = [run(reason="timeout"), run("HUNG", "no heartbeat")]
    assert retry_decision(differ, 1, "mock", CFG, NOW).action == "retry"


def test_api_tasks_are_not_retried_unless_opted_in():
    assert retry_decision([run()], 0, "api", CFG, NOW).action == "give_up"
    opt = SchedulerConfig(retry_billing_modes=("mock", "api"), retry_delay_s=0)
    assert retry_decision([run()], 0, "api", opt, NOW).action == "retry"


def test_config_is_clamped_and_survives_garbage():
    assert SchedulerConfig.load("junk") == SchedulerConfig()
    c = SchedulerConfig.load(
        {"heavy_limit": 9, "max_auto_retries": 99, "retry_delay_s": -4, "hang_timeout_s": True,
         "per_project_limit": 3, "retry_billing_modes": ["api", "evil"]}
    )  # fmt: skip
    assert c.heavy_limit == 1 and c.max_auto_retries == 5 and c.retry_delay_s == 0
    assert c.hang_timeout_s == SchedulerConfig().hang_timeout_s
    assert c.per_project_limit == 3 and c.retry_billing_modes == ("api",)
