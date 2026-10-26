"""The lane command allowlist, exercised through the real permission policy (`decide`)."""

import pytest

from cpm.lanes.commands import make_hook
from cpm.lanes.spec import CommandRule
from cpm.workers.permissions import ToolContext, decide
from tests.fixtures.repos import write

RULES = [
    CommandRule.model_validate(
        {
            "name": "render",
            "program": "renderer",
            "flags": {"--seed": "value", "--out": "out_path", "--in": "path", "--v": "none"},
            "positional": "path",
            "max_positional": 1,
        }
    ),
    CommandRule.model_validate(
        {"name": "check", "program": "python", "prefix": ["tools/check.py"], "needs_trust": True}
    ),
    CommandRule.model_validate({"name": "ver", "program": "renderer", "prefix": ["--version"]}),
]


@pytest.fixture
def ctx(tmp_path):
    wt = tmp_path / "wt"
    (wt / "assets/out").mkdir(parents=True)
    write(wt, "scene.json", "{}")
    return ToolContext(wt, (), False, make_hook(RULES, ["assets/out/"]))


def bash(ctx, cmd):
    return decide("Bash", {"command": cmd}, ctx)


@pytest.mark.parametrize(
    "cmd",
    [
        "renderer scene.json",
        "renderer --seed 42 --out assets/out/a.wav scene.json",
        "renderer --seed=42 --v",
        "renderer --in scene.json",
        "renderer --version",
    ],
)
def test_allowed_forms(ctx, cmd):
    assert bash(ctx, cmd).allow, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "renderer --evil",  # unknown option
        "renderer --seed",  # missing value
        "renderer --seed /etc/passwd",  # absolute value
        "renderer --seed ../x",
        "renderer --seed 'a b'",  # pattern
        "renderer --out scene.json",  # outside the asset paths
        "renderer --out assets/out/../x.wav",
        "renderer --out .git/config",
        "renderer ../scene.json",  # leaves the worktree
        "renderer /tmp/x",
        "renderer a b",  # too many positionals
        "renderer --v=1",  # switch with a value
        "renderer --seed 1; rm -rf x",  # shell operator
        "renderer $(id)",
        "renderer --seed 1 > out.txt",
        "other scene.json",  # not on the list
        "python -c print(1)",
        "python tools/check.py",  # needs trust
    ],
)
def test_refused_forms(ctx, cmd):
    assert not bash(ctx, cmd).allow, cmd


def test_trusted_workspace_unlocks_needs_trust_rules(ctx):
    trusted = ToolContext(ctx.worktree, (), True, ctx.bash_extra)
    assert bash(trusted, "python tools/check.py").allow
    assert not bash(trusted, "python tools/check.py --x").allow
    assert not bash(trusted, "python tools/other.py").allow


def test_symlink_out_of_the_worktree_is_refused(ctx, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (ctx.worktree / "assets/out/link").symlink_to(outside, target_is_directory=True)
    assert not bash(ctx, "renderer --out assets/out/link/a.wav").allow
    assert not bash(ctx, "renderer assets/out/link").allow


def test_no_lane_means_the_old_allowlist(tmp_path):
    plain = ToolContext(tmp_path, (), False)
    assert not decide("Bash", {"command": "renderer x"}, plain).allow
    assert decide("Bash", {"command": "ls"}, plain).allow


def test_lane_rules_do_not_widen_git(ctx):
    assert not bash(ctx, "git push origin main").allow
