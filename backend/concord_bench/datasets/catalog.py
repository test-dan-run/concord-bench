"""Bench's side of the Dataset Catalog's datasets and versions (DATASET_CATALOG_PLAN.md §9.2, §9.3).

- `datasets` / `dataset` list the catalog's datasets and versions for "Import references", marking which
  versions Bench can import (they have references) and which Bench datasets are made from each one.
- `stage_version` stages a version as a reference import, read from its folder on the store's mount, so
  the import preview and accept are the ones a zip gets (`refpackage`).
- `publish` publishes a Bench dataset from an annotated upload or a zip as version 1 of a new catalog
  dataset, or as the next version of an existing one. `write_version` writes the version folder from
  Bench's own segments and labels (CONTRACTS.md §2 without audio/ and outputs/). Datasets made from catalog
  versions are published from Concord Label, where their references are corrected (§9.3).
"""

from __future__ import annotations

import csv
import json
import logging
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import paths
from ..core.models import AudioFile, Dataset, LabelType, ReferenceImport, Segment
from ..platform import dataset_catalog
from . import labels as labels_mod
from .refpackage import ATTRIBUTES, FILES_HEADER, FORMAT, SPEAKER_PREFIX, PackageError, add_use, stage

log = logging.getLogger("concord_bench.dataset_catalog")

# The NO_REFERENCES rule of an upload (datasets.ingest), for a version with audio only (format version 3).
NO_REFERENCES_VERSION = ("v{n} has audio only: Concord Bench benchmarks datasets with references. Label it in "
                         "Concord Label and publish a version with references, then import that one.")


def importable(version: dict[str, Any] | None) -> bool:
    """A version with references (an audio-only version has no label types)."""
    return bool(version and version.get("label_types"))


def _bench(session: Session, catalog_id: str) -> dict[int, list[dict[str, Any]]]:
    """The Bench datasets made from or published as each version of a catalog dataset, by version number."""
    out: dict[int, list[dict[str, Any]]] = {}
    for d in session.scalars(select(Dataset).where(Dataset.catalog_dataset_id == catalog_id).order_by(Dataset.id)):
        out.setdefault(d.catalog_version or 0, []).append(
            {"id": d.id, "name": d.name, "status": d.status, "published": not dataset_catalog.from_catalog(d)})
    return out


def _version(v: dict[str, Any], bench: dict[int, list[dict[str, Any]]]) -> dict[str, Any]:
    keep = ("dataset_id", "n", "parent_n", "parent_deleted", "label_types", "labels", "counts", "published_by",
            "published_at", "format_version")
    return {**{k: v.get(k) for k in keep}, "importable": importable(v), "bench": bench.get(v["n"], [])}


def datasets(q: str | None = None) -> list[dict[str, Any]]:
    """The catalog's datasets, most recently changed first, each with its latest version."""
    with dataset_catalog.calling() as dc:
        rows = dc.datasets(q=q or None)
    return [{**{k: d.get(k) for k in ("id", "name", "language", "description", "tags", "created_by", "updated_at")},
             "latest": _version(d["latest"], {}) if d.get("latest") else None} for d in rows]


def dataset(session: Session, catalog_id: str) -> dict[str, Any]:
    """One catalog dataset with its versions (newest first) and the Bench datasets made from each."""
    with dataset_catalog.calling() as dc:
        d = dc.dataset(catalog_id)
    bench = _bench(session, catalog_id)
    return {**{k: d.get(k) for k in ("id", "name", "language", "description", "tags", "created_by", "updated_at")},
            "latest": _version(d["latest"], bench) if d.get("latest") else None,
            "versions": [_version(v, bench) for v in d.get("versions") or []],
            "url": f"{dataset_catalog.ui_url()}/{catalog_id}"}


def stage_version(session: Session, catalog_id: str, n: int) -> ReferenceImport:
    """A catalog version staged for import. An import of the same version that is still staged is returned
    instead of a second one."""
    for imp in session.scalars(select(ReferenceImport).where(ReferenceImport.status == "staged")):
        cat = (imp.preview or {}).get("catalog") or {}
        if cat.get("dataset_id") == catalog_id and cat.get("n") == n:
            return imp
    with dataset_catalog.calling() as dc:
        d = dc.dataset(catalog_id)
        v = dc.version(catalog_id, n)
    if not importable(v):
        raise PackageError(NO_REFERENCES_VERSION.format(n=n))
    folder = paths.store_version(catalog_id, n)
    if folder is None or not (folder / "manifest.json").is_file():
        raise PackageError(f"version {n} is not at {folder} on the store's mount: DATASET_STORE must be where the "
                           "Dataset Catalog's store is mounted here")
    return stage(session, None, folder, catalog={"dataset_id": catalog_id, "n": n, "dataset_name": d["name"]})


# ---------------------------------------------------------------- publishing (§9.3)

FORMAT_VERSION = 2  # labels on records and in refs/attributes.json (CONTRACTS.md §2.3, §2.7)
PROVENANCE_FIELDS = ("scope", "review", "models", "normaliser")  # carried over from a zip's manifest


class PublishError(ValueError):
    """The dataset can't be published as it is; the message says why."""


def publish_blocker(ds: Dataset) -> str | None:
    """Why the dataset can't be published from Bench, or None."""
    if dataset_catalog.from_catalog(ds):
        return ("this dataset was made from a Dataset Catalog version: its next versions are published from "
                "Concord Label, where the references are corrected")
    if ds.catalog_dataset_id:
        return f"this dataset is already published as v{ds.catalog_version} of a Dataset Catalog dataset"
    if ds.status != "ready":
        return f"the dataset is {ds.status}"
    return None


def _num(x: float) -> float:
    return round(float(x), 6)


def _nest(labels: dict[str, Any]) -> dict[str, Any]:
    """Dotted label names as nested objects again ({"accent.region": x} as {"accent": {"region": x}}), the way
    the references carried them; a name that is also the prefix of another stays dotted."""
    out: dict[str, Any] = {}
    for k, v in sorted(labels.items()):
        parts = k.split(".")
        node = out
        for p in parts[:-1]:
            if not isinstance(node.get(p, {}), dict):
                node = None
                break
            node = node.setdefault(p, {})
        if node is None or parts[-1] in node:
            out[k] = v
        else:
            node[parts[-1]] = v
    return out


def _speaker_labels(segs: list[Segment]) -> dict[str, dict[str, Any]]:
    """Per speaker, the `speaker.` labels every one of their segments has with the same value."""
    out: dict[str, dict[str, Any]] = {}
    by_speaker: dict[str, list[Segment]] = {}
    for seg in segs:
        if seg.speaker is not None:
            by_speaker.setdefault(seg.speaker, []).append(seg)
    for spk, group in by_speaker.items():
        first = labels_mod.of(group[0].meta)
        same = {k: v for k, v in first.items() if k.startswith(SPEAKER_PREFIX)
                and all((g.meta or {}).get(k) == v for g in group[1:])}
        if same:
            out[spk] = same
    return out


def write_version(session: Session, ds: Dataset, dest: Path, name: str | None = None) -> dict[str, Any]:
    """The dataset as a version folder (contracts/datasets/version.md in the platform): manifest.json,
    files.csv and one references file in the format its segments need, with Bench's segment uids and labels
    (labels on the records; file labels and speaker labels in refs/attributes.json). Files that can't be
    decoded are left out, because a version gives each file's duration and sample rate. Returns the manifest."""
    files = [f for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id)
                                        .order_by(AudioFile.rel_path)) if f.usable and f.duration]
    if not files:
        raise PublishError("the dataset has no usable audio files to publish")
    segs = {f.id: sorted(f.segments, key=lambda x: (x.start, x.end, x.speaker or "")) for f in files}
    every = [x for group in segs.values() for x in group]
    if not every:
        raise PublishError("the dataset has no reference segments to publish")
    speakers, texts = {x.speaker is not None for x in every}, {x.text is not None for x in every}
    if speakers == {True} and texts == {True}:
        refs_name, label_types = "segments.seglst.json", [LabelType.asr.value, LabelType.diar.value]
    elif speakers == {False}:
        refs_name, label_types = "segments.jsonl", [LabelType.asr.value]
    elif texts == {False}:
        refs_name, label_types = "refs.rttm", [LabelType.diar.value]
    else:
        raise PublishError("some segments have a speaker without text and others text without a speaker, which one "
                           "references file can't hold together")

    attrs: dict[str, dict[str, Any]] = {"files": {}, "speakers": {}}
    records: list[Any] = []
    rttm: list[str] = []
    uids: list[str] = []
    for f in files:
        group = segs[f.id]
        spk_labels = _speaker_labels(group)
        if spk_labels:
            attrs["speakers"][f.rel_path] = {spk: _nest({k[len(SPEAKER_PREFIX):]: v for k, v in labels.items()})
                                             for spk, labels in spk_labels.items()}
        # File labels that aren't on every record already (those stay on the records).
        own = {k: v for k, v in labels_mod.of(f.meta).items()
               if not group or not all((x.meta or {}).get(k) == v for x in group)}
        if own:
            attrs["files"][f.rel_path] = _nest(own)
        for x in group:
            labels = {k: v for k, v in labels_mod.of(x.meta).items() if k not in spk_labels.get(x.speaker or "", {})}
            if refs_name == "refs.rttm":
                rttm.append(f"SPEAKER {f.stem} 1 {_num(x.start)} {_num(x.end - x.start)} <NA> <NA> {x.speaker} <NA> <NA>")
            elif refs_name == "segments.jsonl":
                records.append({"file": f.rel_path, "start": x.start, "end": x.end, "text": x.text,
                                **({"attributes": _nest(labels)} if labels else {})})
            else:
                records.append({"session_id": f.stem, "speaker": x.speaker, "start_time": x.start, "end_time": x.end,
                                "words": x.text, **({"attributes": _nest(labels)} if labels else {})})
            uids.append(json.dumps({"file": f.rel_path, "start": x.start, "end": x.end, "uid": x.uid}))

    (dest / "refs").mkdir(parents=True, exist_ok=True)
    refs = dest / "refs" / refs_name
    if refs_name == "refs.rttm":
        refs.write_text("\n".join(rttm) + "\n", encoding="utf-8")
    elif refs_name == "segments.jsonl":
        refs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    else:
        refs.write_text(json.dumps(records, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    (dest / "refs" / "uids.jsonl").write_text("\n".join(uids) + "\n", encoding="utf-8")
    attrs = {k: v for k, v in attrs.items() if v}
    if attrs:
        (dest / "refs" / ATTRIBUTES).write_text(json.dumps(attrs, ensure_ascii=False, indent=1) + "\n",
                                                encoding="utf-8")
    with open(dest / "files.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(FILES_HEADER)
        w.writerows([f.rel_path, f.sha256, _num(f.duration or 0.0), f.orig_sr] for f in files)
    prov = ds.provenance or {}
    manifest = {
        "format": FORMAT, "format_version": FORMAT_VERSION, "name": name or ds.name,
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **({"language": ds.language} if ds.language else {}),
        **({"description": ds.description} if ds.description else {}),
        "tags": list(ds.tags or []), "label_types": label_types,
        "counts": {"files": len(files), "segments": len(every),
                   "hours": round(sum(f.duration or 0.0 for f in files) / 3600, 6)},
        "audio_included": "none",
        **{k: prov[k] for k in PROVENANCE_FIELDS if prov.get(k) is not None},
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return manifest


def _audio_to_store(session: Session, ds: Dataset) -> None:
    """Every original of the dataset in the store (ones from before the store go up from data/originals), held."""
    shas: list[str] = []
    for f in session.scalars(select(AudioFile).where(AudioFile.dataset_id == ds.id)):
        if not f.usable:
            continue
        if paths.store_audio(f.sha256) is None:
            local = paths.local_original(f.sha256, f.ext)
            if not local.is_file():
                raise PublishError(f"the original of {f.rel_path} is neither in the store nor in {local.parent}")
            dataset_catalog.upload(local, f.sha256)
        shas.append(f.sha256)
    dataset_catalog.hold(shas)


def publish(session: Session, ds: Dataset, *, catalog_id: str | None = None, name: str | None = None,
            parent: int | None = None, latest: bool = False, force: bool = False) -> dict[str, Any]:
    """Publish the dataset as v1 of a new catalog dataset (no `catalog_id`) or as the next version of
    `catalog_id`, based on version `parent` (`latest`: on whatever version is latest now). Raises PublishError,
    and the catalog's StaleParent (ask, then publish with `force`), MissingAudio or InvalidVersion. Returns the
    version with `created` (False: the latest version has the same content, and the dataset is linked to it)."""
    reason = publish_blocker(ds)
    if reason:
        raise PublishError(reason)
    _audio_to_store(session, ds)
    with tempfile.TemporaryDirectory(prefix="bench-version-") as tmp:
        folder = Path(tmp) / "version"
        folder.mkdir()
        manifest = write_version(session, ds, folder, name)
        with dataset_catalog.calling() as dc:
            made = None
            if catalog_id is None:
                made = dc.create_dataset(manifest["name"], language=ds.language, description=ds.description,
                                         tags=list(ds.tags or []))
                catalog_id, parent = made["id"], None
            elif latest:
                parent = (dc.dataset(catalog_id).get("latest") or {}).get("n")
            try:
                v = dc.publish(catalog_id, folder, parent, force=force)
            except Exception:
                if made is not None:  # no empty dataset left behind
                    dc.delete_dataset(catalog_id)
                raise
    ds.catalog_dataset_id, ds.catalog_version = catalog_id, v["n"]
    session.flush()
    add_use(catalog_id, v["n"], ds.id, log.warning)
    return v
