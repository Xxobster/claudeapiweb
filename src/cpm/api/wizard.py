"""First-run wizard API (MASTER_PROMPT section 5). Money is Decimal in, integer micro-USD stored."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator

from cpm.db.settings_repo import get_setting, set_setting
from cpm.security.paths import PathPolicyError, validate_root
from cpm.security.secrets_store import ANTHROPIC_KEY, SecretStoreUnavailable

router = APIRouter(prefix="/api/wizard", tags=["wizard"])

MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5", "claude-fable-5-1")
_MICRO = Decimal(1_000_000)
_CFG = "wizard.config"
_DONE = "wizard.completed"
_ROOTS = "approved_roots"


class WizardConfig(BaseModel):
    default_project_name: str = Field(min_length=1, max_length=80)
    project_dir: str
    git_url: str | None = None
    default_model: Literal[MODELS] = "claude-sonnet-5-5"  # type: ignore[valid-type]
    default_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    task_budget_usd: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    project_budget_usd: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    global_budget_usd: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    max_parallel_agents: int = Field(ge=1, le=8, default=3)

    @field_validator("git_url")
    @classmethod
    def _git_url(cls, v: str | None) -> str | None:
        if v in (None, ""):
            return None
        if v.startswith("-") or not v.startswith(("https://", "git@", "ssh://")):
            raise ValueError("git_url must be an https/ssh remote and must not start with '-'")
        if any(c.isspace() or ord(c) < 32 for c in v):
            raise ValueError("git_url contains whitespace")
        parts = urlsplit(v) if "://" in v else None
        if parts is not None and (
            parts.password is not None or (parts.username and parts.scheme == "https")
        ):
            raise ValueError("git_url must not embed credentials")
        return v

    @model_validator(mode="after")
    def _ordered(self) -> WizardConfig:
        if not self.task_budget_usd <= self.project_budget_usd <= self.global_budget_usd:
            raise ValueError("budgets must satisfy task <= project <= global")
        return self


def _public(cfg: dict[str, object]) -> dict[str, object]:
    out = dict(cfg)
    for k in ("task", "project", "global"):
        micro = out.pop(f"{k}_budget_microusd", None)
        if micro is not None:
            out[f"{k}_budget_usd"] = str(Decimal(int(str(micro))) / _MICRO)
    return out


@router.get("/status")
def status(request: Request) -> dict[str, object]:
    engine = request.app.state.engine
    try:
        key = request.app.state.secrets.get(ANTHROPIC_KEY) is not None
    except SecretStoreUnavailable:
        key = False
    cfg = get_setting(engine, _CFG)
    return {
        "api_key_configured": key,
        "config_saved": cfg is not None,
        "completed": bool(get_setting(engine, _DONE, False)),
        "billing_mode": "api",
        "live_runs_allowed": key and cfg is not None,  # a cap > 0 is mandatory in the schema
        "config": _public(cfg) if cfg else None,
        "models": list(MODELS),
    }


@router.put("/config")
def save_config(body: WizardConfig, request: Request) -> dict[str, object]:
    engine = request.app.state.engine
    try:
        root = validate_root(body.project_dir)
    except PathPolicyError as exc:
        raise HTTPException(422, f"project_dir rejected: {exc}") from exc
    data_dir = request.app.state.settings.data_dir.resolve()
    if root == data_dir or data_dir in root.parents or root in data_dir.parents:
        raise HTTPException(422, "project_dir must not overlap the CPM data directory")
    cfg = body.model_dump(
        mode="json", exclude={"task_budget_usd", "project_budget_usd", "global_budget_usd"}
    )
    cfg["project_dir"] = str(root)
    for k in ("task", "project", "global"):
        cfg[f"{k}_budget_microusd"] = int(getattr(body, f"{k}_budget_usd") * _MICRO)
    set_setting(engine, _CFG, cfg)
    roots = get_setting(engine, _ROOTS, [])
    if str(root) not in roots:
        set_setting(engine, _ROOTS, [*roots, str(root)])
    set_setting(engine, _DONE, True)
    return status(request)
