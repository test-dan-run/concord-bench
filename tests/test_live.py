"""Against the live model catalog and platform services (`just test-live`): the catalog (AICORE_CATALOG or
CATALOG_URL) must be running; the lease starts what the tests need."""

from __future__ import annotations

import pytest

from concord_bench.adapters import for_model
from concord_bench.adapters.base import AudioRef
from concord_bench.core.models import Model
from concord_bench.platform import catalog
from concord_bench.runs.jobs import holder_for
from conftest import GEN, dialogue_files

pytestmark = pytest.mark.live
MODELS = ["silero-vad", "faster-whisper-large-v3"]


@pytest.fixture(scope="module", autouse=True)
def lease():
    with catalog.get().lease(holder_for(0), MODELS, on_wait=lambda d: print("waiting:", d)) as lease:
        yield lease


def test_catalog_lists_verified_models():
    for name in MODELS:
        assert catalog.unusable_reason(catalog.record(name, refresh=True), name) is None
    assert catalog.record("silero-vad")["capabilities"]["tasks"] == ["vad"]
    assert catalog.record("faster-whisper-large-v3")["capabilities"]["timing_level"] == "word"


def test_adapter_through_the_gateway():
    from concord_bench.datasets.audio import decode, probe, to_mono_16k

    rec = catalog.record("faster-whisper-large-v3", refresh=True)
    m = Model(catalog_id=rec["id"], display_name=rec["id"], catalog=catalog.snapshot(rec))
    path = GEN / "dataset" / "audio" / next(iter(dialogue_files().values()))["file"]
    p = probe(path)
    audio = to_mono_16k(decode(path, p.sample_rate, p.channels), p.sample_rate)
    out, latency = for_model(m, "en").run(audio, AudioRef("live-test"))
    assert out["words"] and latency > 0
