"""Budget and cost-center endpoints (WP8). All behind the security guard.

Money in and out is USD as a decimal string; the ledger stores integer micro-USD. Every figure
is labelled estimate or reconciled. There is no way here to read or change the API key.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from cpm.api.wizard import MODELS
from cpm.budget.ledger import BudgetError, BudgetLedger, period_of
from cpm.budget.pricing import PriceError, PriceTable, Tier
from cpm.db.models import Event, Task
from cpm.tasks.service import TaskError, TaskService

router = APIRouter(prefix="/api", tags=["budget"])
_MICRO = Decimal(1_000_000)
ACTOR = "user"
UsdAmount = Field(gt=0, max_digits=12, decimal_places=6)


def _led(request: Request) -> BudgetLedger:
    return request.app.state.supervisor.ledger  # type: ignore[no-any-return]


def _task_default(request: Request, billing_mode: str) -> int | None:
    """The ceiling API tasks get when no task budget is configured (same as admission)."""
    live_max = request.app.state.settings.live_run_max_microusd
    return int(live_max) if billing_mode == "api" else None


def _micro(usd: Decimal) -> int:
    return int(usd * _MICRO)


def _usd(micro: int | None) -> str | None:
    return None if micro is None else str(Decimal(micro) / _MICRO)


def _call(fn: Any, *a: Any, **kw: Any) -> Any:
    try:
        return fn(*a, **kw)
    except BudgetError as e:
        body: Any = {"error": str(e)}
        if hasattr(e, "detail"):
            body = {"error": str(e), **e.detail}
        raise HTTPException(e.status, body) from e
    except (PriceError, TaskError) as e:
        raise HTTPException(getattr(e, "status", 422), str(e)) from e


def _with_usd(d: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out[k] = _with_usd(v)
        elif isinstance(v, list):
            out[k] = [_with_usd(x) if isinstance(x, dict) else x for x in v]
        else:
            out[k] = v
        if k.endswith("_microusd") and isinstance(v, int):
            out[k[: -len("_microusd")] + "_usd"] = _usd(v)
    return out


@router.get("/budget")
def budget_summary(request: Request, period: str | None = None) -> dict[str, Any]:
    return _with_usd(_call(_led(request).summary, period))


@router.get("/budget/reservations")
def reservations(request: Request) -> list[Any]:
    return _led(request).reservations()


@router.get("/tasks/{task_id}/budget")
def task_budget(request: Request, task_id: int) -> dict[str, Any]:
    led = _led(request)
    task = _call(TaskService(request.app.state.engine).get, task_id)
    spent = led.task_spent(task_id)
    try:
        cap: int | None = led.effective_cap(
            "task",
            str(task_id),
            task.project_id,
            task_id,
            _task_default(request, task.billing_mode),
        )
    except BudgetError:
        cap = None
    out = {
        "task_id": task_id, "status": task.status, "spent_microusd": spent,
        "cap_microusd": cap,
        "remaining_microusd": max(cap - spent, 0) if cap is not None else None,
        "label": "estimate (SDK/price table)",
    }  # fmt: skip
    return _with_usd(out)


class CreditIn(BaseModel):
    balance_usd: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


@router.put("/budget/credit-balance")
def set_credit(request: Request, body: CreditIn) -> dict[str, Any]:
    return _with_usd({"credit": _call(_led(request).set_credit_balance, _micro(body.balance_usd))})


@router.delete("/budget/credit-balance")
def clear_credit(request: Request) -> dict[str, Any]:
    _led(request).clear_credit_balance()
    return {"cleared": True}


class ReconcileIn(BaseModel):
    period: str = Field(default="", max_length=7)
    actual_usd: Decimal = Field(ge=0, max_digits=12, decimal_places=6)


@router.post("/budget/reconcile")
def reconcile(request: Request, body: ReconcileIn) -> dict[str, Any]:
    rec = _call(_led(request).reconcile, body.period or period_of(), _micro(body.actual_usd))
    return _with_usd({"reconciliation": rec})


class TopUpIn(BaseModel):
    scope: Literal["month", "project", "task"]
    ref: str = Field(default="", max_length=16)
    amount_usd: Decimal = UsdAmount


@router.post("/budget/top-up")
def top_up(request: Request, body: TopUpIn) -> dict[str, Any]:
    """Add a fixed amount to a cap. The caller is the explicit authorisation to spend more."""
    led = _led(request)
    project_id = task_id = 0
    default: int | None = None
    ref = body.ref
    if body.scope == "month":
        ref = ref or period_of()
    else:
        if not ref.isdigit():
            raise HTTPException(422, "ref must be the numeric project or task id")
        if body.scope == "project":
            project_id = int(ref)
        else:
            task_id = int(ref)
            t = _call(TaskService(request.app.state.engine).get, task_id)
            project_id, default = t.project_id, _task_default(request, t.billing_mode)
    new = _call(led.add_to_cap, body.scope, ref, _micro(body.amount_usd),
                project_id=project_id, task_id=task_id,
                task_default=default)  # fmt: skip
    return _with_usd({"scope": body.scope, "ref": ref, "cap_microusd": new})


# --------------------------------------------------------------------------------- prices
class TierIn(BaseModel):
    input_usd: Decimal = Field(ge=0, le=1000, max_digits=12, decimal_places=6)
    output_usd: Decimal = Field(ge=0, le=1000, max_digits=12, decimal_places=6)
    cache_read_usd: Decimal = Field(ge=0, le=1000, max_digits=12, decimal_places=6)
    cache_write_usd: Decimal = Field(ge=0, le=1000, max_digits=12, decimal_places=6)

    def tier(self) -> Tier:
        return Tier(
            _micro(self.input_usd), _micro(self.output_usd),
            _micro(self.cache_read_usd), _micro(self.cache_write_usd),
        )  # fmt: skip


class PriceIn(TierIn):
    """USD per million tokens. `long` applies once the prompt exceeds `long_threshold` tokens."""

    long_threshold: int | None = Field(default=None, ge=1, le=10_000_000)
    long: TierIn | None = None
    source: str = Field(min_length=1, max_length=200)
    verified_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


def _price_json(r: Any) -> dict[str, Any]:
    def tier(t: Tier) -> dict[str, str | None]:
        return {
            "input_usd": _usd(t.input), "output_usd": _usd(t.output),
            "cache_read_usd": _usd(t.cache_read), "cache_write_usd": _usd(t.cache_write),
        }  # fmt: skip

    return {
        "model": r.model, "version": r.version, **tier(r.base),
        "long_threshold": r.long_threshold, "long": tier(r.long) if r.long else None,
        "source": r.source, "verified_on": r.verified_on, "unit": "USD per million tokens",
    }  # fmt: skip


@router.get("/budget/prices")
def list_prices(request: Request) -> list[Any]:
    table: PriceTable = _led(request).prices
    return [_price_json(r) for r in table.current().values()]


@router.put("/budget/prices/{model}")
def put_price(request: Request, model: str, body: PriceIn) -> dict[str, Any]:
    """Append a new version of a model's price (old versions stay for past ledger rows)."""
    table: PriceTable = _led(request).prices
    row = _call(
        table.add_version, model, body.tier(), long_threshold=body.long_threshold,
        long=body.long.tier() if body.long else None, source=body.source,
        verified_on=body.verified_on,
    )  # fmt: skip
    return _price_json(row)


# --------------------------------------------------------------- PAUSED_BUDGET decisions
class BudgetActionIn(BaseModel):
    action: Literal["add_budget", "resume_no_increase", "reassign_model", "defer", "cancel"]
    amount_usd: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=6)
    model: str | None = Field(default=None, max_length=64)


@router.post("/tasks/{task_id}/budget-action")
def budget_action(request: Request, task_id: int, body: BudgetActionIn) -> dict[str, Any]:
    """The five choices when a task stopped for budget (MASTER_PROMPT section 12).

    None of them starts a worker; after `add_budget`, `resume_no_increase` or `reassign_model`
    the user resumes the task through `POST /api/tasks/{id}/resume`, which goes through admission.
    """
    led = _led(request)
    svc = TaskService(request.app.state.engine)
    task = _call(svc.get, task_id)
    if task.status != "PAUSED_BUDGET":
        raise HTTPException(409, f"task is {task.status}, not PAUSED_BUDGET")
    engine = request.app.state.engine
    result: dict[str, Any] = {"action": body.action, "task_id": task_id}
    if body.action == "add_budget":
        if body.amount_usd is None:
            raise HTTPException(422, "amount_usd is required")
        result["cap_microusd"] = _call(
            led.add_to_cap, "task", str(task_id), _micro(body.amount_usd),
            project_id=task.project_id, task_id=task_id,
            task_default=_task_default(request, task.billing_mode),
        )  # fmt: skip
    elif body.action == "resume_no_increase":
        spent = led.task_spent(task_id)
        cap = None
        try:
            cap = led.effective_cap(
                "task",
                str(task_id),
                task.project_id,
                task_id,
                _task_default(request, task.billing_mode),
            )
        except BudgetError:
            pass
        result |= {"spent_microusd": spent, "cap_microusd": cap,
                   "headroom_microusd": None if cap is None else max(cap - spent, 0)}  # fmt: skip
        if cap is not None and spent >= cap:
            raise HTTPException(
                409,
                {"error": "the task has no budget left; correct the accounting (reconcile) or "
                          "add budget", **result},
            )  # fmt: skip
    elif body.action == "reassign_model":
        if body.model not in MODELS:
            raise HTTPException(422, f"model must be one of {', '.join(MODELS)}")
        _call(svc.edit, task_id, actor=ACTOR, model=body.model)
        result["model"] = body.model
    elif body.action == "defer":
        result["defer_until"] = led.defer_until_renewal(task_id)
    else:  # cancel: the worktree and partial work stay on disk (WorktreeManager never deletes)
        _call(svc.cancel, task_id, actor=ACTOR, reason="cancelled after budget exhaustion")
    with Session(engine) as s:
        t = s.get(Task, task_id)
        s.add(Event(project_id=t.project_id if t else None, task_id=task_id,
                    kind="budget.action", detail=json.dumps({"action": body.action})))  # fmt: skip
        s.commit()
    return _with_usd(result)
