"""Reference package imports and dataset provenance

`reference_import` holds received reference packages (staged, accepted, rejected); a dataset made from
one keeps where its references came from and links to its earlier versions (SPLIT_PLAN.md §4.3).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-22 09:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("dataset") as b:
        b.add_column(sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"))
        b.add_column(sa.Column("source_key", sa.String(length=300), nullable=True))
        b.add_column(sa.Column("export_version", sa.Integer(), nullable=True))
        b.add_column(sa.Column("previous_version_id", sa.Integer(), nullable=True))
        b.create_index("ix_dataset_source_key", ["source_key"])
        b.create_foreign_key("fk_dataset_previous_version", "dataset", ["previous_version_id"], ["id"],
                             ondelete="SET NULL")
    op.create_table(
        "reference_import",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("upload_id", sa.String(length=64), nullable=True),
        sa.Column("path", sa.String(length=1000), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=True),
        sa.Column("preview", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("dataset_id", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["dataset.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("reference_import")
    with op.batch_alter_table("dataset") as b:
        b.drop_constraint("fk_dataset_previous_version", type_="foreignkey")
        b.drop_index("ix_dataset_source_key")
        b.drop_column("previous_version_id")
        b.drop_column("export_version")
        b.drop_column("source_key")
        b.drop_column("provenance")
