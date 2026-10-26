"""WP4: task hierarchy, retry/source/timestamps columns, BACKLOG as the initial status

Revision ID: 0002
Revises: 0001
"""
import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as b:
        b.add_column(sa.Column("parent_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"))
        b.add_column(sa.Column("source", sa.String(16), nullable=False, server_default="manual"))
        b.add_column(sa.Column("source_ref", sa.String(64), nullable=True))
        b.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        b.create_foreign_key(
            op.f("fk_tasks_parent_id_tasks"), "tasks", ["parent_id"], ["id"], ondelete="SET NULL"
        )
        b.create_unique_constraint(op.f("uq_tasks_project_id"), ["project_id", "source_ref"])
    op.execute("UPDATE tasks SET status = 'BACKLOG' WHERE status = 'DRAFT'")


def downgrade() -> None:
    with op.batch_alter_table("tasks", schema=None) as b:
        b.drop_constraint(op.f("uq_tasks_project_id"), type_="unique")
        b.drop_constraint(op.f("fk_tasks_parent_id_tasks"), type_="foreignkey")
        for c in ("completed_at", "started_at", "source_ref", "source", "retry_count", "parent_id"):
            b.drop_column(c)
