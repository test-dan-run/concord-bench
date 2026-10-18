"""Audio ingest helpers: fingerprint, probe and decode with ffmpeg, clipping check, the derived
16 kHz mono FLAC every model sees, and waveform peaks for the timeline.

Originals are kept byte-for-byte; everything else is derived from them once.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr

TARGET_SR = 16000
PEAKS_PER_SECOND = 100
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac", ".webm"}
CLIP_LEVEL = 0.999
CLIP_FRACTION = 0.001  # files with more than 0.1 % of samples at full scale are flagged as clipped
CLIP_MIN_RUN = 3  # ...counting only runs of at least this many consecutive full-scale samples


class DecodeError(Exception):
    pass


@dataclass
class Probe:
    duration: float
    sample_rate: int
    channels: int
    codec: str


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> Probe:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=sample_rate,channels,codec_name,duration:format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise DecodeError(r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "ffprobe failed")
    info = json.loads(r.stdout or "{}")
    streams = info.get("streams") or []
    if not streams:
        raise DecodeError("no audio stream")
    st = streams[0]
    dur = st.get("duration") or (info.get("format") or {}).get("duration")
    try:
        return Probe(float(dur) if dur not in (None, "N/A") else 0.0, int(st["sample_rate"]), int(st["channels"]),
                     st.get("codec_name", "?"))
    except (KeyError, ValueError) as exc:
        raise DecodeError(f"unreadable stream info: {exc}") from None


def decode(path: Path, sample_rate: int, channels: int) -> np.ndarray:
    """All channels at the native rate, float32 [samples, channels]."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-f", "f32le", "-acodec", "pcm_f32le",
                        "-ac", str(channels), "-ar", str(sample_rate), "-"], capture_output=True)
    if r.returncode != 0 or not r.stdout:
        msg = r.stderr.decode(errors="replace").strip().splitlines()
        raise DecodeError(msg[-1] if msg else "ffmpeg produced no audio")
    data = np.frombuffer(r.stdout, dtype=np.float32)
    return data[: len(data) // channels * channels].reshape(-1, channels)


def clipped_fraction(x: np.ndarray) -> float:
    """Fraction of samples inside runs of >= CLIP_MIN_RUN consecutive full-scale samples."""
    full = np.abs(x) >= CLIP_LEVEL
    if not full.any():
        return 0.0
    total = 0
    for ch in range(full.shape[1]):
        col = full[:, ch].astype(np.int8)
        edges = np.diff(np.concatenate(([0], col, [0])))
        starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
        runs = ends - starts
        total += int(runs[runs >= CLIP_MIN_RUN].sum())
    return total / full.size


def to_mono_16k(x: np.ndarray, sr: int) -> np.ndarray:
    mono = x.mean(axis=1) if x.shape[1] > 1 else x[:, 0]
    if sr != TARGET_SR:
        mono = soxr.resample(mono, sr, TARGET_SR, quality="HQ")
    return np.clip(mono, -1.0, 1.0).astype(np.float32)


def write_flac(path: Path, mono: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.flac")
    sf.write(tmp, mono, TARGET_SR, subtype="PCM_24", format="FLAC")
    tmp.replace(path)


def compute_peaks(mono: np.ndarray, sr: int = TARGET_SR, per_second: int = PEAKS_PER_SECOND) -> dict:
    """Min/max pairs per bucket, quantised to int8 range to keep the JSON small."""
    bucket = max(1, sr // per_second)
    n = int(np.ceil(len(mono) / bucket))
    padded = np.zeros(n * bucket, dtype=np.float32)
    padded[: len(mono)] = mono
    frames = padded.reshape(n, bucket)
    mins = np.round(frames.min(axis=1) * 127).astype(int)
    maxs = np.round(frames.max(axis=1) * 127).astype(int)
    return {"per_second": per_second, "duration": len(mono) / sr, "min": mins.tolist(), "max": maxs.tolist()}


def write_peaks(path: Path, peaks: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(peaks, separators=(",", ":")))


def load_derived(path: Path) -> np.ndarray:
    data, sr = sf.read(path, dtype="float32", always_2d=False)
    if sr != TARGET_SR:
        raise DecodeError(f"{path} is {sr} Hz, expected {TARGET_SR}")
    return data


def cut(mono: np.ndarray, start: float, end: float, sr: int = TARGET_SR) -> np.ndarray:
    a = max(0, int(round(start * sr)))
    b = min(len(mono), int(round(end * sr)))
    return mono[a:b]


def wav_bytes(mono: np.ndarray, sr: int = TARGET_SR) -> bytes:
    import io

    buf = io.BytesIO()
    sf.write(buf, mono, sr, subtype="PCM_16", format="WAV")
    return buf.getvalue()
