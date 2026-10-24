"""Offline export is gone (EVALUATE_PLAN.md §9): bench_0005 drops `offline_export` and fails export jobs
that had not finished, and the API and CLI no longer offer it."""

import pytest
from alembic import command
from sqlalchemy import inspect, text

from concord_bench import cli
from concord_bench.core import db


def test_bench_0005_drops_offline_export_and_fails_unfinished_export_jobs(data_dir):
    cfg = db.alembic_config()
    command.downgrade(cfg, "bench_0004")
    eng = db.engine()
    with eng.begin() as c:
        for i, status in enumerate(("queued", "running", "done"), 1):
            c.execute(text("INSERT INTO job (id, type, payload, status, required_services, after, attempts, progress, "
                           "result, created_at) VALUES (:i, 'export', :p, :s, '[]', '[]', 0, 0, '{}', '2026-10-20')"),
                      {"i": i, "p": f'{{"export_id": {i}}}', "s": status})
            c.execute(text("INSERT INTO offline_export (id, kind, services, dest, status, job_id, created_at) "
                           "VALUES (:i, 'full', '[\"silero-vad\"]', '/tmp/kits', :s, :i, '2026-10-20')"),
                      {"i": i, "s": status})
    command.upgrade(cfg, "head")
    assert "offline_export" not in inspect(eng).get_table_names()
    with eng.connect() as c:
        rows = c.execute(text("SELECT status, error FROM job ORDER BY id")).all()
    assert [r[0] for r in rows] == ["failed", "failed", "done"]
    assert "removed from Bench" in rows[0][1] and rows[2][1] is None


def test_the_api_and_cli_offer_no_offline_export(client):
    assert client.get("/api/offline/services").status_code == 404
    with pytest.raises(SystemExit):
        cli.main(["export-offline", "silero-vad"])
