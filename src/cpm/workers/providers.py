"""Providers run INSIDE the child process (`python -m cpm.workers.run`).

- `MockProvider`: scripted scenarios, no network, no key. All tests and the UI demo use it.
- `AgentSdkProvider`: the real Claude Agent SDK, API-billed. Only constructed when the supervisor
  passed `provider="sdk"` after the live-run gates (flag, cap, key, per-run budget) held.

A provider never touches the database or the key store: it receives a `RunSpec`, calls `emit`
with event dicts, and returns the process exit code. Verified SDK facts: docs/SDK_NOTES.md.
"""

from __future__ import annotations

import asyncio
import os
import subprocess  # noqa: S404 (argument list, mock scenario only)
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from cpm.workers.permissions import Decision, ToolContext, decide, non_idempotent_step

Emit = Callable[..., None]
SCENARIOS = (
    "success", "fail", "hang", "crash", "budget_burst", "grandchild", "slow", "sdk_budget_error",
    "half_done",
)  # fmt: skip


@dataclass
class RunSpec:
    """Everything the child needs. Written by the supervisor as JSON; contains no secret."""

    run_id: int
    task_id: int
    provider: str  # mock | sdk
    prompt: str
    worktree: str
    model: str | None = None
    effort: str | None = None
    scenario: str = "success"
    scenario_args: dict[str, Any] = field(default_factory=dict)
    scope: list[str] = field(default_factory=list)
    trusted: bool = False
    setting_sources: list[str] = field(default_factory=list)
    max_budget_usd: float | None = None
    max_turns: int = 40
    resume_session_id: str | None = None
    heartbeat_s: float = 5.0

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class StopRequested(Exception):
    pass


# ---------------------------------------------------------------------------------------- mock
class MockProvider:
    def __init__(self, spec: RunSpec, emit: Emit, stop: threading.Event) -> None:
        self.spec, self.emit, self.stop = spec, emit, stop
        self.session_id = spec.resume_session_id or f"mock-session-{spec.run_id}"
        self._n = 0

    def _wait(self, seconds: float) -> None:
        if self.stop.wait(seconds):
            raise StopRequested

    def _usage(self, cost_microusd: int, message_id: str | None = None) -> None:
        self._n += 1
        self.emit(
            "usage",
            message_id=message_id or f"mock-{self.spec.run_id}-{self._n}",
            model=self.spec.model or "mock",
            input_tokens=100, output_tokens=50, cost_microusd=cost_microusd,
        )  # fmt: skip

    def _start(self) -> None:
        self.emit("session", session_id=self.session_id, resumed=bool(self.spec.resume_session_id))
        self.emit("text", text=f"mock worker started: {self.spec.scenario}")

    def _result(self, ok: bool, text: str) -> int:
        self.emit("result", ok=ok, text=text, session_id=self.session_id)
        return 0 if ok else 1

    def run(self) -> int:
        try:
            code: int = getattr(self, f"_s_{self.spec.scenario}")()
            return code
        except StopRequested:
            self.emit("stopped", session_id=self.session_id)
            return 0
        except AttributeError:
            self.emit("error", message=f"unknown scenario {self.spec.scenario!r}")
            return 2

    def _write_output(self) -> None:
        out = Path(self.spec.worktree) / f"mock_output_task{self.spec.task_id}.txt"
        out.write_text(f"mock result for task {self.spec.task_id}\n", encoding="utf-8")
        self.emit("tool_use", name="Write", input={"file_path": out.name})

    def _s_success(self) -> int:
        self._start()
        self._write_output()
        self._usage(1_000)
        return self._result(True, "mock work done")

    def _s_fail(self) -> int:
        self._start()
        self._usage(500)
        self.emit("error", message="mock failure: the scripted task could not be completed")
        return self._result(False, "mock failed")

    def _s_sdk_budget_error(self) -> int:
        """The SDK's own max_budget_usd fires first: error result, cost only in the total."""
        self._start()
        self._usage(200)
        self.emit(
            "result", ok=False, text="budget", session_id=self.session_id,
            subtype="error_max_budget_usd", total_cost_usd=0.0015,
        )  # fmt: skip
        return 1

    def _s_half_done(self) -> int:
        """Dies inside a non-idempotent step (allowed `git commit`, never finished)."""
        self._start()
        self.emit("permission", tool="Bash", allowed=True, reason="ok", step="git commit")
        os._exit(3)

    def _s_hang(self) -> int:
        self._start()
        time.sleep(3600)  # no heartbeat, no output: the watchdog must kill us
        return 0

    def _s_crash(self) -> int:
        self._start()
        os._exit(3)  # abrupt death: no result, no flush of anything pending

    def _s_budget_burst(self) -> int:
        self._start()
        step = int(self.spec.scenario_args.get("step_microusd", 500_000))
        for i in range(int(self.spec.scenario_args.get("steps", 20))):
            self._usage(step, message_id=f"mock-{self.spec.run_id}-burst-{i}")
            self._usage(step, message_id=f"mock-{self.spec.run_id}-burst-{i}")  # duplicate id
            self._wait(0.05)
        return self._result(True, "burst finished")

    def _s_grandchild(self) -> int:
        self._start()
        child = subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(3600)"], stdin=subprocess.DEVNULL
        )
        self.emit("grandchild", pid=child.pid)
        while True:
            self._wait(0.2)

    def _s_slow(self) -> int:
        self._start()
        steps = int(self.spec.scenario_args.get("steps", 50))
        for i in range(steps):
            self.emit("text", text=f"step {i + 1}/{steps}")
            self._usage(100)
            self._wait(float(self.spec.scenario_args.get("delay_s", 0.1)))
        self._write_output()
        return self._result(True, "slow work done")


# ---------------------------------------------------------------------------------------- sdk
class AgentSdkProvider:
    """Real run through `claude_agent_sdk.query` (SDK_NOTES sections 1-4)."""

    def __init__(self, spec: RunSpec, emit: Emit, stop: threading.Event) -> None:
        self.spec, self.emit, self.stop = spec, emit, stop
        self.ctx = ToolContext(Path(spec.worktree), tuple(spec.scope), spec.trusted)
        self._seen: set[str] = set()

    def run(self) -> int:
        try:
            return asyncio.run(self._run())
        except ImportError:
            self.emit("error", message="claude-agent-sdk is not installed (pip install .[sdk])")
            return 2

    async def _can_use(self, tool: str, inp: dict[str, Any], _ctx: Any) -> Any:
        import claude_agent_sdk as sdk

        d: Decision = decide(tool, inp, self.ctx)
        step = non_idempotent_step(tool, inp) if d.allow else None
        self.emit("permission", tool=tool, allowed=d.allow, reason=d.reason, step=step)
        if d.allow:
            if d.updated_command is not None:
                return sdk.PermissionResultAllow(
                    updated_input={**inp, "command": d.updated_command}
                )
            return sdk.PermissionResultAllow()
        return sdk.PermissionResultDeny(message=d.reason)

    async def _run(self) -> int:
        import claude_agent_sdk as sdk

        s = self.spec
        # Tools in `allowed_tools` skip `can_use_tool`, so nothing is listed there: every call
        # goes through `decide` (SDK-3, types._warn_if_can_use_tool_shadowed).
        options = sdk.ClaudeAgentOptions(
            cwd=s.worktree,
            model=s.model,
            effort=s.effort,
            permission_mode="default",
            tools=["Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "Bash", "TodoWrite"],
            allowed_tools=[],
            disallowed_tools=["WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit"],
            can_use_tool=self._can_use,
            setting_sources=list(s.setting_sources),
            max_turns=s.max_turns,
            max_budget_usd=s.max_budget_usd,
            resume=s.resume_session_id,
        )
        session_id = s.resume_session_id
        ok, text = False, ""
        async for msg in sdk.query(prompt=s.prompt, options=options):
            new_id = getattr(msg, "session_id", None)
            if new_id and new_id != session_id:
                session_id = new_id
                self.emit("session", session_id=new_id, resumed=bool(s.resume_session_id))
            if self.stop.is_set():
                self.emit("stopped", session_id=session_id)
                return 0
            if isinstance(msg, sdk.AssistantMessage):
                self._assistant(msg)
            elif isinstance(msg, sdk.ResultMessage):
                ok = not msg.is_error
                text = msg.result or ""
                self.emit(
                    "result", ok=ok, text=text, session_id=msg.session_id,
                    total_cost_usd=msg.total_cost_usd, num_turns=msg.num_turns,
                    subtype=msg.subtype, errors=msg.errors,
                )  # fmt: skip
        return 0 if ok else 1

    def _assistant(self, msg: Any) -> None:
        import claude_agent_sdk as sdk

        for block in msg.content:
            if isinstance(block, sdk.TextBlock):
                self.emit("text", text=block.text)
            elif isinstance(block, sdk.ToolUseBlock):
                self.emit("tool_use", name=block.name, input=block.input)
        mid = msg.message_id
        if mid and mid not in self._seen and msg.usage:  # one message id counts once (SDK-4)
            self._seen.add(mid)
            u = msg.usage
            tokens = (
                int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)),
                int(u.get("cache_read_input_tokens", 0)),
                int(u.get("cache_creation_input_tokens", 0)),
            )  # fmt: skip
            self.emit(
                "usage",
                message_id=mid,
                model=msg.model,
                input_tokens=tokens[0],
                output_tokens=tokens[1],
                cache_read_tokens=tokens[2],
                cache_write_tokens=tokens[3],
            )  # fmt: skip
            # no cost here: the supervisor prices the tokens from the DB table (WP8)


def make_provider(spec: RunSpec, emit: Emit, stop: threading.Event) -> Any:
    if spec.provider == "mock":
        return MockProvider(spec, emit, stop)
    if spec.provider == "sdk":
        return AgentSdkProvider(spec, emit, stop)
    raise ValueError(f"unknown provider {spec.provider!r}")
