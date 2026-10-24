"""No offline export

Offline kits are no longer written by Bench (EVALUATE_PLAN.md §9): the platform's model catalog exports
models. The `offline_export` table goes, and export jobs that had not finished are failed, since no
handler runs them any more. Kits already written on disk are left where they are.

Revision ID: bench_0005
Revises: bench_0004
Create Date: 2026-10-24 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bench_0005"
down_revision: str | None = "bench_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE job SET status = 'failed', error = :e WHERE type = 'export' AND status IN "
                         "('queued', 'waiting', 'running', 'blocked')"),
                 {"e": "offline export was removed from Bench; export models from the model catalog"})
    op.drop_table("offline_export")


def downgrade() -> None:
    op.create_table(
        "offline_export",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("services", sa.JSON(), nullable=False),
        sa.Column("concord_image", sa.String(length=300), nullable=True),
        sa.Column("dest", sa.String(length=1000), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("path", sa.String(length=1000), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
