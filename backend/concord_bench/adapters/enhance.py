"""`oip-v2` / `infer` with AUDIO FP32 [-1] + SAMPLE_RATE INT32 [1] in and AUDIO (+ SAMPLE_RATE) out: speech
enhancement (the platform's `enhancer`, verified by the catalog; platform contracts/README.md §4).

An enhancer's output is audio, not a JSON output, so it never goes through the output cache or the
record / replay transport: the enhanced copy of a dataset is its cache (`runs.enhance`). Calls retry the
way other adapters do.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import numpy as np

from .base import RETRY_DELAYS, AdapterError, _retryable

TIMEOUT_S = 900.0  # audio goes as JSON tensors both ways: ~50 MB each way for 6 min


class OIPEnhancer:
    kind = "enh"
    contract = "oip-v2"
    api = "infer"

    def __init__(self, service: str, options: dict[str, Any] | None = None, *, client=None):
        self.service = service
        self.options = options or {}
        self._client = client

    @property
    def params(self) -> dict[str, Any]:
        return {"adapter": type(self).__name__, **self.options}

    def client(self):
        if self._client is None:
            import aicore

            self._client = aicore.client(self.service, timeout=TIMEOUT_S)
        return self._client

    def call(self, audio: np.ndarray, sample_rate: int = 16000) -> tuple[np.ndarray, int]:
        """(enhanced samples, their sample rate) for one stretch of mono audio."""
        out = self.client().infer({"AUDIO": np.asarray(audio, dtype=np.float32),
                                   "SAMPLE_RATE": np.array([sample_rate], dtype=np.int32)})
        if "AUDIO" not in out:
            raise AdapterError(f"{self.service}: no AUDIO output returned")
        rate = int(np.asarray(out["SAMPLE_RATE"]).reshape(-1)[0]) if "SAMPLE_RATE" in out else sample_rate
        return np.asarray(out["AUDIO"], dtype=np.float32).reshape(-1), rate

    def run(self, audio: np.ndarray, sample_rate: int = 16000,
            sleep: Callable[[float], None] = time.sleep) -> tuple[np.ndarray, float]:
        """(enhanced 16 kHz samples, latency seconds), with the retries other adapters use."""
        last: Exception | None = None
        for attempt, delay in enumerate((0, *RETRY_DELAYS)):
            if delay:
                sleep(delay)
            t0 = time.perf_counter()
            try:
                out, rate = self.call(audio, sample_rate)
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                if _retryable(exc) and attempt < len(RETRY_DELAYS):
                    continue
                raise
            if rate != sample_rate:
                raise AdapterError(f"{self.service}: returned audio at {rate} Hz for {sample_rate} Hz input")
            if not np.all(np.isfinite(out)):
                raise AdapterError(f"{self.service}: NaN or infinite samples in the output")
            return out, time.perf_counter() - t0
        raise AdapterError(f"{self.service}: gave up after retries: {last}")
