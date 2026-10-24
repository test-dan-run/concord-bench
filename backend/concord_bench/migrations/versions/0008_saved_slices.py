"""Saved slices (EVALUATE_PLAN.md §7.4): label filters on one dataset, scored as Benchmarks columns.

Revision ID: bench_0008
Revises: bench_0007
Create Date: 2026-10-24 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bench_0008"
down_revision: str | None = "bench_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "slice",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("filters", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["dataset_id"], ["dataset.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("slice")
