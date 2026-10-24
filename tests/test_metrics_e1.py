"""EVALUATE_PLAN.md E1: MER / WIL / WIP, tcpWER, overlap scoring, and the hardware a run records."""

from __future__ import annotations

import jiwer
import pytest
from meeteval.io.seglst import SegLST
from meeteval.wer.wer.time_constrained import time_constrained_minimum_permutation_word_error_rate

from concord_bench.metrics import cpwer, der, wer
from concord_bench.metrics.cpwer import SpkSegment
from concord_bench.metrics.der import Turn
from concord_bench.platform import catalog
from concord_bench.runs import jobs
from conftest import catalog_model

PAIRS = [("the cat sat on the mat", "the cat sat on mat"), ("a b c d", "a x c e f"), ("one two", ""),
         ("hello world", "hello there world again"), ("same words here", "same words here")]


@pytest.mark.parametrize("ref,hyp", PAIRS)
def test_mer_wil_wip_match_jiwer_per_utterance(ref, hyp):
    c = wer.score(ref, hyp).counts()
    if hyp:
        out = jiwer.process_words(ref, hyp)
        assert c["mer"] == pytest.approx(out.mer)
        assert c["wil"] == pytest.approx(out.wil)
        assert c["wip"] == pytest.approx(out.wip)
    else:
        assert c["mer"] == 1.0 and c["wip"] == 0.0 and c["wil"] == 1.0
    assert c["p"] == c["h"] + c["s"] + c["i"] == len(hyp.split())


def test_corpus_values_come_from_summed_counts():
    rows = [wer.score(r, h).counts() for r, h in PAIRS]
    agg = wer.aggregate(rows)
    n, s, d, i, h = (sum(r[k] for r in rows) for k in ("n", "s", "d", "i", "h"))
    p = h + s + i
    assert agg["mer"] == pytest.approx((s + d + i) / (h + s + d + i))
    assert agg["wip"] == pytest.approx((h / n) * (h / p))
    assert agg["wil"] == pytest.approx(1 - agg["wip"])
    assert agg["mer"] != pytest.approx(sum(r["mer"] for r in rows) / len(rows))  # not a mean of utterances


def test_older_scores_get_mer_wil_wip_from_their_counts():
    old = {"n": 4, "s": 2, "d": 0, "i": 1, "h": 2, "wer": 0.75}
    filled = wer.with_info(old)
    assert filled["mer"] == pytest.approx(0.6) and filled["wip"] == pytest.approx(0.2) and filled["p"] == 5
    assert wer.with_info({**old, "mer": 0.123})["mer"] == 0.123  # stored values are kept


REF = [SpkSegment(0.0, 2.0, "A", "hello there"), SpkSegment(2.0, 4.0, "B", "how are you"),
       SpkSegment(4.5, 6.0, "A", "fine thanks")]
HYP = [SpkSegment(0.1, 2.1, "x", "hello there"), SpkSegment(8.0, 10.0, "y", "how are you"),
       SpkSegment(4.6, 6.1, "x", "fine thanks")]


@pytest.mark.parametrize("collar", cpwer.TCP_COLLARS)
def test_tcpwer_matches_meeteval(collar):
    def seglst(segs):
        return SegLST([{"session_id": "f", "speaker": s.speaker, "start_time": s.start, "end_time": s.end,
                        "words": s.text} for s in segs])

    res = time_constrained_minimum_permutation_word_error_rate(seglst(REF), seglst(HYP), collar=collar)
    got = cpwer.tcpwer(REF, HYP, collar)
    assert (got["errors"], got["n"]) == (res.errors, res.length)
    assert got["tcpwer"] == pytest.approx(res.error_rate)


def test_tcpwer_adds_timing_errors_cpwer_ignores():
    cp = cpwer.cpwer(REF, HYP)
    assert cp["errors"] == 0  # right words, right speakers
    assert cpwer.tcpwer(REF, HYP, 2.0)["errors"] > 0  # B's words came 6 s late
    assert cpwer.tcpwer(REF, HYP, 10.0)["errors"] == 0
    assert cpwer.tcpwer([], [], 5.0)["tcpwer"] is None


def test_skip_overlap_leaves_overlapped_speech_out_of_der():
    ref = [Turn(0.0, 5.0, "A"), Turn(3.0, 8.0, "B")]  # 2 s of overlap
    hyp = [Turn(0.0, 5.0, "x"), Turn(5.0, 8.0, "y")]  # right outside the overlap, misses B inside it
    scored = der.der(ref, hyp, 8.0, collar=0.0)
    skipped = der.der(ref, hyp, 8.0, collar=0.0, skip_overlap=True)
    assert scored["total"] == pytest.approx(10.0) and skipped["total"] == pytest.approx(6.0)
    assert scored["der"] == pytest.approx(0.2) and skipped["der"] == pytest.approx(0.0)


# ---------------------------------------------------------------- hardware

def test_hardware_of_a_gpu_model_with_and_without_the_platform_fields():
    rec = catalog_model("whisper", ["asr"], gpu={"fraction": 0.15, "gb": 4.8}, engine="vllm")
    assert catalog.hardware(rec, {}) == {"device": "gpu", "vram_gb": 4.8, "engine": "vllm",
                                         "image": "ghcr.io/owner/whisper:0.1.0"}
    p1 = {"gpu_total_gb": 32, "host": {"gpu_name": "NVIDIA GeForce RTX 5090", "gpu_count": 1, "driver_version": "575.1",
                                      "cuda_version": "12.9", "cpu_model": "AMD Ryzen 9", "cpu_cores": 16,
                                      "hostname": "gpu-01"}}
    rec_p1 = {**rec, "runtime": {"precision": "fp16", "batch_size": 16}}
    hw = catalog.hardware(rec_p1, p1)
    assert hw["gpu_name"] == "NVIDIA GeForce RTX 5090" and hw["gpu_total_gb"] == 32 and hw["cpu_cores"] == 16
    assert hw["precision"] == "fp16" and hw["batch_size"] == 16 and hw["hostname"] == "gpu-01"


def test_hardware_of_cpu_and_hosted_models():
    p1 = {"gpu_total_gb": 32, "host": {"gpu_name": "RTX", "cpu_model": "AMD Ryzen 9", "cpu_cores": 16}}
    cpu = catalog.hardware(catalog_model("silero", ["vad"], gpu=None), p1)
    assert cpu["device"] == "cpu" and "gpu_name" not in cpu and "gpu_total_gb" not in cpu
    assert cpu["cpu_model"] == "AMD Ryzen 9"
    hosted = catalog.hardware(catalog_model("cloud-asr", ["asr"], gpu=None, image=None,
                                            hosted={"host": "api.example.com", "base_url": "https://api.example.com/v1"}),
                              p1)
    assert hosted == {"device": "hosted", "hosted_at": "api.example.com"}
    gen = catalog.hardware(catalog_model("sgm", ["enhance"], gpu=None, generative=True), {})
    assert gen["generative"] is True
    assert catalog.hardware(None) == {}


def test_a_build_carries_the_hardware_and_the_platform_is_read_once(fake_catalog):
    fake_catalog.add_model(catalog_model("whisper", ["asr"], gpu={"fraction": 0.2, "gb": 6.0}))
    calls = []
    fake_catalog.platform = lambda: calls.append(1) or {"gpu_total_gb": 32}
    b = jobs._current_build("whisper")
    assert b["hardware"] == {"device": "gpu", "vram_gb": 6.0, "image": "ghcr.io/owner/whisper:0.1.0",
                             "gpu_total_gb": 32}
    jobs._current_build("whisper")
    assert len(calls) == 1  # cached for PLATFORM_TTL_S


def test_peak_vram_comes_from_the_live_lease(fake_catalog):
    fake_catalog.add_model(catalog_model("whisper", ["asr"]))
    lease = catalog.request_lease("bench:job-1", ["whisper"])
    assert catalog.peak_vram(lease) == {}
    fake_catalog._leases[lease.id]["peak_vram_gb"] = {"whisper": 9.8}
    assert catalog.peak_vram(lease) == {"whisper": 9.8}
    lease.release()
