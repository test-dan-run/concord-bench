"""`openai` / `audio.transcriptions`: multipart upload of the 16 kHz audio (or a segment cut).

Asks for the best timing level the catalog's verifier found: word times
(`verbose_json` + `timestamp_granularities[]=word`), segment times (`verbose_json`), or text (`json`).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ..datasets.audio import wav_bytes
from .base import Adapter, register

TIMEOUT_S = 600.0


@register
class OpenAIASR(Adapter):
    kind = "asr"
    contract = "openai"
    api = "audio.transcriptions"

    def __init__(self, service: str, options: dict[str, Any] | None = None, *, client=None):
        super().__init__(service, {"timing": "text", **(options or {})})
        self._client = client

    def client(self):
        if self._client is None:
            import aicore

            self._client = aicore.client(self.service, timeout=TIMEOUT_S, max_retries=0)
        return self._client

    def request_kwargs(self) -> dict[str, Any]:
        import aicore

        timing = self.options.get("timing", "text")
        kw: dict[str, Any] = {"model": aicore.model_name(self.service),
                              "response_format": "json" if timing == "text" else "verbose_json"}
        if timing == "word":
            kw["timestamp_granularities"] = ["word"]
        if self.options.get("language"):
            kw["language"] = self.options["language"]
        return kw

    def call(self, audio: np.ndarray) -> Any:
        kw = self.request_kwargs()
        resp = self.client().audio.transcriptions.with_raw_response.create(
            file=("audio.wav", wav_bytes(audio), "audio/wav"), **kw)
        return resp.http_response.json()

    def parse(self, raw: Any, duration: float) -> dict[str, Any]:
        return parse_transcription(raw)


def parse_transcription(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        return {"text": raw.strip(), "words": None, "segments": None}
    text = (raw.get("text") or "").strip()
    words = None
    if raw.get("words"):
        words = [{"w": str(w.get("word", "")).strip(), "start": float(w["start"]), "end": float(w["end"])}
                 for w in raw["words"] if str(w.get("word", "")).strip()]
    segments = None
    if raw.get("segments"):
        segments = [{"start": float(s["start"]), "end": float(s["end"]), "text": str(s.get("text", "")).strip()}
                    for s in raw["segments"] if "start" in s and "end" in s]
    return {"text": text, "words": words or None, "segments": segments or None}
