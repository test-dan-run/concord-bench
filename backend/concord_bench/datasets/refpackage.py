"""Reference packages (CONTRACTS.md §2, SPLIT_PLAN.md §4.3): received from Concord Label, another tool or
a person, staged with a preview, then accepted into a new dataset through the normal ingest job.

- `find_root` / `read_package` locate and validate a package in an upload (a folder, or a `.zip` whose
  root or only top-level directory is the package). An unknown `format_version` is refused with the
  versions this app reads.
- `preview` is what the import page shows before anything is stored.
- `accept` builds the ingest source from `audio/` (or Bench's own originals, by sha256) plus the one
  references file, so the manifest, `files.csv`, `uids.jsonl` and `outputs/` are never read as
  annotations. A package without a manifest is ingested as it is, like an annotated upload.
- `finish` runs after the ingest: provenance, Label's segment uids (for flags back, §3), the file and speaker
  labels of `refs/attributes.json` (format version 2, §2.7), and seg-model outputs imported into the output
  cache when they come from the build the model catalog runs now (`build_key`, §2.5), recorded in the
  catalog's output index too. Labels on the records themselves (§2.3) come in with the ingest.

References that have been scored are never changed: a package from the same source as an earlier
dataset (`source.install_id` + `source.dataset_uid`) becomes a new dataset, `<name> v<N>`, linked to
the earlier versions.

A Dataset Catalog version (DATASET_CATALOG_PLAN.md §9.2) is staged straight from its folder on the store's
mount (`datasets.catalog`), with its audio found by sha256 in the store. The catalog fills in its `source`
(install id `dataset-catalog`, the catalog dataset id) and `export_version` (the version number), so the
Bench datasets made from one catalog dataset form one chain, together with the ones published as its
versions (`Dataset.catalog_dataset_id`).
"""

from __future__ import annotations

import contextlib
import csv
import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..core import paths
from ..core.models import AudioFile, Dataset, Job, ModelOutput, ReferenceImport, Segment, utcnow
from ..platform import dataset_catalog
from . import audio as audio_mod
from . import labels as labels_mod
from .ingest import NO_REFERENCES, SKIP_DIRS, extract_zips, inspect_source
from .parsers import ANNOTATION_EXTS, flatten, parse_file

FORMAT = "concord-reference-package"
KNOWN_FORMAT_VERSIONS = (1, 2)
# A catalog version may be format version 3 (contracts/datasets/version.md in the platform): version 2 that
# also allows audio only, which Bench can't import (NO_REFERENCES). Zips stay at CONTRACTS.md's versions.
CATALOG_FORMAT_VERSIONS = (1, 2, 3)
ATTRIBUTES = "attributes.json"  # refs/attributes.json, format version 2 (CONTRACTS.md §2.7)
SPEAKER_PREFIX = "speaker."
MANIFEST_REQUIRED = ("format", "format_version", "name", "created_at", "label_types", "counts", "audio_included")
SOURCE_FIELDS = ("app", "install_id", "dataset_uid", "dataset_name")
FILES_HEADER = ["path", "sha256", "duration", "sample_rate"]
# Where the references file goes in the ingest source, beside the audio at its dataset paths.
REFS_DIR = "_references"
UID_TOLERANCE = 0.01  # CONTRACTS.md §2.4
BENCH_SAMPLE_RATE = 16000  # every model call sends 16 kHz mono


class PackageError(ValueError):
    """The package does not follow the contract; the message is shown to the sender."""


@dataclass
class Package:
    root: Path
    manifest: dict[str, Any] | None
    files: list[dict[str, Any]] = field(default_factory=list)  # files.csv rows
    refs: Path | None = None
    uids: list[dict[str, Any]] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    outputs_invalid: int = 0
    attributes: dict[str, Any] = field(default_factory=dict)  # refs/attributes.json

    @property
    def source(self) -> dict[str, Any] | None:
        return (self.manifest or {}).get("source") or None

    @property
    def export_version(self) -> int | None:
        return (self.manifest or {}).get("export_version") if self.source else None

    @property
    def name(self) -> str:
        if self.manifest:
            return str(self.manifest["name"])
        return self.root.name.replace("_", " ").replace("-", " ").strip() or "Reference package"

    def audio_path(self, row: dict[str, Any]) -> Path:
        return self.root / "audio" / row["path"]


def source_key(source: dict[str, Any] | None) -> str | None:
    if not source or not source.get("install_id") or not source.get("dataset_uid"):
        return None
    return f"{source['install_id']}/{source['dataset_uid']}"


# ---------------------------------------------------------------- reading

def _visible(p: Path) -> bool:
    return not p.name.startswith(".") and p.name not in SKIP_DIRS


def find_root(upload: Path) -> Path:
    """The package folder in an upload: zips are unpacked, then single top-level folders are entered
    until one holds `manifest.json` (or holds more than one thing)."""
    try:
        extract_zips(upload)
    except (ValueError, OSError, zipfile.BadZipFile) as exc:
        raise PackageError(f"cannot unpack the upload: {exc}") from None
    d = upload
    while not (d / "manifest.json").is_file():
        entries = [p for p in d.iterdir() if _visible(p)]
        dirs = [p for p in entries if p.is_dir()]
        files = [p for p in entries if p.is_file() and p.suffix.lower() != ".zip"]
        if len(dirs) == 1 and not files:
            d = dirs[0]
            continue
        break
    return d


def _rel_ok(rel: str) -> bool:
    p = PurePosixPath(rel)
    return bool(rel) and not p.is_absolute() and ".." not in p.parts and "\\" not in rel


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    good, bad = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if isinstance(rec, dict):
            good.append(rec)
        else:
            bad += 1
    return good, bad


def read_manifest(root: Path, formats: tuple[int, ...] = KNOWN_FORMAT_VERSIONS) -> dict[str, Any]:
    try:
        m = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PackageError(f"manifest.json is not JSON: {exc}") from None
    if not isinstance(m, dict) or m.get("format") != FORMAT:
        raise PackageError(f"manifest.json: format must be {FORMAT!r} (got {m.get('format') if isinstance(m, dict) else m!r})")
    v = m.get("format_version")
    if v not in formats:
        known = ", ".join(str(k) for k in formats)
        raise PackageError(f"reference package format_version {v!r} is not supported: this Concord Bench reads "
                           f"format version {known}")
    return m


def read_package(root: Path, *, check_audio: bool = True, formats: tuple[int, ...] = KNOWN_FORMAT_VERSIONS) -> Package:
    """Validate a package folder (CONTRACTS.md §2). Raises PackageError listing what is wrong."""
    if not (root / "manifest.json").is_file():
        try:
            found = inspect_source(root)
        except ValueError as exc:
            raise PackageError(str(exc)) from None
        if not found["audio_files"]:
            raise PackageError("no manifest.json and no audio: a package without a manifest is read like an "
                               "annotated upload, so it needs the audio files and an annotation file")
        if len(found["audio_without_labels"]) == found["audio_files"]:
            raise PackageError(NO_REFERENCES)
        return Package(root, None)

    m = read_manifest(root, formats)
    problems: list[str] = [f"manifest.json: missing {k}" for k in MANIFEST_REQUIRED if k not in m]
    lt = m.get("label_types")
    if not isinstance(lt, list) or not lt or not set(lt) <= {"asr", "diar"}:
        problems.append(f"manifest.json: label_types must be a non-empty subset of asr, diar (got {lt!r})")
    if m.get("audio_included") not in ("all", "partial", "none"):
        problems.append(f"manifest.json: audio_included {m.get('audio_included')!r}")
    if m.get("scope") not in (None, "complete", "any"):
        problems.append(f"manifest.json: scope {m.get('scope')!r}")
    src = m.get("source")
    if src is not None:
        if not isinstance(src, dict):
            problems.append("manifest.json: source must be an object")
        else:
            problems += [f"manifest.json: source.{k} missing" for k in SOURCE_FIELDS if not src.get(k)]
        ev = m.get("export_version")
        if not isinstance(ev, int) or isinstance(ev, bool) or ev < 1:
            problems.append("manifest.json: export_version must be 1, 2, ... when source is given")

    pkg = Package(root, m)
    fpath = root / "files.csv"
    if not fpath.is_file():
        problems.append("files.csv missing (required with a manifest)")
    else:
        with open(fpath, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames != FILES_HEADER:
                problems.append(f"files.csv: header must be {','.join(FILES_HEADER)} (got {reader.fieldnames})")
            rows = list(reader)
        stems: dict[str, str] = {}
        for r in rows:
            rel = (r.get("path") or "").strip()
            if not _rel_ok(rel):
                problems.append(f"files.csv: bad path {rel!r}")
                continue
            stem = PurePosixPath(rel).stem.lower()
            if stem in stems:
                problems.append(f"files.csv: the stem of {rel} repeats {stems[stem]}")
            stems[stem] = rel
            sha = (r.get("sha256") or "").strip().lower()
            if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                problems.append(f"files.csv: sha256 of {rel} is not a hex SHA-256")
            try:
                duration = float(r["duration"]) if r.get("duration") not in (None, "") else None
                sr = int(float(r["sample_rate"])) if r.get("sample_rate") not in (None, "") else None
            except ValueError:
                problems.append(f"files.csv: duration / sample_rate of {rel} are not numbers")
                duration = sr = None
            pkg.files.append({"path": rel, "sha256": sha, "duration": duration, "sample_rate": sr})
        if not pkg.files and not problems:
            problems.append("files.csv lists no files")

    refs_dir = root / "refs"
    side = {"uids.jsonl", ATTRIBUTES} if m.get("format_version") != 1 else {"uids.jsonl"}
    refs = sorted(p for p in refs_dir.iterdir() if p.is_file() and p.name not in side and _visible(p)) \
        if refs_dir.is_dir() else []
    if len(refs) != 1:
        problems.append(f"refs/ must hold exactly one references file besides {' and '.join(sorted(side))} "
                        f"(found {len(refs)})")
    elif refs[0].suffix.lower() not in ANNOTATION_EXTS:
        problems.append(f"refs/{refs[0].name}: not a references format Bench reads "
                        f"({', '.join(sorted(ANNOTATION_EXTS))})")
    else:
        pkg.refs = refs[0]
    if (refs_dir / "uids.jsonl").is_file():
        uids, bad = _read_jsonl(refs_dir / "uids.jsonl")
        known = {f["path"] for f in pkg.files}
        for n, rec in enumerate(uids, 1):
            if not {"file", "start", "end", "uid"} <= rec.keys() or rec["file"] not in known:
                problems.append(f"refs/uids.jsonl record {n}: needs file (a files.csv path), start, end, uid")
                break
        if bad:
            problems.append(f"refs/uids.jsonl: {bad} line(s) are not JSON objects")
        pkg.uids = uids
    if ATTRIBUTES in side and (refs_dir / ATTRIBUTES).is_file():
        try:
            pkg.attributes = json.loads((refs_dir / ATTRIBUTES).read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            problems.append(f"refs/{ATTRIBUTES} is not JSON: {exc}")
        else:
            problems += attribute_problems(pkg.attributes, {f["path"] for f in pkg.files})
    if (root / "outputs" / "seg.jsonl").is_file():
        pkg.outputs, pkg.outputs_invalid = _read_jsonl(root / "outputs" / "seg.jsonl")

    if problems:
        raise PackageError("; ".join(problems[:12]) + (f" (and {len(problems) - 12} more)" if len(problems) > 12 else ""))
    if check_audio:
        bad_audio = [f"audio/{f['path']}" for f in pkg.files
                     if pkg.audio_path(f).is_file() and audio_mod.sha256_file(pkg.audio_path(f)) != f["sha256"]]
        if bad_audio:
            raise PackageError(f"bytes do not match their sha256 in files.csv: {', '.join(bad_audio[:20])}")
    return pkg


def _labels_ok(obj: Any) -> bool:
    """A labels object (CONTRACTS.md §2.3): strings, numbers, true / false, or nested labels objects."""
    return isinstance(obj, dict) and all(isinstance(v, (str, int, float, bool)) or _labels_ok(v) for v in obj.values())


def attribute_problems(a: Any, paths_: set[str]) -> list[str]:
    """What is wrong with refs/attributes.json (CONTRACTS.md §2.7), given the files.csv paths."""
    if not isinstance(a, dict) or set(a) - {"files", "speakers"}:
        return [f"refs/{ATTRIBUTES}: must be an object with files and / or speakers"]
    problems = []
    for path, labels in (a.get("files") or {}).items():
        if path not in paths_:
            problems.append(f"refs/{ATTRIBUTES}: files.{path} is not a files.csv path")
        elif not _labels_ok(labels):
            problems.append(f"refs/{ATTRIBUTES}: files.{path} must be an object of labels")
    for path, speakers in (a.get("speakers") or {}).items():
        if path not in paths_:
            problems.append(f"refs/{ATTRIBUTES}: speakers.{path} is not a files.csv path")
        elif not isinstance(speakers, dict) or not all(_labels_ok(v) for v in speakers.values()):
            problems.append(f"refs/{ATTRIBUTES}: speakers.{path} must map each speaker to an object of labels")
    return problems


def attribute_names(a: dict[str, Any]) -> list[str]:
    """The label names refs/attributes.json gives, speaker labels with their prefix."""
    names = {k for labels in (a.get("files") or {}).values() for k in flatten(labels)}
    names |= {SPEAKER_PREFIX + k for spk in (a.get("speakers") or {}).values() for labels in spk.values()
              for k in flatten(labels)}
    return sorted(names)


# ---------------------------------------------------------------- what Bench already has

def stored_original(session: Session, sha256: str, ext_hint: str | None = None) -> Path | None:
    """The original with this sha256 in the Dataset Catalog's store, else Bench's own, whichever dataset
    brought it."""
    in_store = paths.store_audio(sha256)
    if in_store is not None:
        return in_store
    exts = [ext_hint] if ext_hint else []
    exts += list(session.scalars(select(AudioFile.ext).where(AudioFile.sha256 == sha256).distinct()))
    for ext in dict.fromkeys(e for e in exts if e):
        p = paths.original(sha256, ext)
        if p.is_file():
            return p
    return None


def missing_originals(session: Session, sha256s: list[str]) -> list[str]:
    """The sha256s Bench has no stored original for (POST /api/fingerprints/missing)."""
    out = []
    for sha in dict.fromkeys(s.strip().lower() for s in sha256s):
        if stored_original(session, sha) is None:
            out.append(sha)
    return out


def missing_files(session: Session, pkg: Package) -> list[str]:
    """files.csv paths neither in audio/ nor already stored by Bench (CONTRACTS.md §2.2)."""
    return [f["path"] for f in pkg.files if not pkg.audio_path(f).is_file()
            and stored_original(session, f["sha256"], PurePosixPath(f["path"]).suffix.lower()) is None]


# ---------------------------------------------------------------- versions

def version_number(d: Dataset, key: str | None) -> int:
    """The dataset's version in the chain of `key`: the catalog version for one published as a version of that
    catalog dataset, else its export_version."""
    uid = dataset_catalog.catalog_uid(key)
    if uid and d.catalog_dataset_id == uid:
        return d.catalog_version or 0
    return d.export_version or 0


def chain_key(ds: Dataset) -> str | None:
    """The source key of the dataset's version chain: its source, else the catalog dataset it was published to."""
    if ds.source_key:
        return ds.source_key
    return dataset_catalog.SOURCE_PREFIX + ds.catalog_dataset_id if ds.catalog_dataset_id else None


def versions_of(session: Session, key: str | None, *, failed: bool = False) -> list[Dataset]:
    """Datasets from one source, oldest version first (failed ingests left out unless asked for). For a catalog
    dataset, the Bench datasets published as its versions are in the chain too."""
    if not key:
        return []
    uid = dataset_catalog.catalog_uid(key)
    stmt = select(Dataset).where(or_(Dataset.source_key == key, Dataset.catalog_dataset_id == uid) if uid
                                 else Dataset.source_key == key)
    if not failed:
        stmt = stmt.where(Dataset.status != "failed")
    return sorted(session.scalars(stmt).all(), key=lambda d: (version_number(d, key), d.id))


def previous_version(session: Session, pkg: Package) -> Dataset | None:
    """The newest earlier version of the package's dataset (below its export_version when there is one)."""
    key = source_key(pkg.source)
    vs = versions_of(session, key)
    ev = pkg.export_version
    earlier = [d for d in vs if ev is None or version_number(d, key) < ev] or vs
    return earlier[-1] if earlier else None


def dataset_name_for(session: Session, pkg: Package) -> str:
    """`<name>` for the first dataset from a source, `<name> v<export_version>` for the later ones."""
    if pkg.source and pkg.export_version and versions_of(session, source_key(pkg.source)):
        return f"{pkg.name} v{pkg.export_version}"
    return pkg.name


# ---------------------------------------------------------------- preview

def _count_refs(pkg: Package) -> tuple[int, list[str], list[str]]:
    """(reference records, label types they feed, names of the labels on them)."""
    if pkg.refs is None:
        found = inspect_source(pkg.root)
        tasks = found["tasks"]
        records = sum(t["records"] for t in tasks.values())
        names = [lb["key"] for lb in found["labels"]]
    else:
        res = parse_file(pkg.refs, f"refs/{pkg.refs.name}")
        if res.errors and not res.records:
            raise PackageError(f"refs/{pkg.refs.name}: " + "; ".join(res.errors[:5]))
        stems = {PurePosixPath(f["path"]).stem.lower() for f in pkg.files}
        recs = [r for r in res.records if r.stem.lower() in stems]
        if not recs:
            raise PackageError(f"refs/{pkg.refs.name}: no reference matches a file in files.csv (matched by stem)")
        tasks = {r.kind for r in recs}
        records = len(recs)
        names = sorted({k for r in recs for k in labels_mod.of(r.meta)})
    kinds = set(tasks)
    labels = [t for t, need in (("asr", {"asr", "spk_asr"}), ("diar", {"diar", "spk_asr"})) if kinds & need]
    return records, labels, names


def preview(session: Session, pkg: Package) -> dict[str, Any]:
    """What the import page shows (CONTRACTS.md §4 sample `bench.reference-imports.response.json`)."""
    m = pkg.manifest or {}
    segments, found_labels, record_labels = _count_refs(pkg)
    if pkg.manifest is not None:
        shas = [f["sha256"] for f in pkg.files]
        n_files = len(pkg.files)
        hours = (m.get("counts") or {}).get("hours")
        if hours is None:
            hours = sum(f["duration"] or 0 for f in pkg.files) / 3600
        label_types = list(m.get("label_types") or found_labels)
    else:
        audio = [p for p in pkg.root.rglob("*") if p.is_file() and p.suffix.lower() in audio_mod.AUDIO_EXTS
                 and all(_visible(Path(x)) for x in p.relative_to(pkg.root).parts)]
        shas = [audio_mod.sha256_file(p) for p in audio]
        n_files = len(audio)
        seconds = 0.0
        for p in audio:
            with contextlib.suppress(audio_mod.DecodeError, ValueError):  # reported at ingest
                seconds += audio_mod.probe(p).duration or 0.0
        hours = seconds / 3600
        label_types = found_labels
    prev = previous_version(session, pkg)
    out: dict[str, Any] = {
        "name": pkg.name,
        "source": pkg.source,
        "export_version": pkg.export_version,
        "files": n_files,
        "new_files": len(missing_originals(session, shas)),
        "hours": round(float(hours), 6),
        "segments": segments,
        "label_types": label_types,
        "previous_version": None,
        # Beyond the contract's sample: what the page also shows.
        "has_manifest": pkg.manifest is not None,
        "dataset_name": dataset_name_for(session, pkg),
        "scope": m.get("scope"), "language": m.get("language"), "description": m.get("description"),
        "tags": list(m.get("tags") or []), "created_at": m.get("created_at"), "review": m.get("review"),
        "models": m.get("models"), "normaliser": m.get("normaliser"), "audio_included": m.get("audio_included"),
        "refs_file": f"refs/{pkg.refs.name}" if pkg.refs else None, "uids": len(pkg.uids),
        "labels": sorted(set(record_labels) | set(attribute_names(pkg.attributes))),
        "outputs": len(pkg.outputs), "missing_files": missing_files(session, pkg) if pkg.manifest else [],
    }
    if prev is not None:
        prev_shas = set(session.scalars(select(AudioFile.sha256).where(AudioFile.dataset_id == prev.id)))
        prev_segments = session.scalar(select(func.count(Segment.id)).join(AudioFile)
                                       .where(AudioFile.dataset_id == prev.id))
        mine = set(shas)
        out["previous_version"] = {"dataset_id": prev.id, "name": prev.name, "export_version": prev.export_version}
        out["changes"] = {"files_added": len(mine - prev_shas), "files_removed": len(prev_shas - mine),
                          "segments_before": prev_segments or 0, "segments_now": segments}
    return out


# ---------------------------------------------------------------- staging

def stage(session: Session, upload_id: str | None, upload_dir: Path, *,
          catalog: dict[str, Any] | None = None) -> ReferenceImport:
    """Stage an uploaded package, or (with `catalog`: {"dataset_id", "n", "dataset_name"}) a catalog version's
    folder on the store's mount, which is the package root as it is."""
    root = upload_dir if catalog else find_root(upload_dir)
    pkg = read_package(root, formats=CATALOG_FORMAT_VERSIONS if catalog else KNOWN_FORMAT_VERSIONS)
    prev = preview(session, pkg)
    if prev["missing_files"]:
        raise PackageError(_missing_message(prev["missing_files"]))
    if catalog:
        prev["catalog"] = catalog
    imp = ReferenceImport(status="staged", name=pkg.name[:200], upload_id=upload_id, path=str(root),
                          manifest=pkg.manifest, preview=prev)
    session.add(imp)
    session.flush()
    return imp


def catalog_of(imp: ReferenceImport) -> dict[str, Any] | None:
    """{"dataset_id", "n", "dataset_name"} of an import staged from the Dataset Catalog, else None."""
    return (imp.preview or {}).get("catalog") or None


def _formats(imp: ReferenceImport) -> tuple[int, ...]:
    return CATALOG_FORMAT_VERSIONS if catalog_of(imp) else KNOWN_FORMAT_VERSIONS


def _missing_message(missing: list[str]) -> str:
    where = "in the Dataset Catalog's store" if dataset_catalog.enabled() else "in Concord Bench"
    return (f"{len(missing)} file(s) are listed in files.csv but are neither in audio/ nor already stored {where}: "
            f"{', '.join(missing[:20])}" + (" ..." if len(missing) > 20 else ""))


def import_json(imp: ReferenceImport) -> dict[str, Any]:
    return {"id": imp.id, "status": imp.status, "name": imp.name, "preview": imp.preview, "result": imp.result,
            "error": imp.error, "dataset_id": imp.dataset_id, "job_id": imp.job_id,
            "created_at": imp.created_at.isoformat(), "decided_at": imp.decided_at.isoformat() if imp.decided_at
            else None}


def discard_files(imp: ReferenceImport) -> None:
    """Delete the received package and the ingest source built from it."""
    shutil.rmtree(paths.reference_import(imp.id), ignore_errors=True)
    if imp.upload_id:
        up = paths.uploads(imp.upload_id)
        if up.is_dir() and up.resolve().is_relative_to((paths.data_root() / "uploads").resolve()):
            shutil.rmtree(up, ignore_errors=True)


def reject(session: Session, imp: ReferenceImport) -> None:
    imp.status, imp.decided_at = "rejected", utcnow()
    discard_files(imp)


# ---------------------------------------------------------------- accepting

def _link(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        dest.unlink()
    try:
        os.symlink(src.resolve(), dest)
    except OSError:
        shutil.copyfile(src, dest)


def build_source(session: Session, imp: ReferenceImport, pkg: Package) -> Path:
    """The folder the ingest job reads: each file at its dataset path (from audio/ or Bench's stored
    original) plus the one references file. Nothing else of the package is in it."""
    if pkg.manifest is None:
        return pkg.root
    missing = missing_files(session, pkg)
    if missing:
        raise PackageError(_missing_message(missing))
    src = paths.reference_import(imp.id) / "source"
    if src.exists():
        shutil.rmtree(src)
    for f in pkg.files:
        audio = pkg.audio_path(f)
        if not audio.is_file():
            audio = stored_original(session, f["sha256"], PurePosixPath(f["path"]).suffix.lower())
            assert audio is not None
        _link(audio, src / f["path"])
    assert pkg.refs is not None
    _link(pkg.refs, src / REFS_DIR / pkg.refs.name)
    return src


def accept(session: Session, imp: ReferenceImport, *, name: str | None = None, tags: list[str] | None = None,
           language: str | None = None, plan: dict[str, Any] | None = None) -> tuple[Dataset, Job]:
    """A new dataset from a staged package, filled by the ingest job; `finish` completes it."""
    from ..runs import jobs

    pkg = read_package(Path(imp.path), formats=_formats(imp))
    source = build_source(session, imp, pkg)
    m = pkg.manifest or {}
    prev = previous_version(session, pkg)
    provenance: dict[str, Any] = {}
    if pkg.manifest is not None:
        provenance = {
            "import_id": imp.id, "package_name": pkg.name, "source": pkg.source,
            "export_version": pkg.export_version, "scope": m.get("scope"), "created_at": m.get("created_at"),
            "label_types": m.get("label_types"), "counts": m.get("counts"), "audio_included": m.get("audio_included"),
            "review": m.get("review"), "models": m.get("models"), "normaliser": m.get("normaliser"),
        }
    ds = Dataset(name=(name or "").strip()[:200] or dataset_name_for(session, pkg),
                 language=((language if language is not None else m.get("language")) or "").strip() or None,
                 description=m.get("description"), tags=list(tags if tags is not None else m.get("tags") or []),
                 status="ingesting", provenance=provenance, source_key=source_key(pkg.source),
                 export_version=pkg.export_version, previous_version_id=prev.id if prev else None)
    if (cat := catalog_of(imp)) is not None:
        ds.catalog_dataset_id, ds.catalog_version = cat["dataset_id"], cat["n"]
    session.add(ds)
    session.flush()
    job = jobs.enqueue(session, "ingest", {"dataset_id": ds.id, "source": str(source), "cleanup": False,
                                           "plan": plan, "reference_import_id": imp.id})
    imp.status, imp.dataset_id, imp.job_id, imp.error = "accepting", ds.id, job.id, None
    return ds, job


def finish(session: Session, import_id: int, dataset_id: int, log=print) -> dict[str, Any]:
    """After the ingest: Label's segment uids and seg-model outputs; the import is accepted."""
    imp = session.get(ReferenceImport, import_id)
    ds = session.get(Dataset, dataset_id)
    if imp is None or ds is None:
        return {}
    from ..runs.cache import OutputCache

    result: dict[str, Any] = {}
    root = Path(imp.path)
    oc = OutputCache(session, log)
    if imp.manifest is not None and root.is_dir():
        pkg = read_package(root, check_audio=False, formats=_formats(imp))
        result["uids"] = assign_uids(session, ds, pkg.uids)
        if pkg.attributes:
            result["attributes"] = apply_attributes(session, ds, pkg.attributes)
        result["outputs"] = import_outputs(session, ds, pkg, oc)
        log(f"reference package: {result['uids']['matched']} of {len(pkg.uids)} segment uids matched; "
            f"outputs imported {result['outputs']['imported']}, skipped {sum(result['outputs']['skipped'].values())}"
            + (f"; labels on {result['attributes']['files']} file(s) and {result['attributes']['segments']} "
               "speaker segment(s)" if "attributes" in result else ""))
    if (cat := catalog_of(imp)) is not None:
        result["use"] = add_use(cat["dataset_id"], cat["n"], ds.id, log)
    imp.status, imp.result, imp.decided_at = "accepted", result, utcnow()
    session.commit()
    oc.flush()  # the imported outputs, into the catalog's index
    discard_files(imp)
    return result


def add_use(catalog_id: str, n: int, dataset_id: int, log=print) -> bool:
    """Record the Bench dataset as a use of the catalog version, so the catalog shows where it is used. The audio
    is held by the ingest already, so a catalog that doesn't answer only leaves the use out."""
    try:
        with dataset_catalog.calling() as dc:
            dc.add_use(catalog_id, n, str(dataset_id))
        return True
    except (dataset_catalog.Unavailable, dataset_catalog.DatasetCatalogError) as exc:
        log(f"warning: the Dataset Catalog did not record this dataset as a use of v{n}: {exc}")
        return False


def fail(session: Session, import_id: int, error: str) -> None:
    imp = session.get(ReferenceImport, import_id)
    if imp is not None:
        imp.status, imp.error, imp.decided_at = "failed", error[:2000], utcnow()
        session.commit()
        discard_files(imp)


def assign_uids(session: Session, ds: Dataset, uids: list[dict[str, Any]]) -> dict[str, int]:
    """Give segments the sender's uid (CONTRACTS.md §2.4), matched by file and times within 0.01 s."""
    files = {f.rel_path: f for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id))}
    taken: set[int] = set()
    matched = 0
    for rec in uids:
        f = files.get(rec.get("file"))
        try:
            s, e = float(rec["start"]), float(rec["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if f is None:
            continue
        cands = [seg for seg in f.segments if seg.id not in taken and abs(seg.start - s) <= UID_TOLERANCE + 1e-9
                 and abs(seg.end - e) <= UID_TOLERANCE + 1e-9]
        if not cands:
            continue
        seg = min(cands, key=lambda x: abs(x.start - s) + abs(x.end - e))
        seg.uid = str(rec["uid"])
        seg.meta = {**(seg.meta or {}), "source_uid": True}
        taken.add(seg.id)
        matched += 1
    session.flush()
    return {"matched": matched, "unmatched": len(uids) - matched}


def apply_attributes(session: Session, ds: Dataset, a: dict[str, Any]) -> dict[str, int]:
    """File labels onto the files and speaker labels (`speaker.<name>`) onto each of that speaker's segments
    (CONTRACTS.md §2.7). A label that came on the records wins over one of the same name from here. The
    dataset's label summary is then made again from every segment."""
    files = {f.rel_path: f for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id))}
    n_files = n_segments = 0
    file_keys: set[str] = set()
    for path, labels in (a.get("files") or {}).items():
        f = files.get(path)
        if f is None:
            continue
        flat = flatten(labels)
        file_keys |= flat.keys()
        f.meta = {**flat, **(f.meta or {})}
        n_files += 1
    for path, speakers in (a.get("speakers") or {}).items():
        f = files.get(path)
        for seg in (f.segments if f else []):
            labels = speakers.get(seg.speaker) if seg.speaker is not None else None
            if labels:
                seg.meta = {**{SPEAKER_PREFIX + k: v for k, v in flatten(labels).items()}, **(seg.meta or {})}
                n_segments += 1
    records = [({**(f.meta or {}), **(s.meta or {})}, s.start <= 0.01 and s.end >= (f.duration or 0.0) - 0.01)
               for f in files.values() for s in f.segments]
    summary = labels_mod.summarize(records)
    for entry in summary:
        if entry["key"] in file_keys:
            entry["level"] = "file"
    ds.report = {**(ds.report or {}), "labels": summary}
    session.flush()
    return {"files": n_files, "segments": n_segments}


def _params_match(adapter, rec_params: dict[str, Any]) -> bool:
    """The package's call params (written out in full) against Bench's own call for that model. `image`, which
    writers from before the catalog put there, is ignored: the build is matched on `build_key`."""
    own = {k: v for k, v in adapter.params.items() if k not in ("adapter", "image")}
    own.setdefault("sample_rate", BENCH_SAMPLE_RATE)
    theirs = dict(rec_params or {})
    if theirs.pop("adapter", type(adapter).__name__) != type(adapter).__name__:
        return False
    theirs.pop("image", None)
    return all(k in own and own[k] == v for k, v in theirs.items())


def import_outputs(session: Session, ds: Dataset, pkg: Package, oc: Any = None) -> dict[str, Any]:
    """outputs/seg.jsonl rows into the output cache, keyed the way `runs.cache` keys a whole-file call of
    that model, when the model catalog knows the model as a segmenter and `build_key` is the build it runs
    now (CONTRACTS.md §2.5). Others are skipped and counted; rows without a build key (written before the
    catalog) too."""
    from .. import adapters
    from ..platform import catalog
    from ..runs.cache import WHOLE, OutputCache, lookup

    oc = oc or OutputCache(session)
    skipped = {"unknown_model": 0, "no_build_key": 0, "build_differs": 0, "params_differ": 0, "not_in_dataset": 0,
               "invalid": pkg.outputs_invalid}
    imported = cached = 0
    shas = set(session.scalars(select(AudioFile.sha256).where(AudioFile.dataset_id == ds.id)))
    by_model: dict[str, Any] = {}
    for rec in pkg.outputs:
        out = rec.get("output") or {}
        if not {"model", "sha256", "params"} <= rec.keys() or not isinstance(out.get("segments"), list):
            skipped["invalid"] += 1
            continue
        if rec["sha256"] not in shas:
            skipped["not_in_dataset"] += 1
            continue
        name = rec["model"]
        if name not in by_model:
            by_model[name] = None
            try:
                crec = catalog.record(name)
                model = catalog.ensure_model(session, name, crec) if crec is not None else None
                if model is not None and model.is_seg:
                    by_model[name] = (adapters.for_model(model), crec.get("build_key"))
            except (catalog.CatalogUnavailable, catalog.ModelUnusable, adapters.AdapterError):
                pass
        if by_model[name] is None or by_model[name][0].kind != "seg":
            skipped["unknown_model"] += 1
            continue
        adapter, current = by_model[name]
        if not rec.get("build_key"):
            skipped["no_build_key"] += 1
            continue
        if rec["build_key"] != current:
            skipped["build_differs"] += 1
            continue
        if not isinstance(rec["params"], dict) or not _params_match(adapter, rec["params"]):
            skipped["params_differ"] += 1
            continue
        if lookup(session, adapter.service, current, rec["sha256"], WHOLE, WHOLE, adapter.params_hash) is not None:
            cached += 1
            continue
        try:
            segs = [[float(a), float(b)] for a, b in out["segments"]]
            spk = out.get("speakers")
            if spk is not None and len(spk) != len(segs):
                raise ValueError("speakers and segments differ in length")
        except (TypeError, ValueError):
            skipped["invalid"] += 1
            continue
        oc.store(ModelOutput(model_name=adapter.service, build_key=current, audio_sha256=rec["sha256"],
                              span_start=WHOLE, span_end=WHOLE, params_hash=adapter.params_hash,
                              params=adapter.params, output={"segments": segs, "speakers": spk},
                              latency_s=float(rec.get("latency_s") or 0.0), image=rec.get("image")))
        session.flush()
        imported += 1
    return {"lines": len(pkg.outputs) + pkg.outputs_invalid, "imported": imported, "already_cached": cached,
            "skipped": skipped}
