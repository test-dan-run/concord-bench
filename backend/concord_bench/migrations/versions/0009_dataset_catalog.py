"""The Dataset Catalog version a dataset is (DATASET_CATALOG_PLAN.md §9)

- `dataset.catalog_dataset_id`, `dataset.catalog_version`: the catalog dataset and version a Bench dataset
  was imported from or published as. Existing datasets have neither.

Revision ID: bench_0009
Revises: bench_0008
Create Date: 2026-10-24 23:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bench_0009"
down_revision: str | None = "bench_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("dataset") as b:
        b.add_column(sa.Column("catalog_dataset_id", sa.String(length=32), nullable=True))
        b.add_column(sa.Column("catalog_version", sa.Integer(), nullable=True))
        b.create_index("ix_dataset_catalog_dataset_id", ["catalog_dataset_id"])


def downgrade() -> None:
    with op.batch_alter_table("dataset") as b:
        b.drop_index("ix_dataset_catalog_dataset_id")
        b.drop_column("catalog_version")
        b.drop_column("catalog_dataset_id")
