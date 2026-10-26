"""Cost ledger, budget admission and thresholds (PLAN.md WP8, MASTER_PROMPT section 12).

Money is integer micro-USD everywhere (docs/THREATS.md T-16). Rules this module enforces:

* `usage_events` is the append-only ledger; one SDK message id is counted once (`dedup_key`).
  Spend is summed from it, never kept in a counter that a restart or a resume could reset.
* Admission (`admit`) runs in one `BEGIN IMMEDIATE` transaction: it reads spent + held money and
  inserts the reservation before any other writer can look, so two workers can never hold the
  same remaining dollars (T-13). `exposure = spent + sum(max(0, held - spent by that run))`.
* Caps: month (global), project per month, task for its whole life; a manual credit balance, when
  entered, is a fourth ceiling. Each can be raised by a top-up row in `budgets`.
* Everything shown is labelled estimate or reconciled (T-14).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import Connection, Engine, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cpm.budget.pricing import PriceTable
from cpm.db.models import (
    Budget,
    BudgetReservation,
    Event,
    Setting,
    UsageEvent,
    utcnow,
)
from cpm.db.settings_repo import get_setting, set_setting
from cpm.security.redaction import redact

LEVELS = (70, 90, 100)
MAX_TOPUP_MICROUSD = 10_000_000_000  # 10,000 USD per top-up: a typo guard
CFG = "wizard.config"
CREDIT = "budget.credit"
ALERTS = "budget.alerts"
Scope = Literal["month", "project", "task"]


class BudgetError(Exception):
    status = 422


class BudgetDenied(BudgetError):
    """Admission refused. `.detail` says which ceiling, how much is left and why."""

    status = 409

    def __init__(self, message: str, detail: dict[str, Any]) -> None:
        super().__init__(message)
        self.detail = detail


def period_of(dt: datetime | None = None) -> str:
    return (dt or utcnow()).astimezone(UTC).strftime("%Y-%m")


def month_bounds(period: str) -> tuple[datetime, datetime]:
    try:
        y, m = int(period[:4]), int(period[5:7])
        if period[4] != "-" or len(period) != 7 or not 1 <= m <= 12:
            raise ValueError
    except (ValueError, IndexError) as exc:
        raise BudgetError("period must look like 2026-10") from exc
    start = datetime(y, m, 1, tzinfo=UTC)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)
    return start, end


@dataclass
class Caps:
    month: int
    project: int | None  # None = no project cap
    task: int | None


@dataclass
class Reservation:
    id: int
    task_id: int
    project_id: int
    amount_microusd: int


@dataclass
class Alert:
    scope: str
    ref: str
    level: int
    spent_microusd: int
    cap_microusd: int
    checkpoint_requested: bool = field(default=False)


class BudgetLedger:
    def __init__(self, engine: Engine, prices: PriceTable | None = None) -> None:
        self.engine = engine
        self.prices = prices or PriceTable(engine)
        self._alert_lock = threading.Lock()

    # ------------------------------------------------------------------ config and caps
    def configured(self) -> bool:
        cfg = get_setting(self.engine, CFG)
        return bool(cfg and int(cfg.get("global_budget_microusd", 0)) > 0)

    @staticmethod
    def _cfg_int(cfg: dict[str, Any] | None, key: str) -> int:
        return int((cfg or {}).get(key, 0) or 0)

    def _override(self, conn: Connection, scope: str, ref: str) -> int | None:
        v = conn.execute(
            select(Budget.cap_microusd).where(Budget.scope == scope, Budget.scope_ref == ref)
        ).scalar()
        return None if v is None else int(v)

    def _caps(self, conn: Connection, project_id: int, task_id: int, period: str) -> Caps:
        cfg = _setting(conn, CFG)
        if self._cfg_int(cfg, "global_budget_microusd") <= 0:
            raise BudgetDenied(
                "no monthly budget cap configured: finish the first-run wizard",
                {"scope": "month", "reason": "no_cap"},
            )
        month = self._override(conn, "month", period)
        project = self._override(conn, "project", str(project_id))
        task = self._override(conn, "task", str(task_id))
        pcfg = self._cfg_int(cfg, "project_budget_microusd")
        tcfg = self._cfg_int(cfg, "task_budget_microusd")
        return Caps(
            month if month is not None else self._cfg_int(cfg, "global_budget_microusd"),
            project if project is not None else (pcfg or None),
            task if task is not None else (tcfg or None),
        )

    # ------------------------------------------------------------------ spend and holds
    @staticmethod
    def _spent(conn: Connection, *where: Any) -> int:
        v = conn.execute(select(func.coalesce(func.sum(UsageEvent.cost_microusd), 0)).where(*where))
        return int(v.scalar() or 0)

    def _adjustment(self, conn: Connection, period: str) -> int:
        rec = (_setting(conn, "budget.reconciliation") or {}).get(period)
        return int(rec["adjustment_microusd"]) if rec else 0

    def _held(self, conn: Connection) -> list[tuple[int, int, int, int]]:
        """(task_id, project_id, still-unspent hold, reservation id) for every held reservation."""
        rows = conn.execute(
            select(
                BudgetReservation.id, BudgetReservation.task_id, BudgetReservation.project_id,
                BudgetReservation.amount_microusd, BudgetReservation.run_id,
            ).where(BudgetReservation.status == "held")
        ).all()  # fmt: skip
        run_ids = [r.run_id for r in rows if r.run_id is not None]
        spent: dict[int, int] = {}
        if run_ids:
            spent = {
                int(rid): int(c)
                for rid, c in conn.execute(
                    select(UsageEvent.run_id, func.sum(UsageEvent.cost_microusd))
                    .where(UsageEvent.run_id.in_(run_ids))
                    .group_by(UsageEvent.run_id)
                )
            }
        return [
            (r.task_id, r.project_id, max(0, r.amount_microusd - spent.get(r.run_id, 0)), r.id)
            for r in rows
        ]  # fmt: skip

    def _month_spent(self, conn: Connection, period: str, *extra: Any) -> int:
        start, end = month_bounds(period)
        return self._spent(conn, UsageEvent.ts >= start, UsageEvent.ts < end, *extra)

    # ------------------------------------------------------------------ admission
    @contextmanager
    def _write_tx(self) -> Iterator[Connection]:
        conn = self.engine.connect()
        try:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            yield conn
            conn.exec_driver_sql("COMMIT")
        except BaseException:
            try:
                conn.exec_driver_sql("ROLLBACK")
            except Exception:  # noqa: S110 (no transaction left to roll back)
                pass
            raise
        finally:
            conn.close()

    def admit(
        self, task_id: int, project_id: int, amount_microusd: int, *,
        task_cap_default: int | None = None,
    ) -> Reservation:  # fmt: skip
        """Hold `amount_microusd` for one run or raise `BudgetDenied`. Atomic across threads."""
        if amount_microusd <= 0:
            raise BudgetError("the amount to reserve must be positive")
        period = period_of()
        with self._write_tx() as conn:
            caps = self._caps(conn, project_id, task_id, period)
            if caps.task is None:
                caps.task = task_cap_default
            held = self._held(conn)
            held_all = sum(h[2] for h in held)
            checks: list[tuple[str, str, int, int, int]] = []  # scope, ref, cap, spent, held
            checks.append((
                "month", period, caps.month,
                self._month_spent(conn, period) + self._adjustment(conn, period), held_all,
            ))  # fmt: skip
            if caps.project is not None:
                checks.append((
                    "project", str(project_id), caps.project,
                    self._month_spent(conn, period, UsageEvent.project_id == project_id),
                    sum(h[2] for h in held if h[1] == project_id),
                ))  # fmt: skip
            if caps.task is not None:
                checks.append((
                    "task", str(task_id), caps.task,
                    self._spent(conn, UsageEvent.task_id == task_id),
                    sum(h[2] for h in held if h[0] == task_id),
                ))  # fmt: skip
            credit = _setting(conn, CREDIT)
            if credit:
                as_of = datetime.fromisoformat(credit["as_of"])
                since = self._spent(conn, UsageEvent.ts >= as_of)
                checks.append((
                    "credit", "balance", int(credit["microusd"]), since, held_all,
                ))  # fmt: skip
            for scope, ref, cap, spent, held_here in checks:
                remaining = cap - spent - held_here
                if amount_microusd > remaining:
                    raise BudgetDenied(
                        f"not enough {scope} budget: {max(remaining, 0)} micro-USD free, "
                        f"{amount_microusd} requested",
                        {
                            "scope": scope, "ref": ref, "cap_microusd": cap,
                            "spent_microusd": spent, "held_microusd": held_here,
                            "remaining_microusd": max(remaining, 0),
                            "requested_microusd": amount_microusd, "reason": "insufficient",
                        },
                    )  # fmt: skip
            inserted = conn.execute(
                insert(BudgetReservation).values(
                    task_id=task_id,
                    project_id=project_id,
                    amount_microusd=amount_microusd,
                    status="held",
                    created_at=utcnow(),
                )
            ).inserted_primary_key
            assert inserted is not None  # noqa: S101 (INSERT always yields a key)
            return Reservation(int(inserted[0]), task_id, project_id, amount_microusd)

    def attach_run(self, reservation_id: int, run_id: int) -> None:
        with Session(self.engine) as s:
            s.execute(
                update(BudgetReservation)
                .where(BudgetReservation.id == reservation_id, BudgetReservation.status == "held")
                .values(run_id=run_id)
            )
            s.commit()

    def release(self, reservation_id: int) -> bool:
        """Release a hold exactly once (False if it was already released)."""
        with Session(self.engine) as s:
            res = s.execute(
                update(BudgetReservation)
                .where(BudgetReservation.id == reservation_id, BudgetReservation.status == "held")
                .values(status="released", released_at=utcnow())
            )
            s.commit()
            return bool(res.rowcount)  # type: ignore[attr-defined]

    def release_run(self, run_id: int) -> int:
        with Session(self.engine) as s:
            res = s.execute(
                update(BudgetReservation)
                .where(BudgetReservation.run_id == run_id, BudgetReservation.status == "held")
                .values(status="released", released_at=utcnow())
            )
            s.commit()
            return int(res.rowcount)  # type: ignore[attr-defined]

    def release_orphans(self, live_run_ids: set[int]) -> int:
        """For restart recovery (WP9): free holds whose run is not alive. Holds with no run yet
        are young admissions in flight and are only released when `live_run_ids` is exhaustive
        and they are older than a minute."""
        n = 0
        with Session(self.engine) as s:
            for r in s.scalars(select(BudgetReservation).where(BudgetReservation.status == "held")):
                dead = r.run_id is not None and r.run_id not in live_run_ids
                stale = r.run_id is None and (utcnow() - _aware(r.created_at)).total_seconds() > 60
                if dead or stale:
                    r.status, r.released_at = "released", utcnow()
                    n += 1
            s.commit()
        return n

    # ------------------------------------------------------------------ usage
    def record_usage(
        self, *, run_id: int, task_id: int, project_id: int, message_id: str, model: str,
        input_tokens: int, output_tokens: int, cache_read: int, cache_write: int,
        cost_microusd: int | None, price_version: int | None = None,
    ) -> int | None:  # fmt: skip
        """Append one ledger row. Returns the cost counted, or None if the id was seen before.

        With `cost_microusd=None` the cost comes from the price table (the normal, API-billed
        path: a worker never decides what it cost).
        """
        if cost_microusd is None:
            cost_microusd, price_version = self.prices.estimate(
                model, input_tokens, output_tokens, cache_read, cache_write
            )
        row = UsageEvent(
            run_id=run_id, task_id=task_id, project_id=project_id,
            dedup_key=f"msg:{message_id}"[:128], model=model[:64],
            input_tokens=input_tokens, output_tokens=output_tokens,
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            cost_microusd=cost_microusd, reconciled=False, price_version=price_version,
        )  # fmt: skip
        try:
            with Session(self.engine) as s:
                s.add(row)
                s.commit()
        except IntegrityError:
            return None
        return cost_microusd

    def add_adjustment_row(self, run_id: int, task_id: int, project_id: int, delta: int) -> bool:
        """Ledger the part of a result's total that no per-message row covered."""
        try:
            with Session(self.engine) as s:
                s.add(
                    UsageEvent(
                        run_id=run_id, task_id=task_id, project_id=project_id,
                        dedup_key=f"result:{run_id}", model="reconcile", cost_microusd=delta,
                    )
                )  # fmt: skip
                s.commit()
            return True
        except IntegrityError:
            return False

    # ------------------------------------------------------------------ thresholds
    def check_thresholds(self, project_id: int, task_id: int) -> list[Alert]:
        """Emit one event per newly crossed 70/90/100 % level of the month, project or task cap."""
        period = period_of()
        with self.engine.connect() as conn:
            try:
                caps = self._caps(conn, project_id, task_id, period)
            except BudgetDenied:
                return []
            scopes: list[tuple[str, str, int, int]] = [
                ("month", period, caps.month,
                 self._month_spent(conn, period) + self._adjustment(conn, period)),
            ]  # fmt: skip
            if caps.project is not None:
                scopes.append((
                    "project", str(project_id), caps.project,
                    self._month_spent(conn, period, UsageEvent.project_id == project_id),
                ))  # fmt: skip
            if caps.task is not None:
                scopes.append((
                    "task", str(task_id), caps.task,
                    self._spent(conn, UsageEvent.task_id == task_id),
                ))  # fmt: skip
        out: list[Alert] = []
        with self._alert_lock:
            seen: dict[str, int] = get_setting(self.engine, ALERTS, {})
            changed = False
            for scope, ref, cap, spent in scopes:
                if cap <= 0:
                    continue
                pct = spent * 100 // cap
                level = max((lv for lv in LEVELS if pct >= lv), default=0)
                # a raised cap starts a fresh series of alerts; project caps restart every month
                per = f"{period}:" if scope == "project" else ""
                key = f"{scope}:{ref}:{per}{cap}"
                if level and level > seen.get(key, 0):
                    seen[key] = level
                    changed = True
                    out.append(
                        Alert(scope, ref, level, spent, cap, checkpoint_requested=level >= 90)
                    )
            if changed:
                set_setting(self.engine, ALERTS, seen)
        if out:
            with Session(self.engine) as s:
                for a in out:
                    s.add(
                        Event(
                            project_id=project_id,
                            task_id=task_id,
                            kind="budget.threshold",
                            detail=json.dumps(
                                {
                                    "scope": a.scope,
                                    "ref": a.ref,
                                    "level": a.level,
                                    "spent_microusd": a.spent_microusd,
                                    "cap_microusd": a.cap_microusd,
                                    "estimate": True,
                                },
                            ),
                        )  # fmt: skip
                    )
                s.commit()
        return out

    def record_exhaustion(self, project_id: int, task_id: int, run_id: int, detail: str) -> None:
        with Session(self.engine) as s:
            s.add(
                Event(
                    project_id=project_id, task_id=task_id, run_id=run_id,
                    kind="budget.exhausted",
                    detail=redact(json.dumps({
                        "reason": detail,
                        "options": ["add_budget", "resume_no_increase", "reassign_model",
                                    "defer", "cancel"],
                    })),
                )
            )  # fmt: skip
            s.commit()

    # ------------------------------------------------------------------ caps, credit, reconcile
    def effective_cap(
        self, scope: Scope, ref: str, project_id: int = 0, task_id: int = 0,
        task_default: int | None = None,
    ) -> int:  # fmt: skip
        period = ref if scope == "month" else period_of()
        with self.engine.connect() as conn:
            caps = self._caps(conn, project_id, task_id, period)
        cap = {"month": caps.month, "project": caps.project, "task": caps.task or task_default}[
            scope
        ]  # the same task ceiling admission uses (`admit(task_cap_default=...)`)
        if cap is None:
            raise BudgetError(f"no {scope} cap is configured")
        return cap

    def add_to_cap(
        self, scope: Scope, ref: str, amount_microusd: int, *, project_id: int = 0,
        task_id: int = 0, task_default: int | None = None,
    ) -> int:  # fmt: skip
        """Add a fixed amount to a cap (the user's explicit authorisation). Returns the new cap."""
        if not 0 < amount_microusd <= MAX_TOPUP_MICROUSD:
            raise BudgetError("top-up must be positive and at most 10,000 USD")
        new = self.effective_cap(scope, ref, project_id, task_id, task_default) + amount_microusd
        self._set_override(scope, ref, new)
        return new

    def _set_override(self, scope: str, ref: str, cap: int) -> None:
        with Session(self.engine) as s:
            row = s.scalars(
                select(Budget).where(Budget.scope == scope, Budget.scope_ref == ref)
            ).first()
            if row is None:
                s.add(Budget(scope=scope, scope_ref=ref, cap_microusd=cap))
            else:
                row.cap_microusd = cap
            s.commit()

    def set_credit_balance(self, microusd: int) -> dict[str, Any]:
        """Manual credit balance: the API cannot read it (SDK_NOTES section 6)."""
        if microusd < 0:
            raise BudgetError("balance cannot be negative")
        rec = {"microusd": microusd, "as_of": utcnow().isoformat()}
        set_setting(self.engine, CREDIT, rec)
        return rec

    def clear_credit_balance(self) -> None:
        with Session(self.engine) as s:
            row = s.get(Setting, CREDIT)
            if row is not None:
                s.delete(row)
                s.commit()

    def reconcile(
        self, period: str, actual_microusd: int, source: str = "console"
    ) -> dict[str, Any]:
        """Record the real spend of a month (read from the Console by the user)."""
        if actual_microusd < 0:
            raise BudgetError("actual spend cannot be negative")
        start, end = month_bounds(period)
        with self.engine.connect() as conn:
            estimate = self._month_spent(conn, period)
        rec = {
            "actual_microusd": actual_microusd, "estimate_microusd": estimate,
            "adjustment_microusd": actual_microusd - estimate, "source": source[:40],
            "at": utcnow().isoformat(),
        }  # fmt: skip
        allrec: dict[str, Any] = get_setting(self.engine, "budget.reconciliation", {})
        allrec[period] = rec
        set_setting(self.engine, "budget.reconciliation", allrec)
        with Session(self.engine) as s:
            s.execute(
                update(UsageEvent)
                .where(UsageEvent.ts >= start, UsageEvent.ts < end)
                .values(reconciled=True)
            )
            s.commit()
        return rec

    def defer_until_renewal(self, task_id: int) -> str:
        """Mark a PAUSED_BUDGET task as waiting for the next month (the scheduler honours it)."""
        _, end = month_bounds(period_of())
        deferred: dict[str, str] = get_setting(self.engine, "budget.deferred", {})
        deferred[str(task_id)] = end.isoformat()
        set_setting(self.engine, "budget.deferred", deferred)
        return end.isoformat()

    def deferred_until(self, task_id: int) -> datetime | None:
        v = get_setting(self.engine, "budget.deferred", {}).get(str(task_id))
        return datetime.fromisoformat(v) if v else None

    # ------------------------------------------------------------------ reporting
    def task_spent(self, task_id: int) -> int:
        with self.engine.connect() as conn:
            return self._spent(conn, UsageEvent.task_id == task_id)

    def summary(self, period: str | None = None) -> dict[str, Any]:
        period = period or period_of()
        cfg = get_setting(self.engine, CFG) or {}
        start, end = month_bounds(period)
        with self.engine.connect() as conn:
            held = self._held(conn)
            override = self._override(conn, "month", period)
            cap = override if override is not None else self._cfg_int(cfg, "global_budget_microusd")
            est = self._month_spent(conn, period)
            reconciled = self._month_spent(conn, period, UsageEvent.reconciled.is_(True))
            adj = self._adjustment(conn, period)
            rec = (_setting(conn, "budget.reconciliation") or {}).get(period)
            per_project = {
                int(pid): int(c)
                for pid, c in conn.execute(
                    select(UsageEvent.project_id, func.sum(UsageEvent.cost_microusd))
                    .where(UsageEvent.ts >= start, UsageEvent.ts < end)
                    .group_by(UsageEvent.project_id)
                )
            }
            credit = _setting(conn, CREDIT)
            credit_spent = (
                self._spent(conn, UsageEvent.ts >= datetime.fromisoformat(credit["as_of"]))
                if credit
                else 0
            )
        spent = est + adj
        held_all = sum(h[2] for h in held)
        pcfg = self._cfg_int(cfg, "project_budget_microusd")
        with self.engine.connect() as conn2:
            overrides = {
                int(r.scope_ref): int(r.cap_microusd)
                for r in conn2.execute(select(Budget).where(Budget.scope == "project"))
            }
        projects = []
        for pid in sorted(set(per_project) | {h[1] for h in held}):
            pcap = overrides.get(pid, pcfg) or None
            ps = per_project.get(pid, 0)
            ph = sum(h[2] for h in held if h[1] == pid)
            projects.append({
                "project_id": pid, "spent_microusd": ps, "held_microusd": ph,
                "cap_microusd": pcap,
                "percent": (ps * 100 // pcap) if pcap else None,
                "remaining_microusd": max(pcap - ps - ph, 0) if pcap else None,
            })  # fmt: skip
        out: dict[str, Any] = {
            "period": period,
            "configured": cap > 0,
            "month": {
                "cap_microusd": cap, "spent_microusd": spent, "held_microusd": held_all,
                "remaining_microusd": max(cap - spent - held_all, 0),
                "percent": (spent * 100 // cap) if cap else None,
                "level": max((lv for lv in LEVELS if cap and spent * 100 // cap >= lv), default=0),
                "estimate_microusd": est, "reconciled_rows_microusd": reconciled,
                "reconciliation": rec,
                "label": "reconciled with the Console" if rec else "estimate (SDK/price table)",
            },
            "projects": projects,
            "credit_balance": None,
            "limitations": [
                "Costs are estimates from the price table until reconciled with the Console.",
                "The Anthropic API does not expose your remaining credit; enter it by hand.",
                "max_budget_usd is a soft limit in the SDK: CPM adds its own admission and caps.",
            ],
        }  # fmt: skip
        if credit:
            out["credit_balance"] = {
                "entered_microusd": credit["microusd"], "as_of": credit["as_of"],
                "estimated_remaining_microusd": max(int(credit["microusd"]) - credit_spent, 0),
                "label": "estimate: entered balance minus spend since it was entered",
            }  # fmt: skip
        return out

    def reservations(self, only_held: bool = True) -> list[dict[str, Any]]:
        with Session(self.engine) as s:
            q = select(BudgetReservation).order_by(BudgetReservation.id)
            if only_held:
                q = q.where(BudgetReservation.status == "held")
            return [
                {"id": r.id, "task_id": r.task_id, "project_id": r.project_id, "run_id": r.run_id,
                 "amount_microusd": r.amount_microusd, "status": r.status}
                for r in s.scalars(q)
            ]  # fmt: skip


def _setting(conn: Connection, key: str) -> Any:
    v = conn.execute(select(Setting.value).where(Setting.key == key)).scalar()
    return None if v is None else json.loads(v)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
