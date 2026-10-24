"""Speech enhancement (EVALUATE_PLAN.md §5)

- `enhanced_copy`: a cleaned copy of a dataset's audio made by one enhancement model's build, and
  `enhanced_file`: its files, each stored as derived audio under its own sha256.
- `run.enh_model_id`, `run.enh_copy_id`, `run.enh_placement`: a run that read an enhanced copy (before or
  after diarisation, for pipelines). Existing runs read the original audio.

Revision ID: bench_0007
Revises: bench_0006
Create Date: 2026-10-24 14:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bench_0007"
down_revision: str | None = "bench_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "enhanced_copy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.Integer(), nullable=False),
        sa.Column("build_key", sa.String(length=32), nullable=True),
        sa.Column("params_hash", sa.String(length=16), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("files", sa.Integer(), nullable=True),
        sa.Column("length_check", sa.String(length=16), nullable=True),
        sa.Column("max_shift_ms", sa.Float(), nullable=True),
        sa.Column("audio_s", sa.Float(), nullable=True),
        sa.Column("processing_s", sa.Float(), nullable=True),
        sa.Column("hardware", sa.JSON(), nullable=False),
        sa.Column("image", sa.String(length=300), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["dataset.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["model.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_enhanced_copy_dataset", "enhanced_copy", ["dataset_id", "model_id"])
    op.create_table(
        "enhanced_file",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("copy_id", sa.Integer(), nullable=False),
        sa.Column("audio_file_id", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("duration", sa.Float(), nullable=False),
        sa.Column("shift_ms", sa.Float(), nullable=False),
        sa.Column("latency_s", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["copy_id"], ["enhanced_copy.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["audio_file_id"], ["audio_file.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("copy_id", "audio_file_id", name="uq_enhanced_file"),
    )
    with op.batch_alter_table("run") as b:
        b.add_column(sa.Column("enh_model_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("enh_copy_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("enh_placement", sa.String(length=8), nullable=True))
        b.create_foreign_key("fk_run_enh_model", "model", ["enh_model_id"], ["id"], ondelete="CASCADE")
        b.create_foreign_key("fk_run_enh_copy", "enhanced_copy", ["enh_copy_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.execute("DELETE FROM run WHERE enh_model_id IS NOT NULL")
    with op.batch_alter_table("run") as b:
        b.drop_constraint("fk_run_enh_copy", type_="foreignkey")
        b.drop_constraint("fk_run_enh_model", type_="foreignkey")
        b.drop_column("enh_placement")
        b.drop_column("enh_copy_id")
        b.drop_column("enh_model_id")
    op.drop_table("enhanced_file")
    op.drop_index("ix_enhanced_copy_dataset", table_name="enhanced_copy")
    op.drop_table("enhanced_copy")
