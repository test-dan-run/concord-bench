"""Generate the synthesised fixture set with kokoro-tts (PLAN §5 M0, Q10).

    just fixtures            # needs `kokoro-tts` up (the script starts nothing itself)

Each turn in dialogues.yaml is synthesised alone (24 kHz), trimmed to its speech, and mixed at the
offset the spec gives, so reference turns and transcripts are exact by construction. Synthesised
turns are kept in fixtures/synth/turns/ (committed): kokoro on a GPU is not bit-exact between runs,
so re-running reuses them and gives identical files. Delete a turn file to re-synthesise it.

Writes fixtures/generated/:
  dataset/audio/   six dialogues plus the ingest edge cases (8 kHz, 44.1 kHz stereo MP3, OGG,
                   clipped, corrupt)
  dataset/truth.json
  refs/<format>/   the same references in every supported format
  malformed/       broken annotation files for parser tests
  templates/       downloadable annotation templates for the upload page, and reference_package.zip
                   (a reference package, CONTRACTS.md §2) for the import page

    python fixtures/synth/make.py --templates-only   # rewrite templates/ from the files above, no TTS
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf
import soxr
import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = ROOT / "generated"
CACHE = HERE / "turns"  # committed: GPU synthesis is not bit-exact, so the turns are the source of truth
TTS_SR = 24000
OUT_SR = 16000
DEFAULT_GAP = 0.35


# ---------------------------------------------------------------- synthesis

def synth(voice: str, text: str, service: str) -> np.ndarray:
    key = hashlib.sha256(f"{service}|{voice}|{text}".encode()).hexdigest()[:20]
    cached = CACHE / f"{key}.flac"
    if cached.exists():
        data, sr = sf.read(cached, dtype="float32")
        assert sr == TTS_SR
        return data
    import aicore

    client = aicore.client(service)
    resp = client.audio.speech.create(model=aicore.model_name(service), voice=voice, input=text,
                                      response_format="wav")
    data, sr = sf.read(io.BytesIO(resp.content), dtype="float32")
    if data.ndim > 1:
        data = data.mean(axis=1)
    if sr != TTS_SR:
        data = soxr.resample(data, sr, TTS_SR).astype(np.float32)
    CACHE.mkdir(parents=True, exist_ok=True)
    sf.write(cached, data, TTS_SR, subtype="PCM_16", format="FLAC")
    data, _ = sf.read(cached, dtype="float32")  # identical whether cached or fresh
    return data


def trim(x: np.ndarray, sr: int = TTS_SR, rel_db: float = -40.0) -> np.ndarray:
    hop = sr // 100
    n = len(x) // hop
    if n == 0:
        return x
    rms = np.sqrt((x[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    thr = max(rms.max() * 10 ** (rel_db / 20), 1e-4)
    on = np.flatnonzero(rms > thr)
    if not len(on):
        return x
    return x[on[0] * hop: (on[-1] + 1) * hop]


def render(spec: dict, service: str) -> tuple[np.ndarray, list[dict]]:
    """Mix a dialogue at 24 kHz; returns (audio, turns with exact times)."""
    placed, prev_end, prev_start = [], 0.0, 0.0
    for t in spec["turns"]:
        audio = trim(synth(spec["voices"][t["spk"]], t["text"], service))
        if "at" in t:
            start = float(t["at"])
        elif "overlap" in t:
            start = max(prev_start + 0.2, prev_end - float(t["overlap"]))
        else:
            start = prev_end + float(t.get("gap", DEFAULT_GAP))
        s0 = int(round(start * TTS_SR))
        start = s0 / TTS_SR
        end = (s0 + len(audio)) / TTS_SR
        placed.append((s0, audio, {"speaker": t["spk"], "start": round(start, 3), "end": round(end, 3),
                                   "text": t["text"]}))
        prev_end, prev_start = max(prev_end, end), start
    total = int(round((prev_end + 0.6) * TTS_SR))
    mix = np.zeros(total, dtype=np.float32)
    for s0, audio, _ in placed:
        mix[s0: s0 + len(audio)] += audio
    mix *= 0.5 / max(1e-6, np.abs(mix).max())
    if spec.get("noise_db") is not None:
        seed = int(hashlib.sha256(spec["id"].encode()).hexdigest()[:8], 16)
        mix += np.random.default_rng(seed).normal(0, 10 ** (spec["noise_db"] / 20), total).astype(np.float32)
    return mix, sorted((p[2] for p in placed), key=lambda r: (r["start"], r["end"]))


# ---------------------------------------------------------------- writing audio

def to16k(x: np.ndarray) -> np.ndarray:
    return soxr.resample(x, TTS_SR, OUT_SR, quality="HQ").astype(np.float32)


def ffmpeg(src: Path, dst: Path, *args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(src), "-map_metadata", "-1",
                    "-fflags", "+bitexact", "-flags:a", "+bitexact", *args, str(dst)], check=True)


def write_output(mix24: np.ndarray, out: dict, dst_dir: Path, stem: str) -> Path:
    fmt = out.get("format", "flac")
    gain = 10 ** (out.get("gain_db", 0) / 20)
    x = np.clip(mix24 * gain, -1.0, 1.0)
    tmp = dst_dir / f".{stem}.24k.wav"
    sf.write(tmp, x, TTS_SR, subtype="PCM_16")
    sr = str(out.get("sample_rate", OUT_SR))
    ch = str(out.get("channels", 1))
    if fmt == "flac":
        dst = dst_dir / f"{stem}.flac"
        sf.write(dst, to16k(x), OUT_SR, subtype="PCM_16", format="FLAC")
    elif fmt == "wav":
        dst = dst_dir / f"{stem}.wav"
        ffmpeg(tmp, dst, "-ar", sr, "-ac", ch, "-c:a", "pcm_s16le")
    elif fmt == "mp3":
        dst = dst_dir / f"{stem}.mp3"
        ffmpeg(tmp, dst, "-ar", sr, "-ac", ch, "-c:a", "libmp3lame", "-b:a", out.get("bitrate", "96k"), "-write_xing", "0")
    elif fmt == "ogg":
        dst = dst_dir / f"{stem}.ogg"
        ffmpeg(tmp, dst, "-ar", sr, "-ac", ch, "-c:a", "libvorbis", "-q:a", "3")
    else:
        raise ValueError(f"unknown output format {fmt}")
    tmp.unlink()
    return dst


# ---------------------------------------------------------------- references

def write_refs(files: dict[str, dict], refs: Path) -> None:
    """The same truth in every supported annotation format."""
    if refs.exists():
        shutil.rmtree(refs)
    for d in ("stm", "rttm", "json-segments", "jsonl-segments", "seglst", "asr-jsonl", "asr-csv", "asr-txt",
              "diar-json", "nemo-manifest/rttm", "nemo-diar"):
        (refs / d).mkdir(parents=True)
    stm, rttm, jsonl, seglst, asr_jsonl, nemo = [], [], [], [], [], []
    with open(refs / "asr-csv" / "transcripts.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["audio_filepath", "text"])
        for stem, f in files.items():
            text = " ".join(t["text"] for t in f["turns"])
            w.writerow([f["file"], text])
            asr_jsonl.append({"audio_filepath": f["file"], "text": text, "duration": f["duration"],
                              "condition": f["condition"]})
            (refs / "asr-txt" / f"{stem}.txt").write_text(text + "\n")
            segs = [{"file": f["file"], "start": t["start"], "end": t["end"], "speaker": t["speaker"],
                     "text": t["text"]} for t in f["turns"]]
            (refs / "json-segments" / f"{stem}.json").write_text(json.dumps({"file": f["file"], "segments": segs},
                                                                           indent=1) + "\n")
            (refs / "diar-json" / f"{stem}.json").write_text(json.dumps(
                [{k: v for k, v in s.items() if k != "text"} for s in segs], indent=1) + "\n")
            jsonl += segs
            speakers = sorted({t["speaker"] for t in f["turns"]})
            # NeMo diarisation manifest (paths as on a training box; only the RTTM file names matter) + its RTTMs
            nemo.append({"audio_filepath": f"/data/audio/{f['file']}", "offset": 0, "duration": None,
                         "label": "infer", "text": "-", "num_speakers": len(speakers),
                         "rttm_filepath": f"/data/rttm/{stem}.rttm", "uem_filepath": None})
            (refs / "nemo-manifest" / "rttm" / f"{stem}.rttm").write_text("".join(
                f"SPEAKER {stem} 1 {t['start']:.3f} {t['end'] - t['start']:.3f} <NA> <NA> {t['speaker']} <NA> <NA>\n"
                for t in f["turns"]))
            (refs / "nemo-diar" / f"{stem}.json").write_text(json.dumps({
                "status": "success", "session_id": stem, "transcription": text, "speaker_count": len(speakers),
                "sentences": [{"speaker": t["speaker"], "start_time": t["start"], "end_time": t["end"],
                               "text": t["text"]} for t in f["turns"]]}, indent=1) + "\n")
            for t in f["turns"]:
                stm.append(f"{stem} 1 {t['speaker']} {t['start']:.3f} {t['end']:.3f} <o,f0,{f['condition']}> "
                           f"{t['text']}")
                rttm.append(f"SPEAKER {stem} 1 {t['start']:.3f} {t['end'] - t['start']:.3f} <NA> <NA> "
                            f"{t['speaker']} <NA> <NA>")
                seglst.append({"session_id": stem, "speaker": t["speaker"], "start_time": t["start"],
                               "end_time": t["end"], "words": t["text"]})
    (refs / "stm" / "fixtures.stm").write_text(";; synthesised fixture references\n" + "\n".join(stm) + "\n")
    (refs / "rttm" / "fixtures.rttm").write_text("\n".join(rttm) + "\n")
    (refs / "jsonl-segments" / "segments.jsonl").write_text("".join(json.dumps(s) + "\n" for s in jsonl))
    (refs / "seglst" / "fixtures.seglst.json").write_text(json.dumps(seglst, indent=1) + "\n")
    (refs / "asr-jsonl" / "manifest.jsonl").write_text("".join(json.dumps(s) + "\n" for s in asr_jsonl))
    (refs / "nemo-manifest" / "manifest.json").write_text("".join(json.dumps(s) + "\n" for s in nemo))


def write_malformed(d: Path) -> None:
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    (d / "bad.rttm").write_text("SPEAKER d01 1 0.5 notanumber <NA> <NA> agent <NA> <NA>\n"
                                "SPEAKER d01 1 2.0 1.5 <NA> <NA> agent <NA> <NA>\n"
                                "SPEAKER d01 1\n")
    (d / "bad.jsonl").write_text('{"audio_filepath": "d01.flac", "text": "fine"}\n'
                                 "not json\n"
                                 '{"text": "no file field"}\n'
                                 '{"file": "d01.flac", "start": 3.0, "end": 1.0, "speaker": "a"}\n')
    (d / "bad.stm").write_text(";; comment\nd01 1 agent 0.5\nd01 1 agent 1.0 2.0 hello there\n")
    (d / "bad.json").write_text("{ this is not json")
    (d / "bad.csv").write_text("audio_filepath,text\nd01.flac,hello\n,missing file\n")


def write_templates(d: Path, example: dict) -> None:
    """Small, valid files in every format, built from one fixture dialogue."""
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    f, stem = example, example["stem"]
    turns = f["turns"][:3]
    text = " ".join(t["text"] for t in turns)
    (d / "asr.jsonl").write_text(json.dumps({"audio_filepath": f"audio/{stem}.wav", "text": text}) + "\n")
    with open(d / "asr.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["audio_filepath", "text"])
        w.writerow([f"audio/{stem}.wav", text])
    (d / f"{stem}.txt").write_text(text + "\n")
    (d / "diarisation.rttm").write_text("".join(
        f"SPEAKER {stem} 1 {t['start']:.3f} {t['end'] - t['start']:.3f} <NA> <NA> {t['speaker']} <NA> <NA>\n"
        for t in turns))
    (d / "diarisation_segments.json").write_text(json.dumps(
        [{"file": f"{stem}.wav", "start": t["start"], "end": t["end"], "speaker": t["speaker"]} for t in turns],
        indent=1) + "\n")
    (d / "speaker_transcripts.jsonl").write_text("".join(json.dumps(
        {"file": f"{stem}.wav", "start": t["start"], "end": t["end"], "speaker": t["speaker"], "text": t["text"]})
        + "\n" for t in turns))
    (d / "speaker_transcripts.stm").write_text("".join(
        f"{stem} 1 {t['speaker']} {t['start']:.3f} {t['end']:.3f} {t['text']}\n" for t in turns))
    (d / "speaker_transcripts.seglst.json").write_text(json.dumps(
        [{"session_id": stem, "speaker": t["speaker"], "start_time": t["start"], "end_time": t["end"],
          "words": t["text"]} for t in turns], indent=1) + "\n")
    (d / "nemo_manifest.json").write_text(json.dumps(
        {"audio_filepath": f"audio/{stem}.wav", "duration": f["duration"], "text": text}) + "\n")
    (d / "nemo_diarisation_manifest.json").write_text(json.dumps(
        {"audio_filepath": f"audio/{stem}.wav", "offset": 0, "duration": None, "label": "infer", "text": "-",
         "num_speakers": len({t["speaker"] for t in turns}), "rttm_filepath": "diarisation.rttm",
         "uem_filepath": None}) + "\n")
    (d / "nemo_diarisation_output.json").write_text(json.dumps(
        {"status": "success", "session_id": stem, "transcription": text,
         "speaker_count": len({t["speaker"] for t in turns}),
         "sentences": [{"speaker": t["speaker"], "start_time": t["start"], "end_time": t["end"], "text": t["text"]}
                       for t in turns]}, indent=1) + "\n")
    (d / "README.md").write_text(TEMPLATE_README)
    write_reference_template(d / "reference_package.zip", TEMPLATE_PACKAGE_FILE)


TEMPLATE_PACKAGE_FILE = "d04"  # the smallest dialogue keeps the template download small


def write_reference_template(dest: Path, stem: str) -> None:
    """A reference package (CONTRACTS.md §2) written by hand: manifest without a source, files.csv,
    audio/, one references file. Byte-identical between runs (fixed zip timestamps)."""
    files = json.loads((OUT / "dataset" / "truth.json").read_text())
    f = files[stem]
    audio = OUT / "dataset" / "audio" / f["file"]
    info = sf.info(audio)
    turns = f["turns"][:3]
    top = "reference_package"
    manifest = {
        "format": "concord-reference-package", "format_version": 1, "name": "Template reference package",
        "created_at": "2026-10-22T00:00:00Z", "language": "en", "tags": ["template"],
        "description": "A reference package made by hand. Replace the audio, files.csv and refs/ with yours.",
        "scope": "complete", "label_types": ["asr", "diar"],
        "counts": {"files": 1, "segments": len(turns), "hours": round(f["duration"] / 3600, 6)},
        "audio_included": "all",
    }
    files_csv = io.StringIO()
    w = csv.writer(files_csv, lineterminator="\n")
    w.writerow(["path", "sha256", "duration", "sample_rate"])
    w.writerow([f["file"], hashlib.sha256(audio.read_bytes()).hexdigest(), f["duration"], info.samplerate])
    seglst = [{"session_id": stem, "speaker": t["speaker"], "start_time": t["start"], "end_time": t["end"],
               "words": t["text"]} for t in turns]
    entries = {
        "manifest.json": (json.dumps(manifest, indent=1) + "\n").encode(),
        "files.csv": files_csv.getvalue().encode(),
        f"audio/{f['file']}": audio.read_bytes(),
        "refs/segments.seglst.json": (json.dumps(seglst, indent=1) + "\n").encode(),
        "README.md": REFERENCE_TEMPLATE_README.encode(),
    }
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zi = zipfile.ZipInfo(f"{top}/{name}", date_time=(2026, 10, 22, 0, 0, 0))
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            zf.writestr(zi, data)


REFERENCE_TEMPLATE_README = """# Reference package template

A reference package hands finished references to Concord Bench (Datasets -> Import reference package).
Concord Label writes them; you can also make one by hand or with another tool. Zip the folder (or upload
the folder itself).

    reference_package/
      manifest.json      format "concord-reference-package", format_version 1, name, created_at,
                         label_types (asr and/or diar), counts {files, segments, hours},
                         audio_included (all | partial | none); optional language, tags, description,
                         scope, review, models, normaliser
      files.csv          path,sha256,duration,sample_rate - one row per audio file
      audio/<path>       the audio; a file Concord Bench already has (same sha256) may be left out
      refs/<one file>    the references, in any format the annotated upload reads (see the other
                         templates), matched to the audio by file name without the extension

Add `"source": {"app", "install_id", "dataset_uid", "dataset_name"}` and `"export_version": 1, 2, ...`
to the manifest when the same dataset will be sent again: a package from the same source becomes a new
version of the dataset (scored references are never changed). A folder without manifest.json is read
like an annotated upload.

The full format is in CONTRACTS.md section 2 of the Concord Bench repository.
"""


TEMPLATE_README = """# Concord annotation templates

Annotations are matched to audio files by file name without the extension ("stem"), so
`audio/call_01.wav` matches `call_01.txt`, an RTTM line for `call_01`, or `"file": "call_01.wav"`.
Times are seconds from the start of the file.

| File | Task | Notes |
|---|---|---|
| asr.jsonl | ASR | One JSON object per line: `audio_filepath`, `text`. Optional `start`/`end` (or `offset`/`duration`) for segment-level transcripts; extra fields become slice metadata. |
| asr.csv | ASR | Columns `audio_filepath`, `text` (optional `start`, `end`, `speaker`). |
| <stem>.txt | ASR | One transcript file per audio file, same stem. |
| diarisation.rttm | Diarisation | Standard RTTM `SPEAKER` lines. |
| diarisation_segments.json | Diarisation | List of `{file, start, end, speaker}`. |
| speaker_transcripts.jsonl | ASR + diarisation + cpWER | `{file, start, end, speaker, text}` per line (a `.json` list works too). |
| speaker_transcripts.stm | ASR + diarisation + cpWER | `file channel speaker start end [<label>] transcript`. |
| speaker_transcripts.seglst.json | ASR + diarisation + cpWER | SegLST: `session_id, speaker, start_time, end_time, words`. |
| nemo_manifest.json | ASR | NVIDIA NeMo manifest: one JSON object per line with `audio_filepath`, `duration`, `text` (optional `offset`). `.json` or `.jsonl`. |
| nemo_diarisation_manifest.json | Diarisation | NeMo diarisation manifest. Speaker labels come from the RTTM named by `rttm_filepath`, so upload those RTTM files too (matched by file name; the folder in the path does not matter). |
| nemo_diarisation_output.json | ASR + diarisation + cpWER | NeMo diarisation output: `session_id` plus `sentences` of `{speaker, start_time, end_time, text}` (or only `words` of `{word, start_time, end_time, speaker}`). |
| reference_package.zip | Import reference package | A reference package (manifest, files.csv, audio, one references file) for Datasets -> Import reference package. Its README.md explains the layout. |

cpWER needs speaker-attributed transcripts (STM, SegLST, JSON/JSONL segments with text, or NeMo
diarisation output). An RTTM plus per-file text gives WER and DER, but not cpWER.
"""


# ---------------------------------------------------------------- main

def corrupt_bytes(seed: str, n: int = 48000) -> bytes:
    rng = np.random.default_rng(int(hashlib.sha256(seed.encode()).hexdigest()[:8], 16))
    return b"NOTRIFF" + rng.integers(0, 256, n, dtype=np.uint8).tobytes()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service", default="kokoro-tts")
    ap.add_argument("--templates-only", action="store_true",
                    help="rewrite templates/ from the existing generated files (no TTS)")
    args = ap.parse_args(argv)
    if args.templates_only:
        files = json.loads((OUT / "dataset" / "truth.json").read_text())
        write_templates(OUT / "templates", files["d01"])
        print(f"wrote {OUT / 'templates'}")
        return 0
    spec = yaml.safe_load((HERE / "dialogues.yaml").read_text())

    try:
        # dataset
        audio_dir = OUT / "dataset" / "audio"
        if audio_dir.exists():
            shutil.rmtree(audio_dir)
        audio_dir.mkdir(parents=True)
        files: dict[str, dict] = {}
        mixes: list[tuple[np.ndarray, list[dict], dict]] = []
        for d in spec["dataset"]:
            mix, turns = render(d, args.service)
            dst = write_output(mix, d.get("output", {}), audio_dir, d["id"])
            files[d["id"]] = {"stem": d["id"], "file": dst.name, "duration": round(len(mix) / TTS_SR, 3),
                              "condition": d.get("condition", "clean"), "voices": d["voices"],
                              "output": d.get("output", {}), "turns": turns}
            mixes.append((mix, turns, d))
            print(f"{dst.name}: {len(mix) / TTS_SR:.1f} s, {len(turns)} turns, {len(d['voices'])} speakers")

        # corrupt file: not audio at all
        (audio_dir / "d07_corrupt.wav").write_bytes(corrupt_bytes("d07_corrupt"))
        files["d07_corrupt"] = {"stem": "d07_corrupt", "file": "d07_corrupt.wav", "duration": None,
                                "condition": "corrupt", "voices": {}, "output": {"format": "corrupt"},
                                "turns": [{"speaker": "agent", "start": 0.0, "end": 1.0, "text": "unreadable"}]}

        (OUT / "dataset" / "truth.json").write_text(json.dumps(files, indent=1) + "\n")
        write_refs(files, OUT / "refs")
        write_malformed(OUT / "malformed")
        write_templates(OUT / "templates", files["d01"])
    except Exception as exc:  # noqa: BLE001 - one readable line for the most common failure
        if type(exc).__name__ in ("APIConnectionError", "ConnectError"):
            print(f"error: cannot reach {args.service}; start it with `just up {args.service}` in the platform repo",
                  file=sys.stderr)
            return 1
        raise
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
