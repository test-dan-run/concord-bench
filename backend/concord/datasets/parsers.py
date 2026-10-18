"""Annotation parsers, matched to audio by file stem.

| Task | Formats |
|---|---|
| ASR | JSONL (`audio_filepath`, `text`), NeMo manifest, CSV (`audio_filepath`/`file`, `text`), one `.txt` per file |
| Diarisation | RTTM, JSON segments (`file`, `start`, `end`, `speaker`), NeMo diarisation manifest (`rttm_filepath`) |
| Speaker-attributed transcripts (cpWER, Q7) | JSON/JSONL segments with `text`, STM, SegLST, NeMo diarisation output JSON |

NeMo manifests are JSON lines, often saved as `.json`. A NeMo diarisation manifest carries no labels of
its own: its `rttm_filepath` names an RTTM that must be in the upload too (matched by file name).

Every parser yields `RefRecord`s. What a record feeds follows from its fields: text -> ASR,
speaker -> diarisation, both -> cpWER as well. Records without times cover the whole file.

The registry is keyed by format id, and each format lists the tasks it can feed, so a KWS format is
one more entry (Q6).
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

ANNOTATION_EXTS = {".jsonl", ".json", ".csv", ".txt", ".rttm", ".stm", ".tsv"}


@dataclass
class RefRecord:
    stem: str
    start: float | None = None
    end: float | None = None
    speaker: str | None = None
    text: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        if self.speaker is not None and self.text is not None:
            return "spk_asr"
        if self.speaker is not None:
            return "diar"
        return "asr"


@dataclass
class ParseResult:
    path: str
    format: str
    records: list[RefRecord] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)  # file names this file points to (NeMo `rttm_filepath`)

    @property
    def tasks(self) -> set[str]:
        return {r.kind for r in self.records}


def stem_of(path_like: str) -> str:
    name = PurePosixPath(str(path_like).replace("\\", "/")).name
    return name.rsplit(".", 1)[0] if "." in name else name


def _float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    return float(v)


_FILE_KEYS = ("audio_filepath", "file", "filename", "path", "audio", "session_id", "recording_id", "uri")
_TEXT_KEYS = ("text", "transcript", "words", "sentence")
_SPK_KEYS = ("speaker", "speaker_id", "spk")
# NeMo manifest fields that are not slice metadata (`pred_text` is a model's output, not a reference).
_NEMO_KEYS = {"label", "num_speakers", "rttm_filepath", "uem_filepath", "ctm_filepath", "pred_text"}
_KNOWN = set(_FILE_KEYS) | set(_TEXT_KEYS) | set(_SPK_KEYS) | _NEMO_KEYS | {"start", "end", "start_time", "end_time",
                                                                           "offset", "duration"}


def _record_from_obj(obj: dict[str, Any], default_stem: str | None) -> RefRecord:
    file_key = next((k for k in _FILE_KEYS if obj.get(k) not in (None, "")), None)
    stem = stem_of(obj[file_key]) if file_key else default_stem
    if not stem:
        raise ValueError(f"no file field (one of {', '.join(_FILE_KEYS)})")
    text_key = next((k for k in _TEXT_KEYS if k in obj), None)
    text = obj.get(text_key) if text_key else None
    if isinstance(text, list):
        text = " ".join(map(str, text))
    spk_key = next((k for k in _SPK_KEYS if obj.get(k) not in (None, "")), None)
    start = _float(obj.get("start", obj.get("start_time", obj.get("offset"))))
    end = _float(obj.get("end", obj.get("end_time")))
    if end is None and start is not None and obj.get("duration") not in (None, ""):
        end = start + float(obj["duration"])
    if end is None and start == 0 and "offset" in obj:  # NeMo: offset 0 with a null duration is the whole file
        start = None
    if (start is None) != (end is None):
        raise ValueError("needs both start and end (or offset and duration)")
    if start is not None and end is not None and end < start:
        raise ValueError(f"end {end} before start {start}")
    if text is None and spk_key is None:
        raise ValueError("needs text and/or speaker")
    meta = {k: v for k, v in obj.items() if k not in _KNOWN and isinstance(v, (str, int, float, bool))}
    return RefRecord(stem, start, end, str(obj[spk_key]) if spk_key else None,
                     str(text) if text is not None else None, meta)


def parse_jsonl(path: Path, text: str) -> ParseResult:
    res = ParseResult(str(path), "jsonl")
    seglst = nemo = False
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("line is not a JSON object")
            seglst = seglst or ("session_id" in obj and "words" in obj)
            nemo = nemo or ("audio_filepath" in obj and "duration" in obj) or "rttm_filepath" in obj
            if obj.get("rttm_filepath"):
                res.linked.append(PurePosixPath(str(obj["rttm_filepath"]).replace("\\", "/")).name)
                if obj.get("text") in (None, "", "-"):  # "-" is NeMo's placeholder; the labels are in the RTTM
                    continue
            res.records.append(_record_from_obj(obj, None))
        except (ValueError, TypeError) as exc:
            res.errors.append(f"line {n}: {exc}")
    if seglst:
        res.format = "seglst"
    elif nemo:
        res.format = "nemo-manifest"
    elif any(r.speaker is not None for r in res.records):
        res.format = "json-segments"
    return res


def _first_line_is_object(text: str) -> bool:
    line = next((ln for ln in text.splitlines() if ln.strip()), "")
    try:
        return isinstance(json.loads(line), dict)
    except ValueError:
        return False


def _words_to_turns(words: list[Any]) -> list[Any]:
    """NeMo `words` ({word, start_time, end_time, speaker}) merged into turns of consecutive same-speaker words."""
    turns: list[dict[str, Any]] = []
    for w in words:
        if not isinstance(w, dict):
            turns.append(w)  # reported by the caller
            continue
        last = turns[-1] if turns and isinstance(turns[-1], dict) else None
        if last is not None and last["speaker"] == w.get("speaker"):
            last["end_time"] = w.get("end_time")
            last["text"] += " " + str(w.get("word", ""))
        else:
            turns.append({"speaker": w.get("speaker"), "start_time": w.get("start_time"),
                          "end_time": w.get("end_time"), "text": str(w.get("word", ""))})
    return turns


def parse_json(path: Path, text: str) -> ParseResult:
    res = ParseResult(str(path), "json-segments")
    try:
        data = json.loads(text)
    except ValueError as exc:
        if _first_line_is_object(text):  # JSON lines saved as .json, the usual NeMo manifest name
            return parse_jsonl(path, text)
        res.errors.append(f"invalid JSON: {exc}")
        return res
    default = None
    if isinstance(data, dict) and "segments" not in data and "audio_filepath" in data and (
            "duration" in data or "rttm_filepath" in data):
        return parse_jsonl(path, json.dumps(data))  # a one-line NeMo manifest
    if isinstance(data, dict):
        default = stem_of(data.get("session_id") or data.get("file") or data.get("audio_filepath") or path.name)
        items = data.get("segments")
        # NeMo diarisation output (OfflineDiarWithASR): `sentences`, or only `words`, each with a speaker.
        if items is None and isinstance(data.get("sentences", data.get("words")), list) and (
                "speaker_count" in data or "transcription" in data):
            res.format = "nemo-diar"
            items = data.get("sentences") or _words_to_turns(data["words"])
        if items is None:
            items = [data]
    else:
        items = data
    if not isinstance(items, list):
        res.errors.append("expected a list of segments or {\"segments\": [...]}")
        return res
    for n, obj in enumerate(items):
        try:
            if not isinstance(obj, dict):
                raise ValueError("segment is not an object")
            if "session_id" in obj and "words" in obj:
                res.format = "seglst"
            res.records.append(_record_from_obj(obj, default))
        except (ValueError, TypeError) as exc:
            res.errors.append(f"item {n}: {exc}")
    return res


def parse_csv(path: Path, text: str) -> ParseResult:
    res = ParseResult(str(path), "csv")
    dialect = "excel-tab" if path.suffix.lower() == ".tsv" else "excel"
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    for n, row in enumerate(reader, 2):
        try:
            res.records.append(_record_from_obj({k.strip(): v for k, v in row.items() if k}, None))
        except (ValueError, TypeError) as exc:
            res.errors.append(f"row {n}: {exc}")
    if any(r.speaker is not None for r in res.records):
        res.format = "csv-segments"
    return res


def parse_txt(path: Path, text: str) -> ParseResult:
    res = ParseResult(str(path), "txt")
    res.records.append(RefRecord(stem_of(path.name), text=" ".join(text.split())))
    return res


def parse_rttm(path: Path, text: str) -> ParseResult:
    res = ParseResult(str(path), "rttm")
    for n, line in enumerate(text.splitlines(), 1):
        parts = line.split()
        if not parts or parts[0].startswith((";", "#")):
            continue
        if parts[0] != "SPEAKER":
            continue
        try:
            start, dur = float(parts[3]), float(parts[4])
            if dur < 0:
                raise ValueError("negative duration")
            res.records.append(RefRecord(parts[1], start, start + dur, parts[7]))
        except (IndexError, ValueError) as exc:
            res.errors.append(f"line {n}: {exc}")
    return res


def parse_stm(path: Path, text: str) -> ParseResult:
    """`file channel speaker start end [<label>] transcript`; `;;` lines are comments."""
    res = ParseResult(str(path), "stm")
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith(";;"):
            continue
        parts = line.split(maxsplit=5)
        try:
            if len(parts) < 5:
                raise ValueError("needs file channel speaker start end transcript")
            words = parts[5] if len(parts) > 5 else ""
            meta: dict[str, Any] = {}
            if words.startswith("<"):
                label, _, words = words.partition(">")
                # NIST labels look like <o,f0,male>; the last field is the condition worth slicing on.
                meta["condition"] = label[1:].split(",")[-1].strip()
            start, end = float(parts[3]), float(parts[4])
            if end < start:
                raise ValueError(f"end {end} before start {start}")
            if parts[2].lower() in ("inter_segment_gap", "excluded_region"):
                continue
            res.records.append(RefRecord(stem_of(parts[0]), start, end, parts[2], words.strip(), meta))
        except (IndexError, ValueError) as exc:
            res.errors.append(f"line {n}: {exc}")
    return res


Parser = Callable[[Path, str], ParseResult]
PARSERS: dict[str, Parser] = {
    ".jsonl": parse_jsonl, ".json": parse_json, ".csv": parse_csv, ".tsv": parse_csv,
    ".txt": parse_txt, ".rttm": parse_rttm, ".stm": parse_stm,
}

# Format id -> tasks it can feed and a short description (for the upload page and templates).
FORMATS: dict[str, dict[str, Any]] = {
    "jsonl": {"tasks": ["asr"], "label": "JSONL (audio_filepath, text)"},
    "csv": {"tasks": ["asr"], "label": "CSV (audio_filepath, text)"},
    "txt": {"tasks": ["asr"], "label": "One .txt per audio file"},
    "rttm": {"tasks": ["diar"], "label": "RTTM"},
    "nemo-manifest": {"tasks": ["asr", "diar"],
                      "label": "NeMo manifest (audio_filepath, text, duration[, offset]; rttm_filepath for speakers)"},
    "json-segments": {"tasks": ["diar", "asr"], "label": "JSON/JSONL segments (file, start, end, speaker[, text])"},
    "csv-segments": {"tasks": ["diar", "asr"], "label": "CSV segments (file, start, end, speaker[, text])"},
    "stm": {"tasks": ["diar", "asr"], "label": "STM"},
    "seglst": {"tasks": ["diar", "asr"], "label": "SegLST"},
    "nemo-diar": {"tasks": ["diar", "asr"], "label": "NeMo diarisation output JSON (sentences or words with speaker)"},
}


def parse_file(path: Path, rel: str | None = None) -> ParseResult:
    parser = PARSERS.get(path.suffix.lower())
    if parser is None:
        return ParseResult(rel or str(path), "unknown", errors=["unsupported annotation format"])
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return ParseResult(rel or str(path), "unknown", errors=["not UTF-8 text"])
    res = parser(path, text)
    res.path = rel or str(path)
    return res
