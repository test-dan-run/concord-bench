"""M7 performance check at realistic size (`just test-perf`): a few hundred 5-minute files.

Targets (PLAN §5 M7): table queries under 300 ms; scoring 1,000 utterances under 10 s.
The database is synthetic (no audio): 300 files x 60 speaker-attributed segments = 18,000 utterances.
"""

from __future__ import annotations

import random
import time

import pytest

from concord_bench.core.models import AudioFile, Dataset, Model, ModelOutput, Run, Segment
from concord_bench.runs import scoring

pytestmark = pytest.mark.perf

WORDS = ["the", "account", "number", "is", "not", "working", "since", "yesterday", "please", "could", "you", "check", "my", "order", "again", "thank", "you", "very", "much", "hello", "yes", "no", "okay", "right", "sure", "delivery", "payment", "card", "refund", "engineer", "router", "line"]


def sentence(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n))


def corrupt(rng: random.Random, s: str) -> str:
    out = []
    for w in s.split():
        x = rng.random()
        if x < 0.05:
            continue
        out.append(rng.choice(WORDS) if x < 0.12 else w)
    return " ".join(out)


def build(session, files: int, segs_per_file: int, scored_files: int):
    rng = random.Random(7)
    ds = Dataset(name="perf", status="ready", label_types=["asr", "diar"])
    model = Model(catalog_id="perf-asr", display_name="perf",
                  catalog={"contract": "openai", "api": "audio.transcriptions",
                           "capabilities": {"status": "verified", "tasks": ["asr"], "timing_level": "text"}})
    session.add_all([ds, model])
    session.flush()
    afs = [AudioFile(dataset_id=ds.id, rel_path=f"f{i:04d}.wav", sha256=f"{i:064x}", ext=".wav", duration=300.0,
                     orig_sr=16000, channels=1, status="ok") for i in range(files)]
    session.add_all(afs)
    session.flush()
    segs, outs = [], []
    plan: dict[str, dict] = {}
    for f in afs:
        units = []
        for k in range(segs_per_file):
            s0 = k * 5.0
            text = sentence(rng, 12)
            segs.append(Segment(audio_file_id=f.id, start=s0, end=s0 + 4.5, speaker=f"spk{k % 2}", text=text))
            if len(plan) < scored_files:
                outs.append((f, k, s0, ModelOutput(model_name="perf-asr", audio_sha256=f.sha256, span_start=s0,
                                                   span_end=s0 + 4.5, params_hash="p", latency_s=0.1,
                                                   output={"text": corrupt(rng, text), "words": None,
                                                           "segments": None})))
                units.append(k)
        if units:
            plan[str(f.id)] = {"stem": f.stem, "units": units}
    session.add_all(segs)
    session.add_all([o for *_, o in outs])
    session.flush()
    width = len(str(segs_per_file))
    outputs = {fid: {"units": []} for fid in plan}
    for f, k, s0, o in outs:
        outputs[str(f.id)]["units"].append({"key": f"{f.stem}#{k + 1:0{width}d}", "start": s0, "end": s0 + 4.5,
                                            "cache_id": o.id})
    run = Run(task="asr", model_id=model.id, dataset_id=ds.id, status="done", outputs=outputs,
              rtf_by_stage={"asr": 0.02, "total": 0.02})
    session.add(run)
    session.commit()
    return run


def test_scoring_1000_utterances_under_10s(session):
    run = build(session, files=300, segs_per_file=60, scored_files=17)  # 17 x 60 = 1,020 scored utterances
    for norm, limit in (("basic", 10.0), ("whisper_en", 10.0)):
        t = time.perf_counter()
        sc = scoring.score_run(session, run, norm)
        session.commit()
        took = time.perf_counter() - t
        print(f"\nscoring {sc.aggregates['utterances']} utterances with {norm}: {took:.2f} s")
        assert sc.aggregates["utterances"] >= 1000
        assert took < limit


def test_table_queries_under_300ms(client, session):
    run = build(session, files=300, segs_per_file=60, scored_files=300)  # 18,000 utterances in the table
    sc = scoring.score_run(session, run, "basic")
    session.commit()
    queries = {
        "sort by WER": f"/api/errors/asr/utterances?score_id={sc.id}&sort=wer&order=desc&limit=200",
        "sort by S (json)": f"/api/errors/asr/utterances?score_id={sc.id}&sort=s&order=desc&limit=200",
        "page 50": f"/api/errors/asr/utterances?score_id={sc.id}&sort=wer&order=asc&offset=10000&limit=200",
        "only errors": f"/api/errors/asr/utterances?score_id={sc.id}&only_errors=true&limit=200",
        "text search": f"/api/errors/asr/utterances?score_id={sc.id}&q=refund%20engineer&limit=200",
        "benchmark table": "/api/benchmarks/asr",
    }
    client.get(queries["sort by WER"])  # warm caches
    for name, url in queries.items():
        t = time.perf_counter()
        r = client.get(url)
        took = (time.perf_counter() - t) * 1000
        print(f"\n{name}: {took:.0f} ms ({r.json().get('total', '')})")
        assert r.status_code == 200
        assert took < 300, f"{name} took {took:.0f} ms"
