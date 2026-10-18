"""Label packages: move labelling progress between installs (HANDOFF §4, Import / export).

A package is a zip of plain files:
- manifest.json   format, version, install, dataset details, scope, what is included, counts
- files.csv       path, sha256, duration, sample_rate
- segments.jsonl  file_sha256, start, end, speaker, text, status, confidence, source, updated_at
                  (+ uid and reviewed, so the same segment is recognised on the other side)
- keywords.jsonl  empty until KWS lands (kept so packages stay forward-compatible)
- runs/           optional: label runs and the model outputs (cache rows) for these files
- audio/          optional: original audio at its path

Import is staged, previewed, then applied. Files are matched by fingerprint (sha256); segments by uid,
else by time overlap (IoU >= 0.5) within the file. Merge rules:
- reviewed work (confirmed, or touched by a person) is never overwritten by pseudo-labels;
- incoming reviewed work replaces unreviewed local segments;
- two unreviewed versions: the newer one wins;
- both sides reviewed, differently: a conflict for the user to resolve (local is kept until then).
A snapshot of the target's segments is taken before the merge; undo restores it.

Deletions are not carried: a segment deleted on one side reappears if the other side still has it.
"""

from __future__ import annotations

import csv
import io
import json
import re
import secrets
import shutil
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..core import paths
from ..core.models import (
    AudioFile,
    Dataset,
    FileStatus,
    ModelOutput,
    Run,
    RunTask,
    Segment,
    SegmentStatus,
    SyncPackage,
    utcnow,
)
from ..core.settings import get_settings

FORMAT = "concord-label-package"
VERSION = 1
IOU_MATCH = 0.5
TIME_TOL = 0.01
PREVIEW_ROWS = 2000
SEG_FIELDS = ("start", "end", "speaker", "text", "status", "confidence", "flagged")


class SyncError(ValueError):
    pass


def install_id() -> str:
    p = paths.install_id()
    if not p.is_file():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_hex(8))
    return p.read_text().strip()


def source_of(s: Segment) -> str:
    if s.edited_at is not None:
        return "annotator"
    if s.source_run_id is not None or (s.meta or {}).get("source") == "pseudo-label":
        return "pseudo-label"
    return "reference"


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def _parse_time(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def segment_record(s: Segment, sha: str) -> dict[str, Any]:
    return {"uid": s.uid, "file_sha256": sha, "start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text,
            "status": s.status, "confidence": s.confidence, "source": source_of(s), "reviewed": s.reviewed,
            "flagged": s.flagged, "updated_at": _iso(s.edited_at or s.updated_at)}


# ---------------------------------------------------------------- export

def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "dataset"


def last_export(session: Session, dataset_id: int) -> SyncPackage | None:
    return session.scalar(select(SyncPackage).where(SyncPackage.direction == "export",
                                                    SyncPackage.dataset_id == dataset_id)
                          .order_by(SyncPackage.id.desc()))


def export(session: Session, ds: Dataset, mode: str = "changed", file_ids: list[int] | None = None,
           include_pseudo: bool = True, include_outputs: bool = False, include_audio: bool = False) -> SyncPackage:
    if mode not in ("changed", "all", "files"):
        raise SyncError("scope must be changed, all or files")
    files = [f for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id)
                                        .order_by(AudioFile.rel_path)) if f.status != FileStatus.undecodable]
    prev = last_export(session, ds.id)
    since = prev.created_at if prev and mode == "changed" else None
    if mode == "files":
        wanted = set(file_ids or [])
        files = [f for f in files if f.id in wanted]
        if not files:
            raise SyncError("pick at least one file")
    elif since is not None:
        files = [f for f in files if any(s.updated_at > since for s in f.segments)]
    rows: list[dict[str, Any]] = []
    for f in files:
        for s in sorted(f.segments, key=lambda s: (s.start, s.end)):
            if include_pseudo or s.reviewed:
                rows.append(segment_record(s, f.sha256))
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    paths.exports().mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    out = paths.exports() / f"{_slug(ds.name)}-{ds.id}-{stamp}-{secrets.token_hex(2)}.zip"
    n_outputs = n_audio = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["path", "sha256", "duration", "sample_rate"])
        for f in files:
            w.writerow([f.rel_path, f.sha256, f.duration, f.orig_sr])
        z.writestr("files.csv", buf.getvalue())
        z.writestr("segments.jsonl", "".join(json.dumps(r) + "\n" for r in rows))
        z.writestr("keywords.jsonl", "")
        if include_outputs:
            shas = sorted({f.sha256 for f in files})
            outs = session.scalars(select(ModelOutput).where(ModelOutput.audio_sha256.in_(shas)).order_by(ModelOutput.id))
            lines = []
            for o in outs:
                lines.append(json.dumps({"model_name": o.model_name, "audio_sha256": o.audio_sha256,
                                         "span_start": o.span_start, "span_end": o.span_end,
                                         "params_hash": o.params_hash, "output": o.output, "latency_s": o.latency_s,
                                         "image": o.image}) + "\n")
            n_outputs = len(lines)
            z.writestr("runs/model_outputs.jsonl", "".join(lines))
            label_runs = session.scalars(select(Run).where(Run.task == RunTask.label, Run.dataset_id == ds.id))
            z.writestr("runs/label_runs.jsonl", "".join(json.dumps(
                {"id": r.id, "status": r.status, "params": r.params, "image_tags": r.image_tags,
                 "summary": (r.outputs or {}).get("summary"), "finished_at": _iso(r.finished_at)}) + "\n"
                for r in label_runs))
        if include_audio:
            for f in files:
                src = paths.original(f.sha256, f.ext)
                if src.is_file():
                    z.write(src, f"audio/{f.rel_path}", compress_type=zipfile.ZIP_STORED)
                    n_audio += 1
        counts = {"files": len(files), "segments": len(rows), "by_status": by_status,
                  "reviewed": sum(1 for r in rows if r["reviewed"]), "model_outputs": n_outputs, "audio_files": n_audio}
        manifest = {"format": FORMAT, "version": VERSION, "created_at": utcnow().isoformat(),
                    "concord_version": "0.1.0", "install_id": install_id(),
                    "dataset": {"name": ds.name, "language": ds.language, "tags": ds.tags,
                                "description": ds.description, "label_types": ds.label_types, "mode": ds.mode},
                    "scope": {"mode": mode, "since": _iso(since), "file_ids": file_ids if mode == "files" else None},
                    "include": {"pseudo_labels": include_pseudo, "model_outputs": include_outputs,
                                "audio": include_audio},
                    "counts": counts}
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    pkg = SyncPackage(direction="export", dataset_id=ds.id, status="built", scope=manifest["scope"],
                      counts={**counts, "bytes": out.stat().st_size}, manifest=manifest, path=str(out))
    session.add(pkg)
    session.flush()
    return pkg


# ---------------------------------------------------------------- import: stage

def _extract(zip_path: Path, dest: Path) -> None:
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            parts = PurePosixPath(info.filename).parts
            if info.filename.startswith("/") or ".." in parts:
                raise SyncError(f"unsafe path in package: {info.filename!r}")
        z.extractall(dest)


def _root(folder: Path) -> Path:
    """The folder holding manifest.json (packages zipped with a top folder are accepted)."""
    if (folder / "manifest.json").is_file():
        return folder
    found = [p.parent for p in folder.rglob("manifest.json")]
    if len(found) != 1:
        raise SyncError("not a label package: no manifest.json")
    return found[0]


def read_package(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        manifest = json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError) as exc:
        raise SyncError(f"manifest.json unreadable: {exc}") from None
    if manifest.get("format") != FORMAT:
        raise SyncError(f"not a label package (format {manifest.get('format')!r})")
    if int(manifest.get("version", 0)) > VERSION:
        raise SyncError(f"package version {manifest['version']} is newer than this install understands ({VERSION})")
    try:
        with open(root / "files.csv", newline="", encoding="utf-8") as fh:
            files = [{"path": r["path"], "sha256": r["sha256"],
                      "duration": float(r["duration"]) if r.get("duration") else None,
                      "sample_rate": int(float(r["sample_rate"])) if r.get("sample_rate") else None}
                     for r in csv.DictReader(fh)]
        segs = []
        for k, line in enumerate((root / "segments.jsonl").read_text(encoding="utf-8").splitlines()):
            if line.strip():
                r = json.loads(line)
                if not {"file_sha256", "start", "end"} <= set(r):
                    raise SyncError(f"segments.jsonl line {k + 1}: needs file_sha256, start, end")
                segs.append(r)
    except (OSError, ValueError, KeyError) as exc:
        raise SyncError(f"package unreadable: {exc}") from None
    return manifest, files, segs


def stage(session: Session, source: Path) -> SyncPackage:
    """Unpack a package (zip, or an unpacked folder) into the imports area and summarise it."""
    pkg = SyncPackage(direction="import", status="staged")
    session.add(pkg)
    session.flush()
    dest = paths.imports(pkg.id)
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        if source.is_dir():
            zips = sorted(source.glob("*.zip"))
            if zips and not (source / "manifest.json").exists():
                _extract(zips[0], dest)
            else:
                shutil.copytree(source, dest, dirs_exist_ok=True)
        elif zipfile.is_zipfile(source):
            _extract(source, dest)
        else:
            raise SyncError("upload a .zip label package")
        root = _root(dest)
        manifest, files, segs = read_package(root)
    except (SyncError, zipfile.BadZipFile) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise SyncError(str(exc)) from None
    by_status: dict[str, int] = {}
    for s in segs:
        by_status[s.get("status", "?")] = by_status.get(s.get("status", "?"), 0) + 1
    pkg.path = str(root)
    pkg.manifest = manifest
    pkg.scope = manifest.get("scope") or {}
    pkg.counts = {"files": len(files), "segments": len(segs), "by_status": by_status,
                  "reviewed": sum(1 for s in segs if _incoming_reviewed(s)),
                  "audio_files": sum(1 for f in files if (root / "audio" / f["path"]).is_file()),
                  "model_outputs": _count_lines(root / "runs" / "model_outputs.jsonl")}
    session.flush()
    return pkg


def _count_lines(p: Path) -> int:
    return sum(1 for line in p.read_text().splitlines() if line.strip()) if p.is_file() else 0


def suggestions(session: Session, pkg: SyncPackage) -> list[dict[str, Any]]:
    """Datasets ranked by how many of the package's files they hold (fingerprint match)."""
    _, files, _ = read_package(Path(pkg.path))
    shas = {f["sha256"] for f in files}
    hits: dict[int, int] = {}
    for did, _sha in session.execute(select(AudioFile.dataset_id, AudioFile.sha256).where(AudioFile.sha256.in_(shas))):
        hits[did] = hits.get(did, 0) + 1
    name = (pkg.manifest.get("dataset") or {}).get("name")
    out = []
    for did, n in hits.items():
        d = session.get(Dataset, did)
        out.append({"dataset_id": did, "name": d.name, "matched_files": n, "package_files": len(shas),
                    "same_name": d.name == name})
    # most shared files first; ties go to the dataset with the package's name, then the newest
    return sorted(out, key=lambda r: (-r["matched_files"], not r["same_name"], -r["dataset_id"]))


# ---------------------------------------------------------------- import: preview / merge plan

def _incoming_reviewed(r: dict[str, Any]) -> bool:
    if "reviewed" in r:
        return bool(r["reviewed"])
    return r.get("status") == SegmentStatus.confirmed or r.get("source") == "annotator"


def _iou(a0: float, a1: float, b0: float, b1: float) -> float:
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    union = max(a1, b1) - min(a0, b0)
    return inter / union if union > 0 else 0.0


def _same(local: Segment, r: dict[str, Any]) -> bool:
    return (abs(local.start - float(r["start"])) <= TIME_TOL and abs(local.end - float(r["end"])) <= TIME_TOL
            and (local.speaker or None) == (r.get("speaker") or None) and (local.text or None) == (r.get("text") or None)
            and local.status == r.get("status", local.status))


def _local_json(s: Segment) -> dict[str, Any]:
    return {"id": s.id, "uid": s.uid, "start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text,
            "status": s.status, "confidence": s.confidence, "reviewed": s.reviewed, "source": source_of(s),
            "updated_at": _iso(s.edited_at or s.updated_at)}


def plan(session: Session, pkg: SyncPackage, target_id: int | None) -> dict[str, Any]:
    """The merge outcome per file and segment, without changing anything."""
    root = Path(pkg.path)
    _, files, segs = read_package(root)
    target = session.get(Dataset, target_id) if target_id else None
    if target_id and target is None:
        raise SyncError("target dataset not found")
    local_files = {f.sha256: f for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == target.id))} \
        if target else {}
    known = set(session.scalars(select(AudioFile.sha256).where(AudioFile.sha256.in_([f["sha256"] for f in files]))))
    file_rows = []
    file_outcome: dict[str, str] = {}
    for f in files:
        if f["sha256"] in local_files:
            o = "matched"
        elif f["sha256"] in known or (root / "audio" / f["path"]).is_file():
            o = "add"
        else:
            o = "missing_audio"
        file_outcome[f["sha256"]] = o
        file_rows.append({**f, "outcome": o})
    rows: list[dict[str, Any]] = []
    by_file: dict[str, list[dict[str, Any]]] = {}
    for r in segs:
        by_file.setdefault(r["file_sha256"], []).append(r)
    for sha, incoming in by_file.items():
        fo = file_outcome.get(sha, "missing_audio")
        if fo == "missing_audio":
            rows += [{"outcome": "skipped", "reason": "audio not in this install or the package", "incoming": r}
                     for r in incoming]
            continue
        local = list(local_files[sha].segments) if fo == "matched" else []
        by_uid = {s.uid: s for s in local}
        used: set[int] = set()
        for r in incoming:
            m = by_uid.get(r.get("uid") or "")
            if m is None or m.id in used:
                cands = [(s, _iou(s.start, s.end, float(r["start"]), float(r["end"]))) for s in local if s.id not in used]
                cands = [c for c in cands if c[1] >= IOU_MATCH]
                m = max(cands, key=lambda c: c[1])[0] if cands else None
            if m is None:
                rows.append({"outcome": "add", "incoming": r})
                continue
            used.add(m.id)
            inc_rev, loc_rev = _incoming_reviewed(r), m.reviewed
            if _same(m, r):
                o = "unchanged"
            elif loc_rev and not inc_rev:
                o = "keep_local"
            elif inc_rev and not loc_rev:
                o = "take_incoming"
            elif inc_rev and loc_rev:
                o = "conflict"
            else:
                newer = (_parse_time(r.get("updated_at")) or datetime.min) > (m.edited_at or m.updated_at)
                o = "take_incoming" if newer else "keep_local"
            rows.append({"outcome": o, "incoming": r, "local": _local_json(m)})
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    fcounts: dict[str, int] = {}
    for r in file_rows:
        fcounts[r["outcome"]] = fcounts.get(r["outcome"], 0) + 1
    conflicts = [{"key": f"{r['incoming']['file_sha256'][:12]}:{r['local']['id']}", "file_sha256":
                  r["incoming"]["file_sha256"], "local": r["local"], "incoming": r["incoming"], "resolution": None}
                 for r in rows if r["outcome"] == "conflict"]
    return {"target": {"id": target.id, "name": target.name} if target else None, "files": file_rows,
            "file_counts": fcounts, "counts": counts, "rows": rows[:PREVIEW_ROWS], "truncated": len(rows) > PREVIEW_ROWS,
            "conflicts": conflicts, "_all_rows": rows}


RULES = [
    {"outcome": "add", "rule": "No matching segment here: added."},
    {"outcome": "unchanged", "rule": "Same segment on both sides."},
    {"outcome": "keep_local", "rule": "Reviewed work here is never overwritten by pseudo-labels; between two "
                                      "unreviewed versions the newer one wins."},
    {"outcome": "take_incoming", "rule": "Reviewed work from the package replaces an unreviewed segment here, or "
                                         "the package's unreviewed version is newer."},
    {"outcome": "conflict", "rule": "Reviewed differently on both sides: you choose. Until then this install's "
                                    "version is kept."},
    {"outcome": "skipped", "rule": "The audio is neither in this install nor in the package."},
]


# ---------------------------------------------------------------- import: apply / resolve / undo

def _snapshot(session: Session, ds: Dataset, pkg: SyncPackage) -> Path:
    rows = []
    for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id)):
        for s in f.segments:
            rows.append({"id": s.id, "uid": s.uid, "audio_file_id": s.audio_file_id, "start": s.start, "end": s.end,
                         "speaker": s.speaker, "text": s.text, "status": s.status, "confidence": s.confidence,
                         "source_run_id": s.source_run_id, "flagged": s.flagged, "flag_note": s.flag_note,
                         "edited_at": _iso(s.edited_at), "meta": s.meta, "updated_at": _iso(s.updated_at)})
    p = paths.snapshots() / f"import-{pkg.id}-dataset-{ds.id}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"dataset_id": ds.id, "taken_at": utcnow().isoformat(), "segments": rows}))
    return p


def _apply_fields(s: Segment, r: dict[str, Any], pkg_id: int) -> None:
    s.start, s.end = round(float(r["start"]), 3), round(float(r["end"]), 3)
    s.speaker, s.text = r.get("speaker") or None, r.get("text")
    s.status = r.get("status") or SegmentStatus.pseudo
    s.confidence = r.get("confidence")
    s.flagged = bool(r.get("flagged"))
    if r.get("uid"):
        s.uid = r["uid"]
    s.edited_at = (_parse_time(r.get("updated_at")) or utcnow()) if r.get("source") == "annotator" else None
    if r.get("source") == "pseudo-label":
        s.source_run_id = None
    s.meta = {"imported_from": pkg_id, "source": r.get("source")}


def _add_file(session: Session, ds: Dataset, f: dict[str, Any], root: Path) -> AudioFile | None:
    from ..datasets.ingest import _process_audio

    ext = PurePosixPath(f["path"]).suffix.lower()
    other = session.scalar(select(AudioFile).where(AudioFile.sha256 == f["sha256"]))
    af = AudioFile(dataset_id=ds.id, rel_path=f["path"], sha256=f["sha256"], ext=other.ext if other else ext)
    if other is not None and paths.derived(f["sha256"]).is_file():
        af.duration, af.orig_sr, af.channels, af.status = other.duration, other.orig_sr, other.channels, other.status
    else:
        src = root / "audio" / f["path"]
        if not src.is_file():
            return None
        _process_audio(af, src, get_settings().max_duration_s)
    session.add(af)
    session.flush()
    return af


def _import_outputs(session: Session, root: Path) -> int:
    p = root / "runs" / "model_outputs.jsonl"
    if not p.is_file():
        return 0
    n = 0
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        exists = session.scalar(select(ModelOutput.id).where(
            ModelOutput.model_name == r["model_name"], ModelOutput.audio_sha256 == r["audio_sha256"],
            ModelOutput.span_start == r["span_start"], ModelOutput.span_end == r["span_end"],
            ModelOutput.params_hash == r["params_hash"]))
        if exists:
            continue
        try:
            with session.begin_nested():
                session.add(ModelOutput(**{k: r[k] for k in ("model_name", "audio_sha256", "span_start", "span_end",
                                                             "params_hash", "output", "latency_s", "image")}))
            n += 1
        except IntegrityError:
            pass
    return n


def apply(session: Session, pkg: SyncPackage, target_id: int | None, resolutions: dict[str, str] | None = None,
          new_name: str | None = None) -> SyncPackage:
    if pkg.direction != "import" or pkg.status != "staged":
        raise SyncError(f"package {pkg.id} is {pkg.status}; only a staged import can be applied")
    resolutions = resolutions or {}
    pl = plan(session, pkg, target_id)
    root = Path(pkg.path)
    if target_id:
        ds = session.get(Dataset, target_id)
    else:
        d = pkg.manifest.get("dataset") or {}
        ds = Dataset(name=(new_name or d.get("name") or "Imported labels").strip(), language=d.get("language"),
                     description=d.get("description"), tags=list(d.get("tags") or []), mode=d.get("mode") or "audio_only",
                     label_types=list(d.get("label_types") or []), status="ready")
        session.add(ds)
        session.flush()
    snap = _snapshot(session, ds, pkg)
    files = {f.sha256: f for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id))}
    added_files: list[int] = []
    missing: list[str] = []
    for f in pl["files"]:
        if f["outcome"] == "add" and f["sha256"] not in files:
            af = _add_file(session, ds, f, root)
            if af is None:
                missing.append(f["path"])
                continue
            files[af.sha256] = af
            added_files.append(af.id)
    applied: dict[str, int] = {}
    conflicts = []
    for row in pl["_all_rows"]:
        o, r = row["outcome"], row["incoming"]
        f = files.get(r["file_sha256"])
        if o == "add" and f is not None:
            s = Segment(audio_file_id=f.id)
            _apply_fields(s, r, pkg.id)
            session.add(s)
        elif o == "take_incoming":
            _apply_fields(session.get(Segment, row["local"]["id"]), r, pkg.id)
        elif o == "conflict":
            key = f"{r['file_sha256'][:12]}:{row['local']['id']}"
            choice = resolutions.get(key)
            if choice == "incoming":
                _apply_fields(session.get(Segment, row["local"]["id"]), r, pkg.id)
            conflicts.append({"key": key, "file_sha256": r["file_sha256"], "local": row["local"], "incoming": r,
                              "resolution": choice if choice in ("incoming", "local") else None})
        elif o == "add":
            o = "skipped"
        applied[o] = applied.get(o, 0) + 1
    n_out = _import_outputs(session, root)
    if ds.status != "ready":
        ds.status = "ready"
    if added_files:
        _refresh_report(session, ds)
    pkg.dataset_id, pkg.status, pkg.applied_at, pkg.snapshot_path = ds.id, "applied", utcnow(), str(snap)
    pkg.conflicts = conflicts
    pkg.counts = {**pkg.counts, "outcome": applied, "added_file_ids": added_files, "files_missing_audio": missing,
                  "model_outputs_added": n_out, "created_dataset": not target_id}
    session.flush()
    return pkg


def _refresh_report(session: Session, ds: Dataset) -> None:
    fs = list(session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id)))
    usable = [f for f in fs if f.usable]
    rep = dict(ds.report or {})
    srs: dict[str, int] = {}
    for f in fs:
        if f.orig_sr:
            srs[str(f.orig_sr)] = srs.get(str(f.orig_sr), 0) + 1
    rep.update({"files": len(fs), "usable_files": len(usable), "hours": sum(f.duration or 0 for f in fs) / 3600,
                "usable_hours": sum(f.duration or 0 for f in usable) / 3600, "sample_rates": srs})
    rep.setdefault("flagged", {"clipped": [], "undecodable": [], "too_long": []})
    rep.setdefault("tasks", {})
    ds.report = rep


def resolve(session: Session, pkg: SyncPackage, key: str, choice: str) -> dict[str, Any]:
    if pkg.status != "applied":
        raise SyncError(f"package {pkg.id} is {pkg.status}")
    if choice not in ("incoming", "local"):
        raise SyncError("choice must be incoming or local")
    conflicts = [dict(c) for c in pkg.conflicts]
    c = next((c for c in conflicts if c["key"] == key), None)
    if c is None:
        raise SyncError(f"no conflict {key!r}")
    if choice == "incoming":
        s = session.get(Segment, c["local"]["id"])
        if s is None:
            raise SyncError("the local segment no longer exists")
        _apply_fields(s, c["incoming"], pkg.id)
    c["resolution"] = choice
    pkg.conflicts = conflicts
    session.flush()
    return c


def undo(session: Session, pkg: SyncPackage) -> SyncPackage:
    if pkg.direction != "import" or pkg.status != "applied":
        raise SyncError(f"package {pkg.id} is {pkg.status}; only an applied import can be undone")
    later = session.scalar(select(SyncPackage).where(SyncPackage.direction == "import", SyncPackage.status == "applied",
                                                     SyncPackage.dataset_id == pkg.dataset_id, SyncPackage.id > pkg.id))
    if later is not None:
        raise SyncError(f"undo the later import {later.id} first")
    snap = json.loads(Path(pkg.snapshot_path).read_text())
    ds_id = snap["dataset_id"]
    for fid in pkg.counts.get("added_file_ids") or []:
        f = session.get(AudioFile, fid)
        if f is not None:
            session.delete(f)
    session.flush()
    for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds_id)):
        for s in list(f.segments):
            session.delete(s)
    session.flush()
    for r in snap["segments"]:
        if session.get(AudioFile, r["audio_file_id"]) is None:
            continue
        session.add(Segment(**{**r, "edited_at": _parse_time(r["edited_at"]), "updated_at": _parse_time(r["updated_at"])}))
    ds = session.get(Dataset, ds_id)
    if ds is not None and pkg.counts.get("added_file_ids"):
        _refresh_report(session, ds)
    pkg.status, pkg.undone_at = "undone", utcnow()
    session.flush()
    return pkg


def package_json(pkg: SyncPackage) -> dict[str, Any]:
    d = pkg.manifest.get("dataset") or {}
    return {"id": pkg.id, "direction": pkg.direction, "status": pkg.status, "dataset_id": pkg.dataset_id,
            "dataset_name": d.get("name"), "scope": pkg.scope, "counts": pkg.counts,
            "include": pkg.manifest.get("include"), "source_install": pkg.manifest.get("install_id"),
            "created_at": _iso(pkg.created_at), "applied_at": _iso(pkg.applied_at), "undone_at": _iso(pkg.undone_at),
            "conflicts_open": sum(1 for c in pkg.conflicts if c.get("resolution") is None),
            "conflicts": len(pkg.conflicts), "file": Path(pkg.path).name if pkg.direction == "export" and pkg.path
            else None}
