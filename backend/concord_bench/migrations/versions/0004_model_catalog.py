"""Models from the model catalog

Registration, verification and starting models moved to the platform's model catalog
(platform/catalog/README.md):

- `model`: `registry_name` becomes `catalog_id` (the same platform service name); the registration
  columns (`capabilities`, `timing_level`, `check_report`, `registry_snapshot`) go, and `catalog` keeps the
  last catalog record seen. Existing rows are seeded from what they had, so old runs keep their labels and
  scores until the catalog's record replaces it.
- `model_output`: the cache key gains the build key (`build_key`); rows from before have none, so they stay
  readable by id but never match a lookup again. `params` keeps the request parameters (served to other
  apps), `source_app` / `source_output_id` where a row was copied from another app.
- `run.image_tags` becomes `run.builds` (model -> {build_key, image}; old entries keep their tag).
- `job`: `after` (job ids that must finish first: stage-by-stage grids) and `lease` (the catalog lease
  while the job waits for compute). Queued compatibility checks are failed: the catalog verifies models.

Revision ID: bench_0004
Revises: bench_0003
Create Date: 2026-10-22 16:00:00
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bench_0004"
down_revision: str | None = "bench_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Names for the unnamed unique constraints bench_0001 created, so batch mode can drop them.
NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _json(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def upgrade() -> None:
    conn = op.get_bind()
    with op.batch_alter_table("model", naming_convention=NAMING) as b:
        b.add_column(sa.Column("catalog_id", sa.String(length=200), nullable=True))
        b.add_column(sa.Column("catalog", sa.JSON(), nullable=False, server_default="{}"))
    rows = conn.execute(sa.text("SELECT id, registry_name, capabilities, timing_level, registry_snapshot "
                                "FROM model")).all()
    for mid, name, caps, timing, snap in rows:
        snap = _json(snap)
        record = {k: snap.get(k) for k in ("contract", "api", "kind", "engine", "image", "description")
                  if snap.get(k) is not None}
        record["capabilities"] = {"status": "unknown", "tasks": _json(caps) if caps else [], "timing_level": timing}
        conn.execute(sa.text("UPDATE model SET catalog_id = :c, catalog = :r WHERE id = :id"),
                     {"c": name, "r": json.dumps(record), "id": mid})
    with op.batch_alter_table("model", naming_convention=NAMING) as b:
        b.drop_constraint("uq_model_registry_name", type_="unique")
        b.drop_column("registry_name")
        b.drop_column("capabilities")
        b.drop_column("timing_level")
        b.drop_column("check_report")
        b.drop_column("registry_snapshot")
        b.alter_column("catalog_id", existing_type=sa.String(length=200), nullable=False)
        b.create_unique_constraint("uq_model_catalog_id", ["catalog_id"])

    with op.batch_alter_table("model_output", naming_convention=NAMING) as b:
        b.drop_constraint("uq_model_output_model_name", type_="unique")
        b.add_column(sa.Column("build_key", sa.String(length=32), nullable=True))
        b.add_column(sa.Column("params", sa.JSON(), nullable=True))
        b.add_column(sa.Column("source_app", sa.String(length=64), nullable=True))
        b.add_column(sa.Column("source_output_id", sa.String(length=64), nullable=True))
        b.create_unique_constraint("uq_model_output_key", ["model_name", "build_key", "audio_sha256", "span_start",
                                                           "span_end", "params_hash"])
        b.create_index("ix_model_output_audio", ["audio_sha256"])

    with op.batch_alter_table("run") as b:
        b.alter_column("image_tags", new_column_name="builds", existing_type=sa.JSON(), existing_nullable=False)

    with op.batch_alter_table("job") as b:
        b.add_column(sa.Column("after", sa.JSON(), nullable=False, server_default="[]"))
        b.add_column(sa.Column("lease", sa.JSON(), nullable=True))
    conn.execute(sa.text("UPDATE job SET status = 'failed', error = :e WHERE type = 'check' AND status IN "
                         "('queued', 'running', 'blocked')"),
                 {"e": "compatibility checks moved to the model catalog; verify the model in the catalog's UI"})


def downgrade() -> None:
    conn = op.get_bind()
    with op.batch_alter_table("job") as b:
        b.drop_column("lease")
        b.drop_column("after")
    with op.batch_alter_table("run") as b:
        b.alter_column("builds", new_column_name="image_tags", existing_type=sa.JSON(), existing_nullable=False)
    # Outputs keyed by build would collide on the old key: keep one row per old key.
    conn.execute(sa.text("DELETE FROM model_output WHERE id NOT IN (SELECT MIN(id) FROM model_output GROUP BY "
                         "model_name, audio_sha256, span_start, span_end, params_hash)"))
    with op.batch_alter_table("model_output") as b:
        b.drop_index("ix_model_output_audio")
        b.drop_constraint("uq_model_output_key", type_="unique")
        b.drop_column("source_output_id")
        b.drop_column("source_app")
        b.drop_column("params")
        b.drop_column("build_key")
        b.create_unique_constraint("uq_model_output_model_name", ["model_name", "audio_sha256", "span_start",
                                                                  "span_end", "params_hash"])
    with op.batch_alter_table("model") as b:
        b.add_column(sa.Column("registry_name", sa.String(length=200), nullable=True))
        b.add_column(sa.Column("capabilities", sa.JSON(), nullable=False, server_default="[]"))
        b.add_column(sa.Column("timing_level", sa.String(length=16), nullable=True))
        b.add_column(sa.Column("check_report", sa.JSON(), nullable=False, server_default="{}"))
        b.add_column(sa.Column("registry_snapshot", sa.JSON(), nullable=False, server_default="{}"))
    for mid, cid, record in conn.execute(sa.text("SELECT id, catalog_id, catalog FROM model")).all():
        record = _json(record)
        caps = record.get("capabilities") or {}
        snap = {k: record.get(k) for k in ("contract", "api", "kind", "engine", "image", "description")}
        conn.execute(sa.text("UPDATE model SET registry_name = :n, capabilities = :c, timing_level = :t, "
                             "registry_snapshot = :s WHERE id = :id"),
                     {"n": cid, "c": json.dumps(caps.get("tasks") or []), "t": caps.get("timing_level"),
                      "s": json.dumps(snap), "id": mid})
    with op.batch_alter_table("model") as b:
        b.drop_constraint("uq_model_catalog_id", type_="unique")
        b.drop_column("catalog")
        b.drop_column("catalog_id")
        b.alter_column("registry_name", existing_type=sa.String(length=200), nullable=False)
        b.create_unique_constraint("uq_model_registry_name", ["registry_name"])
