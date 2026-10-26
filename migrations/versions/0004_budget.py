"""WP8: versioned price table, budget reservations, price_version on the usage ledger

Revision ID: 0004
Revises: 0003
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_SRC, _VER = "platform.claude.com pricing + SDK_NOTES s5", "2026-10-26"
# model, input, output, cache_read, cache_write_5m (micro-USD per million tokens); literal copy
# of cpm.budget.seed so later edits of the seed never change what this revision did.
_SEED = [
    ("claude-fable-5-1", 10_000_000, 50_000_000, 250_000, 12_500_000, None),
    ("claude-opus-5-5", 4_000_000, 20_000_000, 200_000, 5_000_000, None),
    ("claude-sonnet-5-5", 2_000_000, 10_000_000, 200_000, 2_500_000, None),
    ("claude-haiku-5-5", 100_000, 500_000, 10_000, 125_000,
     (100_000, 500_000, 2_500_000, 50_000, 625_000)),
]  # fmt: skip


def upgrade() -> None:
    prices = op.create_table(
        "prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("input_mtok", sa.BigInteger(), nullable=False),
        sa.Column("output_mtok", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_mtok", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_mtok", sa.BigInteger(), nullable=False),
        sa.Column("long_threshold", sa.BigInteger(), nullable=True),
        sa.Column("long_input_mtok", sa.BigInteger(), nullable=True),
        sa.Column("long_output_mtok", sa.BigInteger(), nullable=True),
        sa.Column("long_cache_read_mtok", sa.BigInteger(), nullable=True),
        sa.Column("long_cache_write_mtok", sa.BigInteger(), nullable=True),
        sa.Column("source", sa.String(200), nullable=False, server_default=""),
        sa.Column("verified_on", sa.String(10), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.UniqueConstraint("model", "version", name=op.f("uq_prices_model")),
        sa.CheckConstraint(
            "input_mtok >= 0 AND output_mtok >= 0 AND cache_read_mtok >= 0"
            " AND cache_write_mtok >= 0",
            name=op.f("ck_prices_nonneg"),
        ),
    )  # fmt: skip
    rows = []
    for model, i, o, cr, cw, lg in _SEED:
        rows.append(
            {
                "model": model, "version": 1, "input_mtok": i, "output_mtok": o,
                "cache_read_mtok": cr, "cache_write_mtok": cw,
                "long_threshold": lg[0] if lg else None,
                "long_input_mtok": lg[1] if lg else None,
                "long_output_mtok": lg[2] if lg else None,
                "long_cache_read_mtok": lg[3] if lg else None,
                "long_cache_write_mtok": lg[4] if lg else None,
                "source": _SRC, "verified_on": _VER,
            }
        )  # fmt: skip
    op.bulk_insert(prices, rows)

    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("tasks.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("projects.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("amount_microusd", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False, server_default="held"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.current_timestamp()),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('held','released')",
                           name=op.f("ck_budget_reservations_status")),
        sa.CheckConstraint("amount_microusd > 0", name=op.f("ck_budget_reservations_positive")),
    )  # fmt: skip
    op.create_index("ix_budget_reservations_status", "budget_reservations", ["status"])
    with op.batch_alter_table("usage_events", schema=None) as b:
        b.add_column(sa.Column("price_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("usage_events", schema=None) as b:
        b.drop_column("price_version")
    op.drop_index("ix_budget_reservations_status", table_name="budget_reservations")
    op.drop_table("budget_reservations")
    op.drop_table("prices")
