"""No audio duration cap

Files over the old 5-minute cap were stored with status `too_long` and kept out of runs. There is no
cap any more: such files become `ok`, and each affected dataset's report counts them as usable and
drops the `too_long` list and `max_duration_s`.

Revision ID: bench_0003
Revises: bench_0002
Create Date: 2026-10-22 10:00:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bench_0003"
down_revision: str | None = "bench_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    long = conn.execute(sa.text("SELECT dataset_id, COUNT(*), COALESCE(SUM(duration), 0) FROM audio_file "
                                "WHERE status = 'too_long' GROUP BY dataset_id")).all()
    conn.execute(sa.text("UPDATE audio_file SET status = 'ok' WHERE status = 'too_long'"))
    for dataset_id, n, seconds in long:
        raw = conn.execute(sa.text("SELECT report FROM dataset WHERE id = :id"), {"id": dataset_id}).scalar()
        report = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        report["usable_files"] = (report.get("usable_files") or 0) + n
        report["usable_hours"] = (report.get("usable_hours") or 0.0) + seconds / 3600
        (report.get("flagged") or {}).pop("too_long", None)
        report.pop("max_duration_s", None)
        conn.execute(sa.text("UPDATE dataset SET report = :r WHERE id = :id"),
                     {"r": json.dumps(report), "id": dataset_id})


def downgrade() -> None:
    pass  # there is no cap to restore; the files stay usable
