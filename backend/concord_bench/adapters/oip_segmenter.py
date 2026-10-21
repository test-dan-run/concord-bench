"""`oip-v2` / `infer` with inputs AUDIO FP32 [-1] + SAMPLE_RATE INT32 [1]: VAD and diarisation.

Outputs SEGMENTS FP32 [-1, 2], plus SPEAKERS INT32 [-1] for diarisers; speaker ints become
`spk0`, `spk1`, ... One adapter covers silero, pyannote and nemotron.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from .base import Adapter, register

TIMEOUT_S = 900.0  # a 5 min file is ~45-50 MB of JSON tensors


@register
class OIPSegmenter(Adapter):
    kind = "seg"
    contract = "oip-v2"
    api = "infer"

    def __init__(self, service: str, options: dict[str, Any] | None = None, *, client=None):
        super().__init__(service, options)
        self._client = client

    def client(self):
        if self._client is None:
            import aicore

            self._client = aicore.client(self.service, timeout=TIMEOUT_S)
        return self._client

    def call(self, audio: np.ndarray) -> Any:
        out = self.client().infer({"AUDIO": np.asarray(audio, dtype=np.float32),
                                   "SAMPLE_RATE": np.array([16000], dtype=np.int32)})
        raw: dict[str, Any] = {"SEGMENTS": np.asarray(out["SEGMENTS"], dtype=float).reshape(-1, 2).round(4).tolist()}
        if "SPEAKERS" in out:
            raw["SPEAKERS"] = np.asarray(out["SPEAKERS"]).reshape(-1).astype(int).tolist()
        return raw

    def parse(self, raw: Any, duration: float) -> dict[str, Any]:
        return parse_segments(raw)


def parse_segments(raw: dict[str, Any]) -> dict[str, Any]:
    segs = [[float(s), float(e)] for s, e in (raw.get("SEGMENTS") or [])]
    spk_raw = raw.get("SPEAKERS")
    speakers = None
    if spk_raw is not None:
        if len(spk_raw) != len(segs):
            raise ValueError(f"SPEAKERS has {len(spk_raw)} values for {len(segs)} segments")
        speakers = [f"spk{int(k)}" for k in spk_raw]
    order = sorted(range(len(segs)), key=lambda i: (segs[i][0], segs[i][1]))
    return {"segments": [segs[i] for i in order], "speakers": [speakers[i] for i in order] if speakers else None}
