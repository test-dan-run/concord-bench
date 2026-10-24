"""The output cache and the model catalog's output index (platform/catalog/README.md): local rows keyed by
build, index lookups batched per call list, outputs another app holds read from it and copied with their
provenance, Bench's own outputs recorded, and a catalog that is down. aicore's FakeCatalog keeps the index."""

from __future__ import annotations

import httpx
import numpy as np
import pytest
from aicore.catalog import FakeCatalog, index_key

from concord_bench.adapters.base import Adapter
from concord_bench.core.models import ModelOutput
from concord_bench.platform import catalog
from concord_bench.runs import cache
from concord_bench.runs.cache import OutputCache

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
