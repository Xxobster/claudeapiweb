"""AgentSdkProvider against a fake `claude_agent_sdk` module (no network, no real SDK)."""

import sys
import threading
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from cpm.workers.providers import AgentSdkProvider, RunSpec


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class AssistantMessage:
    content: list[Any]
    model: str
    usage: dict[str, int]
    message_id: str
    session_id: str = "sess-1"


@dataclass
class ResultMessage:
    is_error: bool
    result: str
    session_id: str
    total_cost_usd: float
    num_turns: int = 2
    subtype: str = "success"
    errors: list[str] = field(default_factory=list)


@pytest.fixture
def fake_sdk(monkeypatch):
    captured = {}
    mod = types.ModuleType("claude_agent_sdk")

    class Opts:
        def __init__(self, **kw):
            captured.update(kw)

    class Allow:
        pass

    class Deny:
        def __init__(self, message):
            self.message = message

    async def query(prompt, options):
        captured["prompt"] = prompt
        a = AssistantMessage(
            [TextBlock("hi"), ToolUseBlock("1", "Write", {"file_path": "a.txt"})],
            "claude-sonnet-5-5", {"input_tokens": 1000, "output_tokens": 500}, "msg_1",
        )  # fmt: skip
        yield a
        yield a  # a parallel tool call repeats the message id: counted once
        yield ResultMessage(False, "done", "sess-1", 0.02)

    mod.ClaudeAgentOptions, mod.query = Opts, query
    mod.PermissionResultAllow, mod.PermissionResultDeny = Allow, Deny
    mod.AssistantMessage, mod.ResultMessage = AssistantMessage, ResultMessage
    mod.TextBlock, mod.ToolUseBlock = TextBlock, ToolUseBlock
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return captured


def _spec(tmp_path, **kw):
    return RunSpec(
        run_id=1, task_id=1, provider="sdk", prompt="P", worktree=str(tmp_path),
        model="claude-sonnet-5-5",
        effort="high", scope=["src/"], setting_sources=[], max_budget_usd=0.5, **kw,
    )  # fmt: skip


def test_options_and_event_mapping(tmp_path, fake_sdk):
    events = []
    prov = AgentSdkProvider(
        _spec(tmp_path), lambda t, **f: events.append((t, f)), threading.Event()
    )
    assert prov.run() == 0
    o = fake_sdk
    assert o["cwd"] == str(tmp_path) and o["permission_mode"] == "default"
    assert o["allowed_tools"] == []  # nothing auto-approved: every call goes through can_use_tool
    assert o["setting_sources"] == [] and o["max_budget_usd"] == 0.5 and o["effort"] == "high"
    assert "WebFetch" in o["disallowed_tools"] and callable(o["can_use_tool"])
    usage = [f for t, f in events if t == "usage"]
    assert (
        len(usage) == 1 and usage[0]["message_id"] == "msg_1" and usage[0]["cost_microusd"] == 7000
    )
    result = next(f for t, f in events if t == "result")
    assert result["ok"] is True and result["total_cost_usd"] == 0.02


def test_can_use_tool_routes_through_policy(tmp_path, fake_sdk):
    import asyncio

    events = []
    prov = AgentSdkProvider(
        _spec(tmp_path), lambda t, **f: events.append((t, f)), threading.Event()
    )
    allow = asyncio.run(prov._can_use("Write", {"file_path": "src/a.py"}, None))
    deny = asyncio.run(prov._can_use("Bash", {"command": "git push"}, None))
    assert type(allow).__name__ == "Allow" and type(deny).__name__ == "Deny"
    assert [f["allowed"] for t, f in events if t == "permission"] == [True, False]


def test_missing_sdk_reports_instead_of_crashing(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    events = []
    prov = AgentSdkProvider(
        _spec(tmp_path), lambda t, **f: events.append((t, f)), threading.Event()
    )
    assert prov.run() == 2 and events[0][0] == "error"
