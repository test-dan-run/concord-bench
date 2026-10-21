"""There is no audio duration cap: long files are ingested as usable, and bench_0003 makes files that
the old 5-minute cap had flagged `too_long` usable again."""

import json
import wave

import numpy as np
from alembic import command
from sqlalchemy import select, text

from concord_bench.core import db
from concord_bench.core.models import AudioFile, Dataset
from concord_bench.datasets.ingest import ingest
from concord_bench.runs.executor import usable_files


def _tone_wav(path, seconds: float, sr: int = 16000) -> None:
    t = np.arange(int(seconds * sr)) / sr
    pcm = (0.1 * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def test_a_file_over_five_minutes_is_usable(session, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _tone_wav(src / "meeting.wav", 310.0)
    (src / "meeting.txt").write_text("a long meeting")
    ds = Dataset(name="long")
    session.add(ds)
    session.flush()
    rep = ingest(session, ds.id, src)
    session.commit()
    f = session.scalar(select(AudioFile))
    assert f.status == "ok" and f.duration > 300
    assert rep["usable_files"] == 1 and set(rep["flagged"]) == {"clipped", "undecodable"}
    assert [x.id for x in usable_files(session, ds.id)] == [f.id]


def test_bench_0003_makes_too_long_files_usable(data_dir):
    cfg = db.alembic_config()
    command.downgrade(cfg, "bench_0002")
    eng = db.engine()
    report = {"files": 2, "usable_files": 1, "usable_hours": 0.01, "max_duration_s": 300.0,
              "flagged": {"clipped": [], "undecodable": [], "too_long": [{"file": "long.wav", "duration": 600.0}]}}
    with eng.begin() as c:
        c.execute(text("INSERT INTO dataset (id, name, tags, label_types, status, report, created_at, provenance) "
                       "VALUES (1, 'old', '[]', '[\"asr\"]', 'ready', :r, '2026-10-01', '{}')"),
                  {"r": json.dumps(report)})
        for i, (status, dur) in enumerate((("ok", 36.0), ("too_long", 600.0)), 1):
            c.execute(text("INSERT INTO audio_file (id, dataset_id, rel_path, sha256, ext, duration, status, meta) "
                           "VALUES (:i, 1, :p, :s, '.wav', :d, :st, '{}')"),
                      {"i": i, "p": f"f{i}.wav", "s": f"{i:064x}", "d": dur, "st": status})
    command.upgrade(cfg, "head")
    with eng.connect() as c:
        assert c.execute(text("SELECT status FROM audio_file ORDER BY id")).scalars().all() == ["ok", "ok"]
        rep = json.loads(c.execute(text("SELECT report FROM dataset")).scalar())
    assert rep["usable_files"] == 2 and abs(rep["usable_hours"] - (0.01 + 600 / 3600)) < 1e-9
    assert "too_long" not in rep["flagged"] and "max_duration_s" not in rep
