"""GitHub client (mock transport), real subprocess test runner, reviewer parsing, recovery."""

import sys

import httpx
import pytest
from sqlalchemy.orm import Session

from cpm.db.models import Task
from cpm.recovery.reconcile import Recovery
from cpm.review.github import (
    GitHubError,
    HttpGitHubClient,
    is_token_format,
    parse_remote,
)
from cpm.review.reviewer import MockReviewer, ReviewInput, parse_verdict
from cpm.review.testrun import SubprocessTestRunner

TOKEN = "ghp_" + "Zz9Yy8Xx7W" * 3


def client(handler):
    return HttpGitHubClient(httpx.Client(transport=httpx.MockTransport(handler)))


def create(c, **kw):
    args = {"head": "claude/x", "base": "main", "title": "T", "body": "B", "token": TOKEN}
    return c.create_pull_request("o", "r", **{**args, **kw})


def test_pull_request_request_shape_and_result():
    seen = {}

    def handler(req):
        seen["url"], seen["auth"], seen["json"] = (
            str(req.url),
            req.headers["authorization"],
            req.read(),
        )
        return httpx.Response(201, json={"number": 5, "html_url": "https://github.com/o/r/pull/5"})

    pr = create(client(handler))
    assert (pr.number, pr.url) == (5, "https://github.com/o/r/pull/5")
    assert seen["url"] == "https://api.github.com/repos/o/r/pulls"
    assert seen["auth"] == f"Bearer {TOKEN}"
    assert (
        b'"head":"claude/x"' in seen["json"].replace(b" ", b"")
        and TOKEN.encode() not in seen["json"]
    )


@pytest.mark.parametrize("status", [401, 403, 404, 422, 500])
def test_errors_are_short_and_never_echo_the_token(status):
    c = client(lambda r: httpx.Response(status, json={"message": f"bad credentials {TOKEN}"}))
    with pytest.raises(GitHubError) as e:
        create(c)
    assert str(status) in str(e.value) and TOKEN not in str(e.value)


def test_unreachable_github_and_odd_answers():
    def down(req):
        raise httpx.ConnectError("boom " + TOKEN)

    with pytest.raises(GitHubError, match="unreachable") as e:
        create(client(down))
    assert TOKEN not in str(e.value)
    with pytest.raises(GitHubError, match="without a pull request number"):
        create(client(lambda r: httpx.Response(201, json={})))
    with pytest.raises(GitHubError, match="unexpected pull request address"):
        create(
            client(
                lambda r: httpx.Response(
                    201, json={"number": 1, "html_url": "https://evil.example/o/r/pull/1"}
                )
            )
        )  # noqa: E501


def test_redirects_are_not_followed_with_the_token():
    c = client(lambda r: httpx.Response(307, headers={"location": "https://evil.example/"}))
    with pytest.raises(GitHubError):
        create(c)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/Xxobster/Nirandyr", ("Xxobster", "Nirandyr")),
        ("https://github.com/a/b.git", ("a", "b")),
        ("git@github.com:a/b.git", ("a", "b")),
        ("https://github.com/a/b/", ("a", "b")),
        ("https://evil.com/a/b", None),
        ("https://github.com.evil.com/a/b", None),
        ("https://github.com/a/b/c", None),
        ("https://github.com/a/..", None),
        ("https://user:pw@github.com/a/b", None),
        ("file:///x/y", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_remote(url, expected):
    assert parse_remote(url) == expected


def test_token_format():
    assert is_token_format(TOKEN) and is_token_format("github_pat_" + "a" * 30)
    assert not is_token_format("sk-ant-api03-" + "a" * 30) and not is_token_format("ghp_short")


# ------------------------------------------------------------------ subprocess runner
def test_runner_reports_exit_code_and_output_tail(tmp_path):
    r = SubprocessTestRunner().run(
        [sys.executable, "-c", "import sys; print('hello'); sys.exit(3)"], tmp_path, 30
    )
    assert (r.exit_code, r.passed, r.timed_out) == (3, False, False) and "hello" in r.tail


def test_runner_kills_a_hung_test_run_and_its_children(tmp_path):
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); time.sleep(60)"  # noqa: E501
    r = SubprocessTestRunner().run([sys.executable, "-c", code], tmp_path, 1.0)
    assert r.timed_out and r.exit_code is None and not r.passed
    assert r.duration_s < 20


def test_runner_caps_output_and_has_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-" + "q" * 40)
    code = "import os; print(os.environ.get('ANTHROPIC_API_KEY','absent')); print('x' * 500000)"
    r = SubprocessTestRunner().run([sys.executable, "-c", code], tmp_path, 30)
    assert len(r.tail) <= 16_000 and r.passed
    r2 = SubprocessTestRunner().run(
        [sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY','absent'))"],
        tmp_path, 30,
    )  # fmt: skip
    assert "absent" in r2.tail


def test_runner_missing_program_is_a_failure_not_an_exception(tmp_path):
    r = SubprocessTestRunner().run(["definitely-not-a-program-xyz"], tmp_path, 5)
    assert not r.passed and "could not start" in r.tail


# ------------------------------------------------------------------ reviewer parsing
def data():
    return ReviewInput("t", "", ["A", "B"], ["f"], "diff", False)


def test_parse_verdict_is_strict():
    v = parse_verdict(
        {"criteria": [{"text": "A", "met": True, "note": "ok"}, {"text": "B", "met": 1}],
         "issues": [{"severity": "nonsense", "category": "??", "text": "x"}, "junk", 5]},
        ["A", "B"],
    )  # fmt: skip
    assert [c["met"] for c in v.criteria] == [True, False]  # 1 is not True
    assert [(i.severity, i.category) for i in v.issues] == [("blocker", "other")]
    assert not v.approved


def test_parse_verdict_rejects_non_dict_and_caps_issues():
    assert not parse_verdict("approved!", ["A"]).approved
    v = parse_verdict(
        {
            "criteria": [{"text": "A", "met": True}],
            "issues": [{"text": "w", "severity": "warning"}] * 99,
        },
        ["A"],
    )  # noqa: E501
    assert len(v.issues) == 30 and v.approved  # warnings alone do not block


def test_mock_reviewer_default_accepts_and_records():
    m = MockReviewer()
    assert parse_verdict(m.review(data()), ["A", "B"]).approved and len(m.calls) == 1


# ------------------------------------------------------------------ recovery of gate states
def test_restart_recovery_of_gate_states(renv):
    t1 = renv.new_task("r", acceptance=["a"])
    t2 = renv.new_task("c", acceptance=["a"])
    for tid, path in ((t1.id, ["RESERVED", "RUNNING", "TESTING", "REVIEWING"]),
                      (t2.id, ["RESERVED", "RUNNING", "TESTING", "CORRECTING"])):  # fmt: skip
        for to in path:
            renv.tasks.transition(tid, to, actor="t")
    rep = Recovery(renv.engine, renv.settings).reconcile()
    assert sorted(rep.tasks_demoted) == sorted([t1.id, t2.id])
    assert renv.tasks.get(t1.id).status == "TESTING"  # the gate asks again
    assert renv.tasks.get(t2.id).status == "PAUSED"  # a person decides
    assert Recovery(renv.engine, renv.settings).reconcile().tasks_demoted == []  # idempotent
    with Session(renv.engine) as s:
        assert s.get(Task, t1.id).status == "TESTING"
