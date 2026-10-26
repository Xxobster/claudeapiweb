"""Run supervisor: starts one child per task, owns its database rows and kills its tree (WP5).

One writer per truth: the child only prints JSON lines; this class turns them into `runs`,
`events`, `usage_events` and `tasks` changes (the latter always through `TaskService.transition`).
Admission (reservations, global budget, concurrency) is the scheduler's job in WP6-WP8; what is
enforced here is the part that must hold for every launch path: the billing gates, the per-run
budget backstop, the hang watchdog, tree-kill and "never delete work".

Intents explain why a run was stopped; the outcome table in `_finalize` maps them to run and task
states. A run that ends without an intent is judged by its exit code and `result` event only.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import IO, Any

from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cpm.config import Settings
from cpm.db.models import Event, Project, Run, Task, UsageEvent, utcnow
from cpm.db.settings_repo import get_setting
from cpm.discovery.gitinfo import _filter_overrides
from cpm.discovery.policy import worker_policy
from cpm.security.childenv import build_child_env, build_mock_env, git_config_env
from cpm.security.paths import PathPolicyError, resolve_in_any
from cpm.security.redaction import redact, register_secret
from cpm.security.secrets_store import ANTHROPIC_KEY, SecretStore, SecretStoreUnavailable
from cpm.tasks.service import IllegalTransition, TaskService
from cpm.workers import proctree
from cpm.workers.events import RunLog
from cpm.workers.prompt import build_prompt
from cpm.workers.providers import SCENARIOS, RunSpec
from cpm.workers.worktree import WorktreeError, WorktreeManager

log = logging.getLogger("cpm.workers")

ACTOR = "worker-supervisor"
_MICRO = Decimal(1_000_000)
MAX_LIVE = 8
MAX_LINE = 1_000_000  # one worker output line; longer ones are dropped (memory bound)
RUN_ENDED = (
    "SUCCEEDED",
    "FAILED",
    "CRASHED",
    "HUNG",
    "TIMEOUT",
    "CANCELLED",
    "STOPPED",
    "BUDGET_STOPPED",
    "KILLED",
)
_STARTABLE = ("READY", "RESERVED", "PAUSED")
# intent -> (run status, task status, reason)
_OUTCOMES = {
    "cancel": ("CANCELLED", "CANCELLED", "cancelled by user"),
    "pause": ("STOPPED", "PAUSED", "paused by user"),
    "budget": ("BUDGET_STOPPED", "PAUSED_BUDGET", "per-run budget reached"),
    "hang": ("HUNG", "FAILED", "worker stopped responding (no heartbeat)"),
    "timeout": ("TIMEOUT", "FAILED", "worker exceeded its maximum run time"),
    "shutdown": ("KILLED", "FAILED", "CPM shut down while the worker was running"),
}


class RunError(Exception):
    """A run cannot start or be controlled. `.status` is the HTTP status the API maps it to."""

    status = 409


class RunRefused(RunError):
    status = 422


class LiveRunNotAllowed(RunError):
    status = 403


class NotRunning(RunError):
    status = 404


@dataclass
class RunInfo:
    id: int
    task_id: int
    attempt: int
    billing_mode: str
    model: str | None
    status: str
    pid: int | None
    worktree_path: str | None
    session_id: str | None
    started_at: Any
    heartbeat_at: Any
    ended_at: Any
    exit_code: int | None
    cost_microusd: int


@dataclass
class _Live:
    run_id: int
    task_id: int
    project_id: int
    tree: proctree.ProcessTree
    log: RunLog
    cap_microusd: int | None
    hang_timeout_s: float
    max_runtime_s: float
    resumed: bool
    intent: str | None = None
    last_event: float = 0.0
    last_db_beat: float = 0.0
    started: float = 0.0
    cost_microusd: int = 0
    result: dict[str, Any] | None = None
    session_id: str | None = None
    exited: bool = False


def _snapshot(s: Session, r: Run) -> RunInfo:
    cost = s.scalar(
        select(func.coalesce(func.sum(UsageEvent.cost_microusd), 0)).where(
            UsageEvent.run_id == r.id
        )
    )
    return RunInfo(
        r.id,
        r.task_id,
        r.attempt,
        r.billing_mode,
        r.model,
        r.status,
        r.pid,
        r.worktree_path,
        r.session_id,
        r.started_at,
        r.heartbeat_at,
        r.ended_at,
        r.exit_code,
        int(cost or 0),
    )


def _lines(stream: IO[bytes]) -> Iterator[bytes | None]:
    """Lines of at most MAX_LINE bytes; an oversized line is skipped to its end (yields None)."""
    while True:
        raw = stream.readline(MAX_LINE + 1)
        if not raw:
            return
        if len(raw) > MAX_LINE and not raw.endswith(b"\n"):
            while True:
                rest = stream.readline(MAX_LINE)
                if not rest or rest.endswith(b"\n"):
                    break
            yield None
            continue
        yield raw


class RunSupervisor:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        secrets: SecretStore,
        *,
        python: str | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.secrets = secrets
        self.python = python or sys.executable
        self.tasks = TaskService(engine)
        self.worktrees = WorktreeManager(settings.worktree_root)
        self._live: dict[int, _Live] = {}
        self._lock = threading.RLock()
        self._threads: dict[int, threading.Thread] = {}
        self._starting: set[int] = set()

    # ----------------------------------------------------------------------- queries
    def get_run(self, run_id: int) -> RunInfo:
        with Session(self.engine) as s:
            r = s.get(Run, run_id)
            if r is None:
                raise NotRunning(f"run {run_id} not found")
            return _snapshot(s, r)

    def list_runs(self, task_id: int) -> list[RunInfo]:
        with Session(self.engine) as s:
            rows = s.scalars(select(Run).where(Run.task_id == task_id).order_by(Run.id))
            return [_snapshot(s, r) for r in rows]

    def run_log(self, run_id: int) -> RunLog:
        with self._lock:
            live = self._live.get(run_id)
        return live.log if live else RunLog(self.settings.runs_dir / str(run_id) / "events.jsonl")

    def is_live(self, run_id: int) -> bool:
        with self._lock:
            return run_id in self._live

    def live_run_for_task(self, task_id: int) -> int | None:
        with self._lock:
            return next((r for r, lv in self._live.items() if lv.task_id == task_id), None)

    # ----------------------------------------------------------------------- start
    def _live_gates(self, task_id: int, max_budget_microusd: int | None) -> tuple[str, int]:
        """All conditions for an API-billed run. Returns (api key, per-run cap in micro-USD)."""
        if not self.settings.allow_live_runs:
            raise LiveRunNotAllowed("live runs are off (start CPM with CPM_ALLOW_LIVE_RUNS=1)")
        cfg = get_setting(self.engine, "wizard.config")
        if not cfg or int(cfg.get("global_budget_microusd", 0)) <= 0:
            raise LiveRunNotAllowed("no budget cap configured: finish the first-run wizard")
        try:
            key = self.secrets.get(ANTHROPIC_KEY)
        except SecretStoreUnavailable as exc:
            raise LiveRunNotAllowed("no secure key store available") from exc
        if not key:
            raise LiveRunNotAllowed("no API key saved")
        if not max_budget_microusd or max_budget_microusd <= 0:
            raise RunRefused("an API-billed run needs an explicit per-run budget")
        ceilings = [self.settings.live_run_max_microusd]
        if int(cfg.get("task_budget_microusd", 0)) > 0:
            ceilings.append(int(cfg["task_budget_microusd"]))
        if max_budget_microusd > min(ceilings):
            raise RunRefused(
                f"per-run budget {max_budget_microusd} exceeds the allowed ceiling {min(ceilings)}"
            )
        # Retries and resumes must not multiply the cap: all runs of a task share one ceiling.
        with Session(self.engine) as s:
            spent = int(
                s.scalar(
                    select(func.coalesce(func.sum(UsageEvent.cost_microusd), 0)).where(
                        UsageEvent.task_id == task_id
                    )
                )
                or 0
            )
        remaining = min(ceilings) - spent
        if max_budget_microusd > remaining:
            raise RunRefused(
                f"task budget left is {max(remaining, 0)} micro-USD ({spent} already spent "
                f"of {min(ceilings)}); lower the per-run budget or raise the task budget"
            )
        return key, max_budget_microusd

    def start(
        self,
        task_id: int,
        *,
        scenario: str = "success",
        scenario_args: dict[str, Any] | None = None,
        max_budget_microusd: int | None = None,
        hang_timeout_s: float = 60.0,
        max_runtime_s: float = 3600.0,
        heartbeat_s: float = 5.0,
    ) -> RunInfo:
        # Reserve the slot under the lock, then do the slow part (git checkout, spawn) outside it
        # so stop(), is_live() and the WebSocket stream are never blocked by a long checkout.
        with self._lock:
            if self.live_run_for_task(task_id) is not None or task_id in self._starting:
                raise RunError(f"task {task_id} already has a running worker")
            if len(self._live) + len(self._starting) >= min(MAX_LIVE, self._max_parallel()):
                raise RunError("the maximum number of parallel workers is running")
            self._starting.add(task_id)
        try:
            return self._start_locked(
                task_id,
                scenario,
                scenario_args or {},
                max_budget_microusd,
                hang_timeout_s,
                max_runtime_s,
                heartbeat_s,
            )
        finally:
            with self._lock:
                self._starting.discard(task_id)

    def busy(self, task_id: int) -> bool:
        """A worker is running or being launched for this task."""
        with self._lock:
            return self.live_run_for_task(task_id) is not None or task_id in self._starting

    def _task_status(self, task_id: int) -> str | None:
        with Session(self.engine) as s:
            t = s.get(Task, task_id)
            return t.status if t else None

    def _max_parallel(self) -> int:
        cfg = get_setting(self.engine, "wizard.config") or {}
        return int(cfg.get("max_parallel_agents", 3))

    def _start_locked(
        self,
        task_id: int,
        scenario: str,
        scenario_args: dict[str, Any],
        max_budget_microusd: int | None,
        hang_timeout_s: float,
        max_runtime_s: float,
        heartbeat_s: float,
    ) -> RunInfo:
        with Session(self.engine) as s:
            task = s.get(Task, task_id)
            if task is None:
                raise NotRunning(f"task {task_id} not found")
            project = s.get(Project, task.project_id)
            assert project is not None  # noqa: S101 (FK guarantees it)
            if task.status not in _STARTABLE:
                raise RunError(f"a {task.status} task cannot start a worker")
            prev_session = s.scalar(
                select(Run.session_id)
                .where(Run.task_id == task_id, Run.session_id.is_not(None))
                .order_by(Run.id.desc())
                .limit(1)
            )
            resumed = task.status == "PAUSED" and prev_session is not None
            attempt = (
                s.scalar(select(func.max(Run.attempt)).where(Run.task_id == task_id)) or 0
            ) + 1
            s.expunge_all()
            billing, model, effort = task.billing_mode, task.model, task.effort
            title, project_path, slug = task.title, Path(project.path), project.slug
            trusted, prior_status = bool(project.workspace_trusted), task.status

        key: str | None = None
        cap: int | None = max_budget_microusd
        if billing == "api":
            key, cap = self._live_gates(task_id, max_budget_microusd)
            register_secret(key)  # exact-match redaction of the key everywhere, logs included
            if scenario != "success" or scenario_args:
                raise RunRefused("scenarios exist only for mock tasks")
            if not model:
                model = (get_setting(self.engine, "wizard.config") or {}).get(
                    "default_model", "claude-sonnet-5-5"
                )
        elif billing == "mock":
            if scenario not in SCENARIOS:
                raise RunRefused(f"unknown scenario; choose one of {', '.join(SCENARIOS)}")
        else:
            raise RunRefused("CLI/subscription mode is an interface stub in v1 (DECISIONS Q9)")

        data = self.tasks.get(task_id)
        try:  # the stored path is re-checked at use time (moved, replaced by a junction, revoked)
            project_path = resolve_in_any(
                get_setting(self.engine, "approved_roots", []) or [], project_path
            )
        except PathPolicyError as exc:
            raise RunRefused(f"project path is not inside an approved root: {exc}") from exc
        try:
            wt = self.worktrees.ensure(project_path, slug, task_id, title)
        except WorktreeError as exc:
            raise RunRefused(f"worktree: {exc}") from exc

        policy = worker_policy(trusted)
        spec = RunSpec(
            run_id=0,
            task_id=task_id,
            provider="sdk" if billing == "api" else "mock",
            prompt=build_prompt(data, branch=wt.branch, resumed=resumed),
            worktree=str(wt.path),
            model=model,
            effort=effort,
            scenario=scenario,
            scenario_args=scenario_args,
            scope=data.declared_scope,
            trusted=trusted,
            setting_sources=list(policy.setting_sources),
            max_budget_usd=float(Decimal(cap) / _MICRO) if cap else None,
            resume_session_id=prev_session if resumed else None,
            heartbeat_s=heartbeat_s,
        )

        extra = {"PYTHONIOENCODING": "utf-8"}
        if not trusted:
            extra |= self._untrusted_git_env(wt.path)
        env = build_child_env("api", key, extra=extra) if key else build_mock_env(extra=extra)

        # Task moves first: an illegal state (e.g. a concurrent cancel) stops the launch here.
        try:
            if prior_status == "READY":
                self.tasks.transition(task_id, "RESERVED", actor=ACTOR)
            self.tasks.transition(task_id, "RUNNING", actor=ACTOR, reason="worker starting")
        except IllegalTransition as exc:
            raise RunError(str(exc)) from exc

        with Session(self.engine) as s:
            run = Run(
                task_id=task_id,
                attempt=attempt,
                billing_mode=billing,
                model=model,
                status="STARTING",
                worktree_path=str(wt.path),
                started_at=utcnow(),
                heartbeat_at=utcnow(),
            )
            s.add(run)
            s.commit()
            run_id = run.id
        project_id = data.project_id
        spec.run_id = run_id
        run_dir = self.settings.runs_dir / str(run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        spec_path = run_dir / "spec.json"
        spec_path.write_text(json.dumps(spec.to_json()), encoding="utf-8")
        runlog = RunLog(run_dir / "events.jsonl")
        runlog.append(
            "run_created",
            run_id=run_id,
            attempt=attempt,
            billing_mode=billing,
            provider=spec.provider,
            model=model,
            resumed=resumed,
            branch=wt.branch,
        )

        if self._task_status(task_id) != "RUNNING":  # cancelled while we were preparing
            self._finish_rows(run_id, task_id, "CANCELLED", None, "task changed before launch")
            raise RunError("the task was stopped before the worker started")
        argv = [self.python, "-I", "-m", "cpm.workers.run", "--spec", str(spec_path)]
        try:
            tree = proctree.spawn(argv, env, run_dir)
        except OSError as exc:
            self._finish_rows(run_id, task_id, "CRASHED", None, f"could not start worker: {exc}")
            self._fail_task(task_id, "could not start worker")
            raise RunError("could not start the worker process") from exc

        live = _Live(
            run_id,
            task_id,
            project_id,
            tree,
            runlog,
            cap,
            hang_timeout_s,
            max_runtime_s,
            resumed,
            last_event=time.monotonic(),
            started=time.monotonic(),
        )
        with self._lock:
            self._live[run_id] = live
        if self._task_status(task_id) != "RUNNING":  # cancel API raced with the spawn
            live.intent = "cancel"
            tree.kill()
        with Session(self.engine) as s:
            r = s.get(Run, run_id)
            assert r is not None  # noqa: S101
            r.pid, r.status = tree.pid, "RUNNING"
            s.add(
                Event(
                    project_id=project_id,
                    task_id=task_id,
                    run_id=run_id,
                    kind="run.started",
                    detail=json.dumps({"attempt": attempt, "billing_mode": billing}),
                )
            )
            s.commit()
        t = threading.Thread(target=self._monitor, args=(live,), name=f"run-{run_id}", daemon=True)
        self._threads[run_id] = t
        t.start()
        return self.get_run(run_id)

    def _untrusted_git_env(self, worktree: Path) -> dict[str, str]:
        """Neutralise repo-defined programs git could run for an untrusted workspace (T-05)."""
        import os

        pairs = [
            ("core.fsmonitor", "false"),
            ("core.hooksPath", os.devnull),
            ("diff.external", ""),
            ("core.pager", "cat"),
        ]
        cfg = _filter_overrides(worktree)
        pairs += [(cfg[i + 1].removesuffix("="), "") for i in range(0, len(cfg), 2)]
        return git_config_env(pairs)

    # ----------------------------------------------------------------------- control
    def stop(self, run_id: int, intent: str, *, grace_s: float = 3.0) -> None:
        """Ask a live run to end. `cancel` kills the tree at once; `pause` asks nicely first."""
        if intent not in ("cancel", "pause"):
            raise ValueError("intent must be cancel or pause")
        with self._lock:
            live = self._live.get(run_id)
            if live is None:
                raise NotRunning(f"run {run_id} is not running")
            if live.intent is None:
                live.intent = intent
        self._request_end(live, graceful=intent == "pause", grace_s=grace_s)

    def _request_end(self, live: _Live, *, graceful: bool, grace_s: float = 3.0) -> None:
        if graceful:
            try:
                assert live.tree.popen.stdin is not None  # noqa: S101
                live.tree.popen.stdin.write(b"stop\n")
                live.tree.popen.stdin.flush()
            except (OSError, ValueError):
                live.tree.kill()
                return
            threading.Timer(grace_s, live.tree.kill).start()
        else:
            live.tree.kill()

    def shutdown(self, timeout_s: float = 10.0) -> None:
        with self._lock:
            lives = list(self._live.values())
            for lv in lives:
                lv.intent = lv.intent or "shutdown"
        for lv in lives:
            lv.tree.kill()
        deadline = time.monotonic() + timeout_s
        for lv in lives:
            t = self._threads.get(lv.run_id)
            if t:
                t.join(max(0.1, deadline - time.monotonic()))

    # ----------------------------------------------------------------------- monitor thread
    def _monitor(self, live: _Live) -> None:
        readers = [
            threading.Thread(target=self._read_stdout, args=(live,), daemon=True),
            threading.Thread(target=self._read_stderr, args=(live,), daemon=True),
        ]
        for r in readers:
            r.start()
        popen = live.tree.popen
        while popen.poll() is None:
            now = time.monotonic()
            if live.intent is None:
                if now - live.last_event > live.hang_timeout_s:
                    live.intent = "hang"
                elif now - live.started > live.max_runtime_s:
                    live.intent = "timeout"
                if live.intent:
                    live.tree.kill()
            time.sleep(0.05)
        live.exited = True
        live.tree.kill()  # leftover grandchildren must not outlive their worker
        for r in readers:
            r.join(5)
        try:
            self._finalize(live, popen.returncode)
        except Exception:  # noqa: BLE001 - never lose the run row because of a bug here
            log.exception("finalizing run %s failed", live.run_id)
        finally:
            live.tree.close()
            with self._lock:
                self._live.pop(live.run_id, None)

    def _read_stdout(self, live: _Live) -> None:
        stream: IO[bytes] = live.tree.popen.stdout  # type: ignore[assignment]
        for raw in _lines(stream):
            live.last_event = time.monotonic()
            if raw is None:
                live.log.append("warning", text="oversized output line dropped")
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
                if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
                    raise ValueError
            except ValueError:
                live.log.append("stdout", text=line)
                continue
            self._handle_event(live, ev)

    def _read_stderr(self, live: _Live) -> None:
        stream: IO[bytes] = live.tree.popen.stderr  # type: ignore[assignment]
        for raw in _lines(stream):
            if raw is None:
                continue
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text:
                live.log.append("stderr", text=text)

    def _handle_event(self, live: _Live, ev: dict[str, Any]) -> None:
        kind = ev.pop("type")
        fields = {k: v for k, v in ev.items() if k not in ("seq", "ts")}
        if kind == "heartbeat":
            self._beat(live)
            live.log.append("heartbeat")
            return
        live.log.append(kind, **fields)
        if kind == "usage":
            self._record_usage(live, fields)
        elif kind == "result":
            live.result = fields
        if kind in ("session", "stopped", "result") and isinstance(fields.get("session_id"), str):
            live.session_id = fields["session_id"]
        self._beat(live)

    def _beat(self, live: _Live) -> None:
        now = time.monotonic()
        if now - live.last_db_beat < 1.0:
            return
        live.last_db_beat = now
        with Session(self.engine) as s:
            r = s.get(Run, live.run_id)
            if r is not None and r.status == "RUNNING":
                r.heartbeat_at = utcnow()
                s.commit()

    def _record_usage(self, live: _Live, f: dict[str, Any]) -> None:
        mid, cost = f.get("message_id"), f.get("cost_microusd")
        if not isinstance(mid, str) or not isinstance(cost, int) or cost < 0 or not mid:
            live.log.append("warning", text="malformed usage event ignored")
            return
        row = UsageEvent(
            run_id=live.run_id,
            task_id=live.task_id,
            project_id=live.project_id,
            dedup_key=f"msg:{mid}"[:128],
            model=str(f.get("model") or "unknown")[:64],
            input_tokens=int(f.get("input_tokens") or 0),
            output_tokens=int(f.get("output_tokens") or 0),
            cache_read_tokens=int(f.get("cache_read_tokens") or 0),
            cache_write_tokens=int(f.get("cache_write_tokens") or 0),
            cost_microusd=cost,
            reconciled=False,
        )
        try:
            with Session(self.engine) as s:
                s.add(row)
                s.commit()
        except IntegrityError:
            return  # same message id seen twice: counted once (SDK_NOTES SDK-4)
        live.cost_microusd += cost
        if live.cap_microusd is not None and live.cost_microusd >= live.cap_microusd:
            if live.intent is None:
                live.intent = "budget"
                live.log.append(
                    "budget_stop", cost_microusd=live.cost_microusd, cap_microusd=live.cap_microusd
                )
                live.tree.kill()

    # ----------------------------------------------------------------------- finalize
    def _finalize(self, live: _Live, code: int | None) -> None:
        res = live.result
        ok = bool(res and res.get("ok"))
        if live.intent is None and res and res.get("subtype") == "error_max_budget_usd":
            # The SDK's own cap fired before our estimate did (SDK_NOTES SDK-4): same outcome.
            live.intent = "budget"
        if live.intent in _OUTCOMES:
            run_status, task_status, reason = _OUTCOMES[live.intent]
        elif code == 0 and ok:
            run_status, task_status, reason = (
                "SUCCEEDED",
                "TESTING",
                "worker finished; gate pending",
            )
        elif res is not None and not ok:
            run_status, task_status, reason = "FAILED", "FAILED", "worker reported failure"
        else:
            run_status, task_status, reason = "CRASHED", "FAILED", f"worker died (exit {code})"
        if res and not ok and live.intent is None and run_status == "FAILED":
            reason = str(res.get("text") or reason)[:300]
        self._reconcile_total(live, res)
        live.log.append("run_ended", status=run_status, exit_code=code, task_status=task_status)
        self._finish_rows(live.run_id, live.task_id, run_status, code, reason, live.session_id)
        try:
            self.tasks.transition(live.task_id, task_status, actor=ACTOR, reason=reason)
        except IllegalTransition as exc:  # e.g. the task was cancelled through the API meanwhile
            log.info("task %s stays as is after run %s: %s", live.task_id, live.run_id, exc)

    def _reconcile_total(self, live: _Live, res: dict[str, Any] | None) -> None:
        """If the final result reports more than the per-message events, ledger the difference.

        Skipped for resumed sessions: their reported total includes earlier runs' spend, so
        per-message deltas are the only trustworthy figure (SDK_NOTES SDK-4).
        """
        total = res.get("total_cost_usd") if res else None
        if live.resumed or not isinstance(total, int | float):
            return
        delta = int(Decimal(str(total)) * _MICRO) - live.cost_microusd
        if delta <= 0:
            return
        try:
            with Session(self.engine) as s:
                s.add(
                    UsageEvent(
                        run_id=live.run_id,
                        task_id=live.task_id,
                        project_id=live.project_id,
                        dedup_key=f"result:{live.run_id}",
                        model="reconcile",
                        cost_microusd=delta,
                    )
                )
                s.commit()
            live.cost_microusd += delta
        except IntegrityError:
            pass

    def _finish_rows(
        self,
        run_id: int,
        task_id: int,
        status: str,
        code: int | None,
        reason: str,
        session_id: str | None = None,
    ) -> None:
        with Session(self.engine) as s:
            r = s.get(Run, run_id)
            if r is None:
                return
            r.status, r.ended_at, r.exit_code = status, utcnow(), code
            if session_id:
                r.session_id = session_id
            r.result_json = json.dumps({"reason": redact(reason)})
            t = s.get(Task, task_id)
            s.add(
                Event(
                    project_id=t.project_id if t else None,
                    task_id=task_id,
                    run_id=run_id,
                    kind="run.ended",
                    detail=json.dumps(
                        {"status": status, "exit_code": code, "reason": redact(reason)}
                    ),
                )
            )
            s.commit()

    def _fail_task(self, task_id: int, reason: str) -> None:
        try:
            self.tasks.transition(task_id, "FAILED", actor=ACTOR, reason=reason)
        except IllegalTransition:
            pass

    def wait(self, run_id: int, timeout_s: float = 30.0) -> RunInfo:
        """Block until a run's monitor thread has finished (tests and CLI helpers)."""
        t = self._threads.get(run_id)
        if t:
            t.join(timeout_s)
        return self.get_run(run_id)
