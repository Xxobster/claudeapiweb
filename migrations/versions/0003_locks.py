"""WP6: reservation project/label columns, one row per (task, resource), desktop claims

Revision ID: 0003
Revises: 0002
"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("reservations", schema=None) as b:
        b.add_column(sa.Column("project_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("label", sa.Text(), nullable=False, server_default=""))
        b.create_foreign_key(
            op.f("fk_reservations_project_id_projects"),
            "projects", ["project_id"], ["id"], ondelete="CASCADE",
        )
        b.create_unique_constraint(
            op.f("uq_reservations_task_id"), ["task_id", "resource_type", "resource_key"]
        )
    op.create_table(
        "desktop_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("path_key", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("note", sa.String(300), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"],
            name=op.f("fk_desktop_claims_project_id_projects"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_desktop_claims")),
    )
    op.create_index(
        "ix_desktop_claims_project", "desktop_claims", ["project_id", "released_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_desktop_claims_project", table_name="desktop_claims")
    op.drop_table("desktop_claims")
    with op.batch_alter_table("reservations", schema=None) as b:
        b.drop_constraint(op.f("uq_reservations_task_id"), type_="unique")
        b.drop_constraint(op.f("fk_reservations_project_id_projects"), type_="foreignkey")
        b.drop_column("label")
        b.drop_column("project_id")
