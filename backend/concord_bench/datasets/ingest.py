"""Dataset ingest: scan a source (upload folder, zip, or local path), match annotations to audio by
stem, then copy originals, derive 16 kHz mono FLAC + peaks, and store reference segments.

`inspect_source` is cheap (no decoding) and drives the upload page's match counts and previews.
`ingest` runs as a job and reports progress. Concord Bench benchmarks against references, so a source
whose annotations match no audio file is refused (`NO_REFERENCES`); audio-only datasets belong in
Concord Label.
"""

from __future__ import annotations

import shutil
import zipfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import paths
from ..core.models import AudioFile, Dataset, FileStatus, LabelType, Segment, SegmentStatus
from ..core.settings import get_settings
from . import audio as audio_mod
from .parsers import ANNOTATION_EXTS, ParseResult, RefRecord, parse_file

Progress = Callable[[float, str], None]
NO_REFERENCES = ("no references found: Concord Bench benchmarks datasets with references, so add annotation "
                 "files whose names match the audio files (see the templates). To label audio, use Concord Label.")
SKIP_DIRS = {"__MACOSX", ".git"}


@dataclass
class Scan:
    root: Path
    audio: dict[str, Path] = field(default_factory=dict)  # rel path -> file
    annotations: dict[str, Path] = field(default_factory=dict)
    other: list[str] = field(default_factory=list)


def extract_zips(root: Path) -> list[str]:
    """Unpack every .zip under root next to itself (once), refusing paths that escape the folder."""
    done = []
    for z in sorted(root.rglob("*.zip")):
        target = z.with_suffix("")
        if target.exists():
            continue
        with zipfile.ZipFile(z) as zf:
            for member in zf.infolist():
                dest = (target / member.filename).resolve()
                if not str(dest).startswith(str(target.resolve())):
                    raise ValueError(f"{z.name}: unsafe path {member.filename!r}")
            zf.extractall(target)
        done.append(str(z.relative_to(root)))
    return done


def scan(root: Path) -> Scan:
    s = Scan(root)
    for p in sorted(root.rglob("*")):
        if not p.is_file() or any(part in SKIP_DIRS or part.startswith(".") for part in p.relative_to(root).parts):
            continue
        rel = p.relative_to(root).as_posix()
        ext = p.suffix.lower()
        if ext in audio_mod.AUDIO_EXTS:
            s.audio[rel] = p
        elif ext in ANNOTATION_EXTS:
            s.annotations[rel] = p
        elif ext != ".zip":
            s.other.append(rel)
    return s


def _stem_index(audio_rels: list[str]) -> dict[str, str]:
    """stem -> rel path. Duplicate stems resolve to the first path; the report lists them."""
    idx: dict[str, str] = {}
    for rel in audio_rels:
        stem = Path(rel).stem
        idx.setdefault(stem, rel)
        idx.setdefault(stem.lower(), rel)
    return idx


def match(scan_: Scan) -> dict[str, Any]:
    """Parse every annotation file and match records to audio files by stem."""
    idx = _stem_index(list(scan_.audio))
    parsed: list[ParseResult] = [parse_file(p, rel) for rel, p in scan_.annotations.items()]
    # Plain .txt files only count as transcripts when their stem names an audio file.
    parsed = [r for r in parsed if not (r.format == "txt" and r.records and r.records[0].stem not in idx
                                        and r.records[0].stem.lower() not in idx)]
    names = {Path(rel).name for rel in scan_.annotations}
    for res in parsed:
        for name in sorted(set(res.linked) - names):
            res.errors.append(f"rttm_filepath {name} is not in the upload; add the RTTM files alongside the manifest")
    by_task: dict[str, dict[str, Any]] = {}
    matched: dict[str, list[RefRecord]] = {}
    unmatched: Counter[str] = Counter()
    for res in parsed:
        for rec in res.records:
            rel = idx.get(rec.stem) or idx.get(rec.stem.lower())
            task = rec.kind
            t = by_task.setdefault(task, {"records": 0, "files": set(), "formats": set(), "preview": []})
            t["records"] += 1
            t["formats"].add(res.format)
            if rel is None:
                unmatched[rec.stem] += 1
                continue
            t["files"].add(rel)
            matched.setdefault(rel, []).append(rec)
            if len(t["preview"]) < 3:
                t["preview"].append({"file": rel, "start": rec.start, "end": rec.end, "speaker": rec.speaker,
                                     "text": rec.text})
    stems = Counter(Path(r).stem for r in scan_.audio)
    return {
        "parsed": parsed,
        "matched": matched,
        "tasks": {k: {"records": v["records"], "files": len(v["files"]), "formats": sorted(v["formats"]),
                      "preview": v["preview"]} for k, v in by_task.items()},
        "unmatched": sorted(unmatched),
        "audio_without_labels": sorted(r for r in scan_.audio if r not in matched),
        "duplicate_stems": sorted(s for s, n in stems.items() if n > 1),
        "parse_errors": [{"file": r.path, "format": r.format, "errors": r.errors[:20], "count": len(r.errors)}
                         for r in parsed if r.errors],
        "files_by_format": [{"file": r.path, "format": r.format, "records": len(r.records),
                             "tasks": sorted(r.tasks)} for r in parsed],
    }


def inspect_source(root: Path) -> dict[str, Any]:
    extract_zips(root)
    s = scan(root)
    m = match(s)
    sizes = sum(p.stat().st_size for p in s.audio.values())
    m.pop("parsed")
    m.pop("matched")
    return {"audio_files": len(s.audio), "audio_bytes": sizes, "annotation_files": len(s.annotations),
            "ignored": s.other[:50], **m}


def label_types_for(records: list[RefRecord]) -> list[str]:
    kinds = {r.kind for r in records}
    out = []
    if kinds & {"asr", "spk_asr"}:
        out.append(LabelType.asr.value)
    if kinds & {"diar", "spk_asr"}:
        out.append(LabelType.diar.value)
    return out


def _process_audio(f: AudioFile, src: Path, max_dur: float) -> dict[str, Any]:
    """Copy the original, decode, check clipping, derive 16 kHz mono and peaks. Returns facts for the report."""
    orig = paths.original(f.sha256, f.ext)
    if not orig.exists():
        orig.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, orig)
    try:
        pr = audio_mod.probe(orig)
        x = audio_mod.decode(orig, pr.sample_rate, pr.channels)
    except audio_mod.DecodeError as exc:
        f.status = FileStatus.undecodable
        return {"error": str(exc).replace(f"{orig}: ", "").replace(str(orig), f.rel_path)}
    f.orig_sr, f.channels = pr.sample_rate, pr.channels
    f.duration = len(x) / pr.sample_rate
    clip = audio_mod.clipped_fraction(x)
    derived = paths.derived(f.sha256)
    if not derived.exists() or not paths.peaks(f.sha256).exists():
        mono = audio_mod.to_mono_16k(x, pr.sample_rate)
        audio_mod.write_flac(derived, mono)
        audio_mod.write_peaks(paths.peaks(f.sha256), audio_mod.compute_peaks(mono))
    if f.duration > max_dur:
        f.status = FileStatus.too_long
    elif clip > audio_mod.CLIP_FRACTION:
        f.status = FileStatus.clipped
    else:
        f.status = FileStatus.ok
    return {"clipped_fraction": clip, "codec": pr.codec}


def ingest(session: Session, dataset_id: int, source: Path, progress: Progress | None = None) -> dict[str, Any]:
    progress = progress or (lambda p, m: None)
    max_dur = get_settings().max_duration_s
    ds = session.get(Dataset, dataset_id)
    if ds is None:
        raise ValueError(f"dataset {dataset_id} not found")
    progress(0.01, "unpacking")
    extract_zips(source)
    s = scan(source)
    m = match(s)
    if not s.audio:
        raise ValueError("no audio files found (accepted: " + ", ".join(sorted(audio_mod.AUDIO_EXTS)) + ")")
    if not m["matched"]:
        raise ValueError(NO_REFERENCES)
    existing = {f.rel_path: f for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id))}
    total = len(s.audio)
    flagged: dict[str, list[dict[str, Any]]] = {"clipped": [], "undecodable": [], "too_long": []}
    sr_hist: Counter[int] = Counter()
    seconds = 0.0
    for k, (rel, src) in enumerate(s.audio.items()):
        progress(0.05 + 0.85 * k / total, f"audio {k + 1}/{total}: {rel}")
        f = existing.get(rel) or AudioFile(dataset_id=ds.id, rel_path=rel, ext=src.suffix.lower())
        f.sha256 = audio_mod.sha256_file(src)
        facts = _process_audio(f, src, max_dur)
        if f.id is None:
            session.add(f)
        session.flush()
        if f.status != FileStatus.ok:
            item = {"file": rel, **({"error": facts["error"]} if "error" in facts else {})}
            if f.status == FileStatus.clipped:
                item["clipped_pct"] = round(100 * facts["clipped_fraction"], 3)
            if f.status == FileStatus.too_long:
                item["duration"] = round(f.duration or 0, 1)
            flagged[f.status].append(item)
        if f.orig_sr:
            sr_hist[f.orig_sr] += 1
        seconds += f.duration or 0.0
        # Reference segments: replace whatever this file had from a previous ingest.
        for seg in list(f.segments):
            session.delete(seg)
        meta: dict[str, Any] = {}
        for rec in m["matched"].get(rel, []):
            if rec.start is None:
                start, end = 0.0, f.duration or 0.0
            else:
                start, end = rec.start, rec.end
            session.add(Segment(audio_file_id=f.id, start=start, end=end, speaker=rec.speaker, text=rec.text,
                                status=SegmentStatus.confirmed))
            meta.update(rec.meta)
        if meta:
            f.meta = {**(f.meta or {}), **meta}
        session.commit()  # short write transactions: the API shares this SQLite file
    progress(0.95, "writing report")
    all_records = [r for recs in m["matched"].values() for r in recs]
    ds.label_types = label_types_for(all_records)
    usable = [f for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id)) if f.usable]
    ds.report = {
        "files": total, "usable_files": len(usable), "hours": seconds / 3600,
        "usable_hours": sum(f.duration or 0 for f in usable) / 3600,
        "sample_rates": {str(k): v for k, v in sorted(sr_hist.items())},
        "flagged": flagged, "tasks": m["tasks"], "unmatched": m["unmatched"],
        "audio_without_labels": m["audio_without_labels"][:200],
        "audio_without_labels_count": len(m["audio_without_labels"]),
        "duplicate_stems": m["duplicate_stems"], "parse_errors": m["parse_errors"],
        "files_by_format": m["files_by_format"], "has_speaker_text": "spk_asr" in m["tasks"],
        "max_duration_s": max_dur,
    }
    ds.status = "ready"
    session.flush()
    progress(1.0, "done")
    return ds.report
