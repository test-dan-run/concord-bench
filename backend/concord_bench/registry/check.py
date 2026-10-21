"""Compatibility check on the synthesised two-speaker check clip (PLAN §5 M3).

| Registry entry | Probe | Result |
|---|---|---|
| openai / audio.transcriptions | verbose_json + timestamp_granularities[]=word | words -> word times; segments -> segment times; text -> text only; garbage -> warn |
| openai / chat.completions | input_audio part + "transcribe" | plausible -> ASR text only; 4xx or unrelated text -> fail |
| oip-v2 / infer (AUDIO + SAMPLE_RATE) | infer | SEGMENTS -> VAD; + SPEAKERS -> diarisation (2 speakers -> pass, else warn); out of bounds -> fail |
| anything else | none | not usable as a model |

Every line is pass / warn / fail with a plain-language reason. "Add" is blocked only when no
capability was detected. A KWS row joins this table when KWS lands (Q6).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import numpy as np

from ..adapters.base import AudioRef, adapter_class
from ..core.settings import REPO_ROOT
from ..datasets.audio import load_derived, sha256_file
from ..metrics import der as der_mod
from ..metrics import normalisers, wer
from . import reader

CHECK_DIR = REPO_ROOT / "fixtures" / "generated" / "check"
WER_WARN = 0.5  # transcripts this far from the clip's text are flagged
WER_FAIL_CHAT = 0.8  # chat answers this far away mean the audio part was ignored


@dataclass
class CheckLine:
    name: str
    status: str  # pass | warn | fail | info
    reason: str


@dataclass
class CheckReport:
    service: str
    usable_as: str | None
    capabilities: list[str] = field(default_factory=list)
    timing_level: str | None = None
    lines: list[CheckLine] = field(default_factory=list)
    sample: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.capabilities)

    def add(self, name: str, status: str, reason: str) -> None:
        self.lines.append(CheckLine(name, status, reason))

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["ok"] = self.ok
        return d


@cache
def check_clip() -> tuple[np.ndarray, dict[str, Any], str]:
    wav = CHECK_DIR / "check_clip.wav"
    truth = json.loads((CHECK_DIR / "check_clip.json").read_text())
    return load_derived(wav), truth, sha256_file(wav)


def _wer_vs_truth(text: str, truth: dict[str, Any]) -> float:
    n = normalisers.get("basic")
    r = wer.score(n(truth["transcript"]), n(text))
    return r.wer if r.wer is not None else 1.0


def _status_code(exc: Exception) -> int | None:
    return getattr(exc, "status_code", None) or getattr(exc, "status", None)


def _short(exc: Exception) -> str:
    msg = getattr(exc, "message", None) or str(exc)
    return msg if len(msg) < 240 else msg[:240] + "..."


def check_asr(rep: CheckReport, audio: np.ndarray, truth: dict[str, Any], sha: str) -> None:
    cls = adapter_class("openai", "audio.transcriptions")
    out = None
    for timing in ("word", "segment", "text"):
        a = cls(rep.service, {"timing": timing})
        try:
            out, latency = a.run(audio, AudioRef(sha))
            rep.add("request", "pass", f"{'verbose_json + word timestamps' if timing == 'word' else timing} "
                    f"request accepted ({latency:.2f} s)")
            break
        except Exception as exc:  # noqa: BLE001
            code = _status_code(exc)
            if code and 400 <= code < 500 and timing != "text":
                rep.add("request", "info", f"{timing} timing request rejected ({code}): {_short(exc)}; trying a "
                        "simpler request")
                continue
            rep.add("request", "fail", f"transcription request failed: {_short(exc)}")
            return
    if out is None:
        rep.add("request", "fail", "no transcription request was accepted")
        return
    if out["words"]:
        rep.timing_level = "word"
        rep.add("timing", "pass", f"word times returned ({len(out['words'])} words)")
    elif out["segments"]:
        rep.timing_level = "segment"
        rep.add("timing", "pass", f"segment times only ({len(out['segments'])} segments); error regions are shown "
                "per utterance")
    else:
        rep.timing_level = "text"
        rep.add("timing", "warn", "text only, no timestamps; per-word audio and error regions are unavailable")
    _judge_transcript(rep, out["text"], truth, warn_only=True)


def _judge_transcript(rep: CheckReport, text: str, truth: dict[str, Any], warn_only: bool) -> None:
    rep.sample["transcript"] = text
    if not text.strip():
        rep.add("transcript", "fail" if not warn_only else "warn", "empty transcript for a clip with speech")
        if not warn_only:
            return
    w = _wer_vs_truth(text, truth)
    rep.sample["wer"] = w
    if w <= WER_WARN:
        rep.add("transcript", "pass", f"transcript matches the clip (WER {w:.1%})")
        rep.capabilities.append("asr")
    elif warn_only or w <= WER_FAIL_CHAT:
        rep.add("transcript", "warn", f"transcript is far from the clip's text (WER {w:.1%}); check the model before "
                "trusting its scores")
        rep.capabilities.append("asr")
    else:
        rep.add("transcript", "fail", f"audio part ignored — model can't take audio (answer unrelated to the clip, "
                f"WER {w:.0%})")


def check_chat(rep: CheckReport, audio: np.ndarray, truth: dict[str, Any], sha: str) -> None:
    a = adapter_class("openai", "chat.completions")(rep.service)
    try:
        out, latency = a.run(audio, AudioRef(sha))
    except Exception as exc:  # noqa: BLE001
        code = _status_code(exc)
        if code and 400 <= code < 500:
            rep.add("request", "fail", f"audio part rejected ({code}) — model can't take audio: {_short(exc)}")
        else:
            rep.add("request", "fail", f"chat request failed: {_short(exc)}")
        return
    rep.add("request", "pass", f"chat request with an input_audio part accepted ({latency:.2f} s)")
    _judge_transcript(rep, out["text"], truth, warn_only=False)
    if "asr" in rep.capabilities:
        rep.timing_level = "text"
        rep.add("timing", "warn", "text only: audio LLMs return no timestamps; pipelines take timing from the "
                "segmentation stage")


def check_segmenter(rep: CheckReport, entry: dict[str, Any], audio: np.ndarray, truth: dict[str, Any],
                    sha: str) -> None:
    outputs = {t["name"] for t in (entry.get("signature") or {}).get("outputs", [])}
    if "SEGMENTS" not in outputs:
        rep.add("signature", "fail", "signature has no SEGMENTS output")
        return
    rep.add("signature", "pass", "AUDIO + SAMPLE_RATE in, SEGMENTS" + (" + SPEAKERS" if "SPEAKERS" in outputs else "")
            + " out")
    a = adapter_class("oip-v2", "infer")(rep.service)
    try:
        out, latency = a.run(audio, AudioRef(sha))
    except Exception as exc:  # noqa: BLE001
        rep.add("request", "fail", f"infer failed: {_short(exc)}")
        return
    rep.add("request", "pass", f"infer accepted ({latency:.2f} s)")
    dur = len(audio) / 16000
    segs = out["segments"]
    rep.sample["segments"] = segs[:50]
    rep.sample["speakers"] = (out["speakers"] or [])[:50]
    if not segs:
        rep.add("segments", "fail", "no segments returned for a clip with speech")
        return
    bad = [s for s in segs if s[0] < -0.05 or s[1] > dur + 0.5 or s[1] < s[0]]
    if bad:
        rep.add("segments", "fail", f"{len(bad)} segment(s) outside the clip's bounds or reversed, e.g. {bad[0]}")
        return
    ref = [der_mod.Turn(t["start"], t["end"], t["speaker"]) for t in truth["turns"]]
    if out["speakers"] is None or "SPEAKERS" not in outputs:
        rep.capabilities.append("vad")
        rep.timing_level = "segment"
        hyp = [der_mod.Turn(s, e, "speech") for s, e in segs]
        from ..metrics.vad import detection

        d = detection(ref, hyp, dur)
        rep.add("segments", "pass", f"speech-only segments: VAD. {len(segs)} segments; missed speech "
                f"{d['vad_missed']:.2f} s, false alarm {d['vad_false_alarm']:.2f} s")
        return
    rep.capabilities.append("diar")
    rep.timing_level = "segment"
    n_spk = len(set(out["speakers"]))
    hyp = [der_mod.Turn(s, e, k) for (s, e), k in zip(segs, out["speakers"], strict=True)]
    d = der_mod.der(ref, hyp, dur)
    rep.sample["der"] = d["der"]
    if n_spk == len(truth["speakers"]):
        rep.add("speakers", "pass", f"diarisation: found {n_spk} speakers, as expected (DER on the clip "
                f"{d['der']:.1%})")
    else:
        rep.add("speakers", "warn", f"diarisation: found {n_spk} speaker(s) on a {len(truth['speakers'])}-speaker "
                f"clip (DER {d['der']:.1%})")


def run_check(service: str, log: Callable[[str], None] = lambda m: None) -> CheckReport:
    entry = reader.get(service)
    rep = CheckReport(entry["name"], entry["usable_as"])
    if not entry["usable_as"]:
        rep.add("contract", "fail", f"not usable as a model: {entry['not_usable']}")
        return rep
    rep.add("contract", "pass", f"{entry['contract']} / {entry['api']}")
    audio, truth, sha = check_clip()
    log(f"probing {entry['name']} with the {len(audio) / 16000:.1f} s check clip")
    if entry["usable_as"] == "asr":
        check_asr(rep, audio, truth, sha)
    elif entry["usable_as"] == "chat-audio":
        check_chat(rep, audio, truth, sha)
    else:
        check_segmenter(rep, entry, audio, truth, sha)
    if not rep.capabilities:
        rep.add("result", "fail", "no usable capability detected; this model cannot be added")
    else:
        rep.add("result", "pass", "detected: " + ", ".join(c.upper() if c != "diar" else "diarisation"
                                                           for c in rep.capabilities))
    return rep


def check_clip_path() -> Path:
    return CHECK_DIR / "check_clip.wav"
