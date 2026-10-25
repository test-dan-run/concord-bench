"""The output cache and the Model Catalog's output index (platform/catalog/README.md): local rows keyed by
build, index lookups batched per call list, outputs another app holds read from it and copied with their
provenance, Bench's own outputs recorded, and a catalog that is down. aicore's FakeCatalog keeps the index."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from aicore.catalog import FakeCatalog, index_key

from concord_bench.adapters.base import Adapter
from concord_bench.core.models import ModelOutput
from concord_bench.core.settings import reset_settings
from concord_bench.platform import catalog
from concord_bench.runs import cache, executor
from concord_bench.runs.cache import OutputCache
from concord_bench.runs.references import FileRefs

SHA = "cd" * 32
BUILD = "f00dfeedf00dfeed"
AUDIO = np.zeros(16000, dtype=np.float32)


class Seg(Adapter):
    kind, contract, api = "seg", "oip-v2", "infer"

    def __init__(self, segments=((0.5, 1.0),)):
        super().__init__("pyannote-diarization", {})
        self.calls = 0
        self.segments = [list(s) for s in segments]

    def call(self, audio):
        self.calls += 1
        return {"SEGMENTS": self.segments}

    def parse(self, raw, duration):
        return {"segments": raw["SEGMENTS"], "speakers": None}


@pytest.fixture
def cat(data_dir):
    fake = FakeCatalog()
    fake.register_app("label", "http://label.test:8620")
    catalog.use(fake)
    return fake


def label_has(cat: FakeCatalog, adapter: Adapter, output_id: int = 77, start=-1, end=-1) -> dict:
    cat.index_record("label", [{**index_key(BUILD, SHA, start, end, adapter.params_hash), "output_id": output_id}])
    return {"id": output_id, "build_key": BUILD, "audio_sha256": SHA, "span_start": start, "span_end": end,
            "params_hash": adapter.params_hash, "params": adapter.params,
            "output": {"segments": [[1.0, 2.0]], "speakers": ["spk0"]}, "latency_s": 0.7}


def test_computed_outputs_are_stored_with_their_build_and_indexed(session, cat):
    a = Seg()
    row, hit = cache.get_or_run(session, a, AUDIO, SHA, build_key=BUILD, image="img:1")
    session.commit()
    assert not hit and a.calls == 1
    assert (row.build_key, row.span_start, row.span_end, row.params, row.image) == (BUILD, -1.0, -1.0,
                                                                                     {"adapter": "Seg"}, "img:1")
    hit = cat.index_lookup("label", [index_key(BUILD, SHA, -1, -1, a.params_hash)])[0]
    assert hit["owner"] == "bench" and hit["output_id"] == row.id
    again, hit = cache.get_or_run(session, a, AUDIO, SHA, build_key=BUILD)
    assert hit and again.id == row.id and a.calls == 1
    # another build is another output
    _, hit = cache.get_or_run(session, a, AUDIO, SHA, build_key="0" * 16)
    assert not hit and a.calls == 2


def test_an_output_label_holds_is_copied_not_computed(session, cat, monkeypatch):
    a = Seg()
    served = label_has(cat, a)
    asked = []

    def read(url, ids):
        asked.append((url, ids))
        return [served]

    monkeypatch.setattr(catalog, "read_outputs", read)
    row, hit = cache.get_or_run(session, a, AUDIO, SHA, build_key=BUILD)
    session.commit()
    assert hit and a.calls == 0 and asked == [("http://label.test:8620", [77])]
    assert (row.source_app, row.source_output_id, row.output, row.latency_s) == ("label", "77", served["output"], 0.7)
    # Bench's copy is in the index too, and Bench prefers its own entry from now on
    assert cat.index_lookup("bench", [index_key(BUILD, SHA, -1, -1, a.params_hash)])[0]["owner"] == "bench"


def test_one_index_lookup_for_a_whole_work_list(session, cat, monkeypatch):
    a = Seg()
    spans = [(float(i), float(i) + 1.0) for i in range(30)]
    served = [label_has(cat, a, 100 + i, s, e) for i, (s, e) in enumerate(spans[:10])]
    monkeypatch.setattr(catalog, "read_outputs", lambda url, ids: [x for x in served if x["id"] in ids])
    oc = OutputCache(session)
    n_lookups = len([c for c in cat.calls if c[0] == "index_lookup"])
    counts = oc.prefetch(a, BUILD, [cache.key(SHA, s, e) for s, e in spans])
    assert counts == {"local": 0, "copied": 10, "missing": 20}
    assert len([c for c in cat.calls if c[0] == "index_lookup"]) == n_lookups + 1
    for s, e in spans:
        oc.run(a, AUDIO, SHA, s, e, BUILD)
    session.commit()
    assert oc.flush() == 30 and a.calls == 20  # copies and computed rows, recorded in one call


@pytest.mark.parametrize("change", [{"params_hash": "0" * 16}, {"build_key": "1" * 16}, {"output": {"text": "x"}}])
def test_a_copy_that_does_not_match_the_key_is_computed_instead(session, cat, monkeypatch, change):
    a = Seg()
    served = {**label_has(cat, a), **change}
    monkeypatch.setattr(catalog, "read_outputs", lambda url, ids: [served])
    row, hit = cache.get_or_run(session, a, AUDIO, SHA, build_key=BUILD)
    assert not hit and a.calls == 1 and row.source_app is None


def test_owner_down_or_catalog_down_computes_here(session, cat, monkeypatch):
    a = Seg()
    label_has(cat, a)

    def refused(url, ids):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(catalog, "read_outputs", refused)
    logs: list[str] = []
    oc = OutputCache(session, logs.append)
    oc.prefetch(a, BUILD, [cache.key(SHA, -1, -1)])
    assert any("could not read outputs from label" in m for m in logs)

    class Down(FakeCatalog):
        def index_lookup(self, app, keys):
            raise httpx.ConnectError("refused")

        def index_record(self, app, entries):
            raise httpx.ConnectError("refused")

    catalog.use(Down())
    logs.clear()
    oc = OutputCache(session, logs.append)
    oc.prefetch(a, BUILD, [cache.key(SHA, 2.0, 3.0)])
    row, hit = oc.run(a, AUDIO, SHA, 2.0, 3.0, BUILD)
    session.commit()
    assert not hit and oc.flush() == 0 and row.build_key == BUILD
    assert len([m for m in logs if "output index unavailable" in m]) == 1


def test_no_build_key_never_matches_and_is_never_indexed(session, cat):
    a = Seg()
    session.add(ModelOutput(model_name=a.service, build_key=None, audio_sha256=SHA, span_start=0.0, span_end=-1.0,
                            params_hash=a.params_hash, output={"segments": []}, latency_s=0.1))  # a pre-catalog row
    session.commit()
    oc = OutputCache(session)
    assert oc.prefetch(a, None, [cache.key(SHA, -1, -1)]) == {"local": 0, "copied": 0, "missing": 1}
    oc.run(a, AUDIO, SHA, -1, -1, None)
    session.commit()
    assert oc.flush() == 0 and not [c for c in cat.calls if c[0] in ("index_lookup", "index_record")]
    assert a.calls == 1


def test_bench_own_stale_entries_are_computed_again(session, cat, monkeypatch):
    a = Seg()
    cat.index_record("bench", [{**index_key(BUILD, SHA, -1, -1, a.params_hash), "output_id": 999}])
    monkeypatch.setattr(catalog, "read_outputs", lambda url, ids: pytest.fail("must not read from Bench itself"))
    row, hit = cache.get_or_run(session, a, AUDIO, SHA, build_key=BUILD)
    assert not hit and a.calls == 1


class SlowASR(Adapter):
    """Sleeps per call and counts how many calls overlap."""

    kind, contract, api = "asr", "openai", "audio.transcriptions"

    def __init__(self, delay=0.2, fail_at=None):
        super().__init__("qwen3-asr-1-7b", {})
        self.delay, self.fail_at = delay, fail_at
        self.lock = threading.Lock()
        self.calls = self.now = self.peak = 0

    def call(self, audio):
        with self.lock:
            self.calls += 1
            self.now += 1
            self.peak = max(self.peak, self.now)
            n = self.calls
        time.sleep(self.delay)
        with self.lock:
            self.now -= 1
        if n == self.fail_at:
            raise ValueError("bad request")
        return {"text": f"call {n}"}

    def parse(self, raw, duration):
        return {"text": raw["text"], "words": None, "segments": None}


def _work(n_files: int, spans=((0.0, 0.5), (0.5, 1.0))):
    refs = [FileRefs(SimpleNamespace(id=i, rel_path=f"f{i}.wav", sha256=f"{i:02x}" * 32, duration=1.0))
            for i in range(n_files)]
    return [(r, [cache.key(r.sha256, s, e) for s, e in spans]) for r in refs]


def test_infer_overlaps_calls_and_latency_sums_to_wall_time(session, cat, monkeypatch):
    monkeypatch.setattr(executor, "_load_audio", lambda r: AUDIO)
    a = SlowASR()
    work = _work(3)
    work.append((FileRefs(SimpleNamespace(id=9, rel_path="copy.wav", sha256=work[0][0].sha256, duration=1.0)),
                 work[0][1]))  # same audio as the first file: computed once
    t0 = time.perf_counter()
    rows, st = executor.infer(session, OutputCache(session), a, {"build_key": BUILD}, work, lambda f, m: None, "asr",
                              concurrency=4)
    wall = time.perf_counter() - t0
    assert (a.calls, a.peak) == (6, 4)
    assert (st["calls"], st["computed"], st["hits"]) == (8, 6, 2)
    total = sum(r.latency_s for r in rows.values())
    assert total <= wall and total > 0.8 * wall and total < 6 * a.delay * 0.75  # wall time, not summed requests
    assert session.query(ModelOutput).count() == 6


def test_infer_keeps_finished_rows_when_a_call_fails(session, cat, monkeypatch):
    monkeypatch.setattr(executor, "_load_audio", lambda r: AUDIO)
    a = SlowASR(delay=0.0, fail_at=3)
    with pytest.raises(ValueError, match="bad request"):
        executor.infer(session, OutputCache(session), a, {"build_key": BUILD}, _work(2), lambda f, m: None, "asr")
    session.rollback()
    assert session.query(ModelOutput).count() == 2
    rows, st = executor.infer(session, OutputCache(session), a, {"build_key": BUILD}, _work(2), lambda f, m: None, "asr")
    assert (st["computed"], st["hits"], a.calls) == (2, 2, 5)


def test_concurrency_by_model_then_engine_then_default(data_dir, monkeypatch):
    def model(cid, engine):
        return SimpleNamespace(catalog_id=cid, catalog={"engine": engine})

    assert executor.concurrency_for(model("qwen3-asr-1-7b", "vllm")) == executor.CONCURRENCY["vllm"]
    assert executor.concurrency_for(model("meralion-3-3b-asr", "custom")) == executor.CONCURRENCY["default"]
    monkeypatch.setenv("INFER_CONCURRENCY", '{"meralion-3-3b-asr": 1, "vllm": 16}')
    reset_settings()
    assert executor.concurrency_for(model("meralion-3-3b-asr", "custom")) == 1
    assert executor.concurrency_for(model("qwen3-asr-1-7b", "vllm")) == 16
    assert executor.concurrency_for(model("ark-asr-0-6b", "custom")) == executor.CONCURRENCY["default"]


def test_infer_stopped_from_progress_keeps_what_finished(session, cat, monkeypatch):
    monkeypatch.setattr(executor, "_load_audio", lambda r: AUDIO)
    a = SlowASR(delay=0.05)

    def progress(f, m):
        raise RuntimeError("stop")  # what JobContext.progress raises when the job is asked to stop

    with pytest.raises(RuntimeError, match="stop"):
        executor.infer(session, OutputCache(session), a, {"build_key": BUILD}, _work(5), progress, "asr", concurrency=2)
    session.rollback()
    stored = session.query(ModelOutput).count()
    assert 1 <= stored <= a.calls <= 2  # the first batch is kept; no call starts after the stop
