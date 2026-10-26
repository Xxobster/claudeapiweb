"""WP11: job board claim / release pull requests

Revision ID: 0006
Revises: 0005
"""
import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_board_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("board_path", sa.Text(), nullable=False),
        sa.Column("branch", sa.String(200), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="committed"),
        sa.Column("pr_number", sa.Integer(), nullable=True),
        sa.Column("pr_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"], ["tasks.id"],
            name=op.f("fk_job_board_claims_task_id_tasks"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_board_claims")),
        sa.UniqueConstraint("task_id", "action", name=op.f("uq_job_board_claims_task_id")),
    )


def downgrade() -> None:
    op.drop_table("job_board_claims")
