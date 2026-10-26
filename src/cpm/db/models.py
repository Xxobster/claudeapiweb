"""Core tables (PLAN.md WP1). Money is stored as integer micro-dollars (docs/THREATS.md T-16).

Status/enum-like columns are plain strings validated by the state machine in WP4, not by SQL.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    from sqlalchemy import MetaData

    metadata = MetaData(naming_convention=NAMING)


def _ts() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(Text)
    remote_url: Mapped[str | None] = mapped_column(Text)
    integration_mode: Mapped[str] = mapped_column(String(32), default="pr_only")
    workspace_trusted: Mapped[bool] = mapped_column(default=False)
    profile: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()


class ProjectSource(Base):
    """Instruction/task sources discovered or configured for a project (CLAUDE.md, TODO, ...)."""

    __tablename__ = "project_sources"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))  # instruction | task | memory | other
    rel_path: Mapped[str] = mapped_column(Text)
    included: Mapped[bool] = mapped_column(default=True)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("project_id", "rel_path"),)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(32), default="BACKLOG")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    role: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(64))
    effort: Mapped[str | None] = mapped_column(String(16))
    billing_mode: Mapped[str] = mapped_column(String(8), default="api")
    declared_scope: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of paths
    acceptance: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of criteria
    estimate_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    block_reason: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual | prompt | todo
    source_ref: Mapped[str | None] = mapped_column(String(64))  # import dedup hash
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    __table_args__ = (
        CheckConstraint("billing_mode IN ('api','cli','mock')", name="billing_mode"),
        Index("ix_tasks_project_status", "project_id", "status"),
        UniqueConstraint("project_id", "source_ref", name="source_ref"),
    )


class TaskDep(Base):
    __tablename__ = "task_deps"
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    depends_on_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    __table_args__ = (CheckConstraint("task_id <> depends_on_id", name="no_self_dep"),)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    billing_mode: Mapped[str] = mapped_column(String(8))  # fixed at start, never switched
    model: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="STARTING")
    pid: Mapped[int | None] = mapped_column(Integer)
    worktree_path: Mapped[str | None] = mapped_column(Text)
    session_id: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    exit_code: Mapped[int | None] = mapped_column(Integer)
    result_json: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("task_id", "attempt"),
        CheckConstraint("billing_mode IN ('api','cli','mock')", name="billing_mode"),
    )


class Event(Base):
    """Append-only audit log: every state transition and notable occurrence."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = _ts()
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id", ondelete="SET NULL"))
    task_id: Mapped[int | None] = mapped_column(ForeignKey("tasks.id", ondelete="SET NULL"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="{}")  # JSON, redacted before storing
    __table_args__ = (Index("ix_events_task_ts", "task_id", "ts"),)


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(primary_key=True)
    resource_type: Mapped[str] = mapped_column(String(32))  # path | unity_editor | gpu | ...
    resource_key: Mapped[str] = mapped_column(Text)  # normalized (WP6)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(8), default="excl")
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("mode IN ('excl','shared')", name="mode"),
        Index("ix_reservations_resource", "resource_type", "resource_key"),
    )


class UsageEvent(Base):
    """Append-only cost ledger. `dedup_key` = SDK message id, so one message is counted once."""

    __tablename__ = "usage_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = _ts()
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    dedup_key: Mapped[str] = mapped_column(String(128), unique=True)
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_microusd: Mapped[int] = mapped_column(BigInteger, default=0)
    reconciled: Mapped[bool] = mapped_column(default=False)  # False = client-side estimate
    price_version: Mapped[int | None] = mapped_column(Integer)  # prices.version used (WP8)


class Budget(Base):
    __tablename__ = "budgets"
    id: Mapped[int] = mapped_column(primary_key=True)
    scope: Mapped[str] = mapped_column(String(16))  # global | project | task | day | month
    scope_ref: Mapped[str] = mapped_column(String(64), default="")  # project id, period, ...
    cap_microusd: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (
        UniqueConstraint("scope", "scope_ref"),
        CheckConstraint("cap_microusd >= 0", name="cap_nonneg"),
    )


class Price(Base):
    """Versioned price table, micro-USD per million tokens. A new row = a new version (WP8)."""

    __tablename__ = "prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    model: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer)
    input_mtok: Mapped[int] = mapped_column(BigInteger)
    output_mtok: Mapped[int] = mapped_column(BigInteger)
    cache_read_mtok: Mapped[int] = mapped_column(BigInteger)
    cache_write_mtok: Mapped[int] = mapped_column(BigInteger)  # 5-minute cache writes
    # Optional higher tier once the prompt (input + cache read + cache write) exceeds this size.
    long_threshold: Mapped[int | None] = mapped_column(BigInteger)
    long_input_mtok: Mapped[int | None] = mapped_column(BigInteger)
    long_output_mtok: Mapped[int | None] = mapped_column(BigInteger)
    long_cache_read_mtok: Mapped[int | None] = mapped_column(BigInteger)
    long_cache_write_mtok: Mapped[int | None] = mapped_column(BigInteger)
    source: Mapped[str] = mapped_column(String(200), default="")
    verified_on: Mapped[str] = mapped_column(String(10), default="")
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (
        UniqueConstraint("model", "version"),
        CheckConstraint(
            "input_mtok >= 0 AND output_mtok >= 0 AND cache_read_mtok >= 0"
            " AND cache_write_mtok >= 0",
            name="nonneg",
        ),
    )


class BudgetReservation(Base):
    """Money held for a run between admission and its end (WP8). Released exactly once."""

    __tablename__ = "budget_reservations"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    run_id: Mapped[int | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    amount_microusd: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(8), default="held")
    created_at: Mapped[datetime] = _ts()
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status IN ('held','released')", name="status"),
        CheckConstraint("amount_microusd > 0", name="positive"),
        Index("ix_budget_reservations_status", "status"),
    )


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text)  # never a secret (secrets live in the SecretStore)


class Prompt(Base):
    __tablename__ = "prompts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    version: Mapped[int] = mapped_column(Integer, default=1)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    __table_args__ = (UniqueConstraint("name", "version"),)
