"""Pure scheduling rules: configuration, queue order, retry decisions.

Nothing here touches the database or the clock, so every rule has a deterministic unit test.
Order of the ready queue (PLAN.md WP7, Nirandyr rule 27 "long jobs yield to short ones"):
lower `priority` number first; among heavy tasks of the same priority the smaller estimate goes
first (a longer job yields); the task id breaks every remaining tie, so two ticks over the same
state always pick the same task.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

SETTING_CONFIG = "scheduler.config"
SETTING_PAUSED = "scheduler.paused"
# Failures are retried automatically only for these billing modes unless the user opts in:
# retrying an API task spends money again (CLAUDE.md rule 7).
DEFAULT_RETRY_MODES = ("mock",)
BILLING_MODES = ("mock", "api", "cli")


@dataclass(frozen=True)
class SchedulerConfig:
    per_project_limit: int | None = None  # None = only the wizard global limit
    heavy_limit: int = 1  # DECISIONS Q8; the lock layer enforces the same class:heavy exclusive
    max_auto_retries: int = 2  # automatic FAILED -> READY moves per task (manual retries count)
    identical_failure_limit: int = 2  # N failed runs in a row with the same cause stop retrying
    retry_delay_s: float = 30.0
    hang_timeout_s: float = 120.0
    max_runtime_s: float = 3600.0
    external_scan_s: float = 30.0
    retry_billing_modes: tuple[str, ...] = field(default=DEFAULT_RETRY_MODES)

    def dump(self) -> dict[str, Any]:
        out = asdict(self)
        out["retry_billing_modes"] = list(self.retry_billing_modes)
        return out

    @classmethod
    def load(cls, raw: Any) -> SchedulerConfig:
        """Stored values are clamped, never trusted (a corrupt setting must not stop the loop)."""
        base = cls()
        if not isinstance(raw, dict):
            return base

        def num(key: str, lo: float, hi: float, default: float) -> float:
            v = raw.get(key, default)
            if isinstance(v, bool) or not isinstance(v, int | float):
                return default
            return min(max(float(v), lo), hi)

        ppl = raw.get("per_project_limit")
        modes = raw.get("retry_billing_modes", list(base.retry_billing_modes))
        if not isinstance(modes, list):
            modes = list(base.retry_billing_modes)
        return cls(
            per_project_limit=(
                int(num("per_project_limit", 1, 8, 1)) if isinstance(ppl, int) and ppl else None
            ),
            heavy_limit=int(num("heavy_limit", 1, 1, 1)),  # raising it needs a decision
            max_auto_retries=int(num("max_auto_retries", 0, 5, base.max_auto_retries)),
            identical_failure_limit=int(
                num("identical_failure_limit", 1, 5, base.identical_failure_limit)
            ),
            retry_delay_s=num("retry_delay_s", 0, 3600, base.retry_delay_s),
            hang_timeout_s=num("hang_timeout_s", 1, 3600, base.hang_timeout_s),
            max_runtime_s=num("max_runtime_s", 1, 86_400, base.max_runtime_s),
            external_scan_s=num("external_scan_s", 1, 3600, base.external_scan_s),
            retry_billing_modes=tuple(m for m in modes if m in BILLING_MODES),
        )


@dataclass(frozen=True)
class Candidate:
    task_id: int
    project_id: int
    priority: int
    estimate_microusd: int
    heavy: bool


def order_key(c: Candidate) -> tuple[int, int, int]:
    """Sort key of the ready queue (see module docstring)."""
    return (c.priority, c.estimate_microusd if c.heavy else 0, c.task_id)


_VARIABLE = re.compile(r"0x[0-9a-f]+|\d+", re.IGNORECASE)


def failure_fingerprint(run_status: str, reason: str | None) -> str:
    """Cause of a failed run with numbers (pids, ids, durations, paths' digits) blanked out."""
    text = _VARIABLE.sub("#", (reason or "").strip().lower())
    return f"{run_status}:{' '.join(text.split())[:200]}"


@dataclass(frozen=True)
class FailedRun:
    status: str
    reason: str | None
    ended_at: datetime | None


@dataclass(frozen=True)
class RetryDecision:
    action: str  # retry | wait | give_up
    why: str = ""


def retry_decision(
    runs: Sequence[FailedRun],
    retry_count: int,
    billing_mode: str,
    cfg: SchedulerConfig,
    now: datetime,
) -> RetryDecision:
    """What to do with a FAILED task. `runs` are its runs, oldest first."""
    if billing_mode not in cfg.retry_billing_modes:
        return RetryDecision("give_up", f"automatic retry is off for {billing_mode} tasks")
    if not runs:
        return RetryDecision("give_up", "no run to learn from")
    if retry_count >= cfg.max_auto_retries:
        return RetryDecision("give_up", f"retry limit reached ({cfg.max_auto_retries})")
    n = cfg.identical_failure_limit
    tail = runs[-n:]
    if len(tail) >= n and len({failure_fingerprint(r.status, r.reason) for r in tail}) == 1:
        return RetryDecision("give_up", f"same failure {n} times in a row: {tail[-1].status}")
    ended = runs[-1].ended_at
    if ended is not None and now - ended < timedelta(seconds=cfg.retry_delay_s):
        return RetryDecision("wait", "retry delay not over")
    return RetryDecision("retry", f"attempt {len(runs) + 1}")
