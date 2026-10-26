"""WP10: acceptance gate results and integrations (branch push / pull request)

Revision ID: 0005
Revises: 0004
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gate_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=True),
        sa.Column("base_sha", sa.String(64), nullable=True),
        sa.Column("fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("summary_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_gate_results_task_id_tasks"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gate_results")),
    )
    op.create_index("ix_gate_results_task", "gate_results", ["task_id", "id"])
    op.create_table(
        "integrations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("gate_id", sa.Integer(), nullable=False),
        sa.Column("branch", sa.String(200), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="committed"),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_integrations_task_id_tasks"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["gate_id"], ["gate_results.id"],
            name=op.f("fk_integrations_gate_id_gate_results"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integrations")),
        sa.UniqueConstraint("task_id", name=op.f("uq_integrations_task_id")),
    )


def downgrade() -> None:
    op.drop_table("integrations")
    op.drop_index("ix_gate_results_task", table_name="gate_results")
    op.drop_table("gate_results")
