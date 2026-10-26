"""The SDK passes tools absolute (cwd-joined) paths: inside the worktree they are fine, elsewhere not."""

from pathlib import Path

import pytest

from cpm.workers.permissions import ToolContext, decide


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    (tmp_path / "docs").mkdir()
    return ToolContext(tmp_path.resolve(), ("docs/",), False, None)


def test_absolute_path_inside_worktree_is_allowed(ctx: ToolContext) -> None:
    target = str(ctx.worktree / "docs" / "note.md")
    assert decide("Write", {"file_path": target, "content": "x"}, ctx).allow


def test_absolute_path_outside_worktree_is_denied(ctx: ToolContext) -> None:
    outside = str(ctx.worktree.parent / "other" / "note.md")
    assert not decide("Write", {"file_path": outside, "content": "x"}, ctx).allow


def test_home_relative_path_is_denied(ctx: ToolContext) -> None:
    assert not decide("Write", {"file_path": "~/note.md", "content": "x"}, ctx).allow


def test_traversal_is_denied(ctx: ToolContext) -> None:
    assert not decide("Write", {"file_path": "docs/../../x.md", "content": "x"}, ctx).allow
