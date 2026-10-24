"""Check the contract samples in contracts/samples against CONTRACTS.md (at the repository root in Concord
Bench, in docs/ in Concord Label).

    python contracts/check.py

Standard library only, so the same file runs unchanged in both apps. It doesn't compare this repository's copy
with the other app's, because the copies are kept identical by hand. When PLATFORM_DIR names the platform
repository, the platform's sample dataset versions (contracts/datasets/samples) are checked as packages too.

`check_package(path)` is also used by both apps' tests on packages and versions they write or read.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
from pathlib import Path, PurePosixPath

CONTRACT_VERSION = 3
FORMAT_VERSIONS = (1, 2, 3)  # reference package format versions a reader takes
AUDIO_ONLY_FROM = 3  # the first format version that allows a package with audio only (empty label_types)
FLAGS_FORMAT_VERSION = 2
CATALOG_INSTALL = "dataset-catalog"  # source.install_id of every Dataset Catalog version
HERE = Path(__file__).resolve().parent
REPO = HERE.parent
REF_FORMATS = {".json", ".jsonl", ".csv", ".txt", ".rttm", ".stm", ".tsv"}
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".opus", ".aac", ".wma", ".webm"}
MANIFEST_REQUIRED = ("format", "format_version", "name", "created_at", "label_types", "counts", "audio_included")
SOURCE_FIELDS = ("app", "install_id", "dataset_uid", "dataset_name")
FLAG_FIELDS = ("uid", "file_sha256", "start", "end", "note")
HEX = set("0123456789abcdef")


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _rel_ok(rel: str) -> bool:
    p = PurePosixPath(rel)
    return bool(rel) and not p.is_absolute() and ".." not in p.parts


def check_package(root: Path) -> list[str]:
    """Problems with a reference package folder (CONTRACTS.md §2); empty when it follows the contract.

    Audio left out of the package cannot be checked here: the receiver must already have it."""
    problems: list[str] = []
    mpath = root / "manifest.json"
    if not mpath.is_file():
        audio = [p for p in root.rglob("*") if p.suffix.lower() in AUDIO_EXTS]
        refs = [p for p in root.rglob("*") if p.suffix.lower() in REF_FORMATS]
        if not audio:
            problems.append("no manifest and no audio: a package without a manifest is an annotated upload")
        if not refs:
            problems.append("no manifest and no annotation file")
        return problems
    try:
        m = json.loads(mpath.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f"manifest.json is not JSON: {exc}"]
    for k in MANIFEST_REQUIRED:
        if k not in m:
            problems.append(f"manifest.json: missing {k}")
    if m.get("format") != "concord-reference-package":
        problems.append(f"manifest.json: format {m.get('format')!r}")
    version = m.get("format_version")
    if version not in FORMAT_VERSIONS:
        problems.append(f"manifest.json: format_version {version!r} (known: {', '.join(map(str, FORMAT_VERSIONS))})")
    label_types = m.get("label_types")
    audio_only = label_types == [] and isinstance(version, int) and version >= AUDIO_ONLY_FROM
    if not isinstance(label_types, list) or not set(label_types) <= {"asr", "diar"} or not (label_types or audio_only):
        problems.append(f"manifest.json: label_types {label_types!r}")
    if m.get("audio_included") not in ("all", "partial", "none"):
        problems.append(f"manifest.json: audio_included {m.get('audio_included')!r}")
    if m.get("scope") not in (None, "complete", "any"):
        problems.append(f"manifest.json: scope {m.get('scope')!r}")
    src = m.get("source")
    if src is not None:
        problems += [f"manifest.json: source.{k} missing" for k in SOURCE_FIELDS if not src.get(k)]
        uid = str(src.get("dataset_uid") or "")
        if src.get("install_id") == CATALOG_INSTALL and not (len(uid) == 32 and set(uid) <= HEX):
            problems.append("manifest.json: source.dataset_uid of a catalog version must be its catalog dataset id")
        if not isinstance(m.get("export_version"), int) or m["export_version"] < 1:
            problems.append("manifest.json: export_version must be 1, 2, ... when source is given")

    fpath = root / "files.csv"
    rows: list[dict[str, str]] = []
    if not fpath.is_file():
        problems.append("files.csv missing")
    else:
        with open(fpath, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != ["path", "sha256", "duration", "sample_rate"]:
                problems.append(f"files.csv: header {reader.fieldnames}")
            rows = list(reader)
    stems: dict[str, str] = {}
    included = 0
    for r in rows:
        rel = r.get("path") or ""
        if not _rel_ok(rel):
            problems.append(f"files.csv: bad path {rel!r}")
            continue
        stem = PurePosixPath(rel).stem.lower()
        if stem in stems:
            problems.append(f"files.csv: stem of {rel} repeats {stems[stem]}")
        stems[stem] = rel
        if len(r.get("sha256") or "") != 64:
            problems.append(f"files.csv: sha256 of {rel}")
        audio = root / "audio" / rel
        if audio.is_file():
            included += 1
            if sha256_file(audio) != r["sha256"]:
                problems.append(f"audio/{rel}: bytes do not match its sha256")
    counts = m.get("counts") or {}
    if rows and counts.get("files") != len(rows):
        problems.append(f"manifest.json: counts.files {counts.get('files')} but files.csv has {len(rows)} rows")
    want = {"all": len(rows), "none": 0}.get(m.get("audio_included"))
    if want is not None and included != want:
        problems.append(f"audio_included is {m.get('audio_included')!r} but {included} of {len(rows)} files are in audio/")
    if m.get("audio_included") == "partial" and not 0 < included < len(rows):
        problems.append("audio_included is 'partial' but audio/ has none or all of the files")

    side = {"uids.jsonl", "attributes.json"} if version != 1 else {"uids.jsonl"}
    in_refs = [p for p in (root / "refs").glob("*") if p.is_file()] if (root / "refs").is_dir() else []
    refs = [p for p in in_refs if p.name not in side]
    if audio_only:
        if in_refs:
            problems.append(f"refs/ must be empty when label_types is empty (found {', '.join(p.name for p in in_refs)})")
        if (m.get("counts") or {}).get("segments") not in (0, None):
            problems.append("manifest.json: counts.segments must be 0 when label_types is empty")
    elif len(refs) != 1:
        problems.append(f"refs/ must hold exactly one references file besides {' and '.join(sorted(side))} "
                        f"(found {len(refs)})")
    attrs = root / "refs" / "attributes.json"
    if version != 1 and attrs.is_file():
        problems += check_attributes(json.loads(attrs.read_text(encoding="utf-8")), set(stems.values()))
    uids = root / "refs" / "uids.jsonl"
    if uids.is_file():
        for n, line in enumerate(uids.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                rec = json.loads(line)
                if not {"file", "start", "end", "uid"} <= rec.keys() or rec["file"] not in stems.values():
                    problems.append(f"refs/uids.jsonl line {n}: needs file (a files.csv path), start, end, uid")
    outs = root / "outputs" / "seg.jsonl"
    if outs.is_file():
        for n, line in enumerate(outs.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                rec = json.loads(line)
                missing = {"model", "image", "sha256", "params", "output"} - rec.keys()
                if missing or "segments" not in (rec.get("output") or {}):
                    problems.append(f"outputs/seg.jsonl line {n}: missing {sorted(missing) or ['output.segments']}")
                bk = rec.get("build_key")
                if bk is not None and not (isinstance(bk, str) and len(bk) >= 16 and all(c in HEX for c in bk)):
                    problems.append(f"outputs/seg.jsonl line {n}: build_key must be the catalog's hex build key")
    return problems


def _labels_ok(obj: object) -> bool:
    """A labels object (CONTRACTS.md §2.3): strings, numbers, true / false, or nested labels objects."""
    return isinstance(obj, dict) and all(isinstance(v, (str, int, float, bool)) or _labels_ok(v) for v in obj.values())


def check_attributes(a: object, paths: set[str]) -> list[str]:
    """Problems with refs/attributes.json (CONTRACTS.md §2.7), given the files.csv paths."""
    if not isinstance(a, dict) or set(a) - {"files", "speakers"}:
        return ["refs/attributes.json: must be an object with files and / or speakers"]
    problems = []
    for path, labels in (a.get("files") or {}).items():
        if path not in paths:
            problems.append(f"refs/attributes.json: files.{path} is not a files.csv path")
        elif not _labels_ok(labels):
            problems.append(f"refs/attributes.json: files.{path} must be an object of labels")
    for path, speakers in (a.get("speakers") or {}).items():
        if path not in paths:
            problems.append(f"refs/attributes.json: speakers.{path} is not a files.csv path")
        elif not isinstance(speakers, dict) or not all(_labels_ok(v) for v in speakers.values()):
            problems.append(f"refs/attributes.json: speakers.{path} must map each speaker to an object of labels")
    return problems


def check_flags(path: Path) -> list[str]:
    """Problems with a flags file (CONTRACTS.md §3), format version 2."""
    f = json.loads(path.read_text(encoding="utf-8"))
    problems = []
    if f.get("format") != "concord-flags" or f.get("format_version") != FLAGS_FORMAT_VERSION:
        problems.append(f"{path.name}: format/format_version")
    cid = f.get("catalog_dataset_id")
    if not (isinstance(cid, str) and len(cid) == 32 and set(cid) <= HEX):
        problems.append(f"{path.name}: catalog_dataset_id must be a catalog dataset id (32 hex characters)")
    n = f.get("catalog_version")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        problems.append(f"{path.name}: catalog_version must be 1, 2, ...")
    if "dataset_name" not in f:
        problems.append(f"{path.name}: dataset_name missing")
    for n, flag in enumerate(f.get("flags") or [], 1):
        if set(FLAG_FIELDS) - flag.keys():
            problems.append(f"{path.name}: flag {n} needs {', '.join(FLAG_FIELDS)}")
    return problems


def check_samples() -> list[str]:
    s = HERE / "samples"
    problems = []
    for pkg in sorted(s.glob("reference-package-*")):
        problems += [f"{pkg.name}: {p}" for p in check_package(pkg)]
    problems += check_flags(s / "flags.json")
    for v in platform_versions():
        problems += [f"platform {v.name}: {p}" for p in check_package(v)]
    for f in sorted((s / "api").glob("*.json")):
        try:
            body = json.loads(f.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"api/{f.name}: {exc}")
            continue
        if f.name.startswith("config.") and body.get("contract_version") != CONTRACT_VERSION:
            problems.append(f"api/{f.name}: contract_version {body.get('contract_version')}")
    text = contracts_doc(REPO).read_text(encoding="utf-8")
    if f"**Contract version {CONTRACT_VERSION}.**" not in text:
        problems.append(f"CONTRACTS.md does not say contract version {CONTRACT_VERSION}")
    return problems


def platform_versions() -> list[Path]:
    """The platform's sample dataset versions (contracts/datasets/samples) when PLATFORM_DIR names the platform."""
    platform = os.environ.get("PLATFORM_DIR")
    samples = Path(platform).expanduser() / "contracts" / "datasets" / "samples" if platform else None
    return sorted(p for p in samples.iterdir() if p.is_dir()) if samples and samples.is_dir() else []


def contracts_doc(root: Path) -> Path:
    """A repository's CONTRACTS.md: in docs/ where the repository keeps its docs there, else at the root."""
    docs = root / "docs" / "CONTRACTS.md"
    return docs if docs.is_file() else root / "CONTRACTS.md"


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)
    problems = check_samples()
    for p in problems:
        print(f"FAIL {p}")
    if not problems:
        extra = f", and {len(platform_versions())} platform versions" if platform_versions() else ""
        print(f"contracts v{CONTRACT_VERSION}: samples ok{extra}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
