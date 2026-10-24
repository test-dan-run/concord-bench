"""Overlap scoring, hardware and wall time on runs

- `score.skip_overlap`: a score can leave overlapped speech out of DER and JER, so the key becomes
  (run, normaliser, collar, skip_overlap); existing scores scored overlap.
- `run.hardware`: what each model ran on, as the model catalog reported it; `run.wall_s`: seconds from
  start to finish. Existing runs have neither.

Revision ID: bench_0006
Revises: bench_0005
Create Date: 2026-10-24 12:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bench_0006"
down_revision: str | None = "bench_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Names for the unnamed unique constraint bench_0001 created, so batch mode can drop it.
NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade() -> None:
    with op.batch_alter_table("score", naming_convention=NAMING) as b:
        b.add_column(sa.Column("skip_overlap", sa.Boolean(), nullable=False, server_default=sa.false()))
        b.drop_constraint("uq_score_run_id", type_="unique")
        b.create_unique_constraint("uq_score_key", ["run_id", "normaliser", "collar", "skip_overlap"])
    with op.batch_alter_table("run") as b:
        b.add_column(sa.Column("hardware", sa.JSON(), nullable=False, server_default="{}"))
        b.add_column(sa.Column("wall_s", sa.Float(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM score WHERE skip_overlap")
    with op.batch_alter_table("run") as b:
        b.drop_column("wall_s")
        b.drop_column("hardware")
    with op.batch_alter_table("score") as b:
        b.drop_constraint("uq_score_key", type_="unique")
        b.create_unique_constraint("uq_score_run_id", ["run_id", "normaliser", "collar"])
        b.drop_column("skip_overlap")
