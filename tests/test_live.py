"""Against live platform services (`just test-live`). Starts what it needs through ensure_services."""

from __future__ import annotations

import pytest

from concord_bench.adapters import for_model
from concord_bench.adapters.base import AudioRef
from concord_bench.platform.services import ensure_services, health
from concord_bench.registry.check import check_clip, run_check

pytestmark = pytest.mark.live


@pytest.fixture(scope="module", autouse=True)
def services():
    ensure_services(["silero-vad", "faster-whisper-large-v3"], print)


def test_health_reports_up():
    assert health("silero-vad").up and health("default-vad").up


def test_check_detects_vad_and_word_times():
    assert run_check("silero-vad").capabilities == ["vad"]
    rep = run_check("faster-whisper-large-v3")
    assert rep.capabilities == ["asr"] and rep.timing_level == "word"


def test_adapter_on_check_clip():
    class M:
        registry_name = "faster-whisper-large-v3"
        registry_snapshot = {"contract": "openai", "api": "audio.transcriptions"}
        timing_level = "word"

    audio, truth, sha = check_clip()
    out, latency = for_model(M(), "en").run(audio, AudioRef(sha))
    assert out["words"] and latency > 0
    assert "parcel" in out["text"].lower()
