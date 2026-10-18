"""The adapter interface, the adapter registry, and the record/replay transport.

An adapter turns 16 kHz mono audio into a normalised output dict:
- ASR: {"text": str, "words": [{"w", "start", "end"}] | None, "segments": [{"start", "end", "text"}] | None}
- seg: {"segments": [[start, end], ...], "speakers": ["spk0", ...] | None}

Each adapter splits the work in two: `call()` makes the HTTP request and returns the raw JSON-able
response, `parse()` turns it into the normalised output. Recordings hold raw responses, so replayed
tests still exercise every parser.

Record / replay (settings `record_dir`, `replay_dir`): files at `<dir>/<service>/<key>.json`, keyed
on the same (service, audio sha256, span, params) as the output cache.
"""

from __future__ import annotations

import hashlib
import json
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy as np

from ..core.settings import get_settings

RETRY_DELAYS = (2, 4, 8, 16)


class AdapterError(Exception):
    pass


class ReplayMiss(AdapterError):
    pass


def params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class AudioRef:
    """The audio a call is about: the file's sha256 plus a span (end = -1 for the whole file)."""

    sha256: str
    start: float = 0.0
    end: float = -1.0

    def key(self, service: str, phash: str) -> str:
        span = "full" if self.end < 0 else f"{self.start:.3f}-{self.end:.3f}"
        return f"{self.sha256[:16]}_{span}_{phash}"


class Adapter(ABC):
    kind: ClassVar[str]  # asr | seg
    contract: ClassVar[str]
    api: ClassVar[str]

    def __init__(self, service: str, options: dict[str, Any] | None = None):
        self.service = service
        self.options = options or {}

    @property
    def params(self) -> dict[str, Any]:
        """Everything besides the audio that changes the output; part of the cache key."""
        return {"adapter": type(self).__name__, **self.options}

    @property
    def params_hash(self) -> str:
        return params_hash(self.params)

    @abstractmethod
    def call(self, audio: np.ndarray) -> Any: ...

    @abstractmethod
    def parse(self, raw: Any, duration: float) -> dict[str, Any]: ...

    def run(self, audio: np.ndarray, ref: AudioRef) -> tuple[dict[str, Any], float]:
        """(normalised output, latency seconds), through replay / record / retries."""
        raw, latency = invoke(self, audio, ref)
        return self.parse(raw, len(audio) / 16000), latency


def _recording_path(root: Path, service: str, key: str) -> Path:
    return root / service / f"{key}.json"


def _retryable(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 503:
        return True
    name = type(exc).__name__
    return name in ("APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout", "RemoteProtocolError",
                    "InternalServerError") or (status is not None and status >= 500 and status != 501)


def invoke(adapter: Adapter, audio: np.ndarray, ref: AudioRef,
           sleep: Callable[[float], None] = time.sleep) -> tuple[Any, float]:
    s = get_settings()
    key = ref.key(adapter.service, adapter.params_hash)
    if s.replay_dir is not None:
        path = _recording_path(s.replay_dir, adapter.service, key)
        if not path.is_file():
            raise ReplayMiss(f"no recorded response for {adapter.service} at {path.name}")
        rec = json.loads(path.read_text())
        return rec["raw"], rec["latency_s"]
    last: Exception | None = None
    for attempt, delay in enumerate((0, *RETRY_DELAYS)):
        if delay:
            sleep(delay)
        t0 = time.perf_counter()
        try:
            raw = adapter.call(audio)
        except Exception as exc:  # noqa: BLE001 - classified below
            last = exc
            if _retryable(exc) and attempt < len(RETRY_DELAYS):
                continue
            raise
        latency = time.perf_counter() - t0
        if s.record_dir is not None:
            path = _recording_path(s.record_dir, adapter.service, key)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"service": adapter.service, "params": adapter.params,
                                        "audio": {"sha256": ref.sha256, "start": ref.start, "end": ref.end},
                                        "latency_s": round(latency, 4), "raw": raw}, indent=None) + "\n")
        return raw, latency
    raise AdapterError(f"{adapter.service}: gave up after retries: {last}")


# ---------------------------------------------------------------- registry

AdapterFactory = Callable[..., Adapter]
REGISTRY: dict[tuple[str, str], type[Adapter]] = {}


def register(cls: type[Adapter]) -> type[Adapter]:
    REGISTRY[(cls.contract, cls.api)] = cls
    return cls


def adapter_class(contract: str, api: str) -> type[Adapter] | None:
    from . import oip_segmenter, openai_asr, openai_chat_audio  # noqa: F401 - registration side effect

    return REGISTRY.get((contract, api))
