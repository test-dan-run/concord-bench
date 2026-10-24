# Concord contracts

**Contract version 3.** This file and `contracts/` are kept identical in the Concord Bench and Concord Label
repositories. Bench keeps this file at its root, and Label keeps it in `docs/`. They are the only things the two
apps must agree on, because the apps share no code. `just check-contracts` checks the samples in each app. It
doesn't compare the two copies, so a change here is copied to the other repository by hand.

A change to anything below bumps the contract version and lands in both repositories. An app refuses a format
version it doesn't know, with a message that names the versions it does know. Fields an app doesn't know are
ignored, so a newer writer can add fields without a version bump.

Version 3 moves datasets between the apps through the platform's Dataset Catalog (DATASET_CATALOG_PLAN.md in
the platform repository). Label publishes a dataset as a catalog version, and Bench imports the version. Neither
app pushes a dataset to the other any more. Flags, links and scores name a dataset by its catalog dataset id,
a hex string of 32 characters. Version 2 added labels to the reference package, and version 1 was the first.

| Contract | Written by | Read by | Samples |
|---|---|---|---|
| 1. `GET /api/config` | both | both | `samples/api/config.*.json` |
| 2. Reference package, format version 3 (versions 1 and 2 are still read) | the Dataset Catalog, Label, people, other tools | Bench | `samples/reference-package-*` |
| 3. Flags file, format version 2 | Bench | Label | `samples/flags.json` |
| 4. Bench endpoints Label calls | Bench serves | Label | `samples/api/bench.*.json` |
| 5. Label endpoints Bench calls or links to | Label serves | Bench, the Dataset Catalog UI | `samples/api/label.*.json` |

Times are seconds as JSON numbers. Timestamps are ISO 8601 in UTC with a `Z`. Paths inside packages are
relative POSIX paths without `..`. JSON files are UTF-8.

Datasets that Bench received from Label under contract version 2 keep their old source (Label's `install_id`
and `dataset_uid`). They aren't migrated, so their flags and their links to Label no longer work. To get them
back, publish the Label dataset to the Dataset Catalog and import the new version in Bench.

---

## 1. `GET /api/config`

Both apps serve it. Fields used by the other app:

| Field | Meaning |
|---|---|
| `app` | `"concord-bench"` or `"concord-label"` |
| `version` | the app's version |
| `contract_version` | the version of this file the app implements (`3`) |
| `peer` | `{"url": "<other app's base URL>" or null, "reachable": bool, "contract_version": int or null}` |

An app calls the other only when `peer.reachable` is true and both contract versions match. Otherwise the
links to the other app are hidden, and flags can only be downloaded as a file.

---

## 2. Reference package (`concord-reference-package`, format version 3)

A reference package is a dataset version plus its audio. The platform owns the version format, in
`contracts/datasets/version.md` of the platform repository (`PLATFORM_DIR`), and that file is the full
definition of `manifest.json`, `files.csv` and `refs/`. This section adds what the apps need on top of it.

The Dataset Catalog stores versions, and the apps read them from the store's mount. A package is how a version
travels as a file: a zip that a person downloads from the catalog, a zip that another tool writes, or a zip that
Label builds for download. A package is a folder, or a `.zip` of that folder. The folder may be the zip's root or
its only top level folder.

```
<name>/
  manifest.json        optional (see "Packages without a manifest")
  files.csv            required with a manifest
  audio/<path>         originals; may be left out for files the receiver already has
  refs/<one file>      the references; none when label_types is empty (format version 3)
  refs/uids.jsonl      optional
  refs/attributes.json optional, format version 2 and later
  outputs/seg.jsonl    optional
```

Format version 3 allows a package with audio only: `label_types` is empty and `refs/` holds no file. Label can
start labelling from such a version. Bench refuses it, because Bench benchmarks against references. Readers
still take format versions 1 and 2.

### 2.1 `manifest.json`

The fields are in the platform's `version.md` §2. Two of them matter most between the apps:

| Field | Meaning |
|---|---|
| `source` | `{"app", "install_id", "dataset_uid", "dataset_name"}`: where the references came from. In a catalog version, `install_id` is `"dataset-catalog"`, `dataset_uid` is the catalog dataset id, and `app` is the app that published it. Packages with the same `install_id` and `dataset_uid` are versions of one dataset. Without `source` the dataset has no versions, no flags round trip and no Label links. |
| `export_version` | with `source`: the version number. In a catalog version it is the version `n`. |
| `audio_included` | `"all"`, `"partial"` or `"none"`: how many of the files have their bytes under `audio/` |

Bench takes a package whose `source.install_id` is `"dataset-catalog"` as that catalog version, so its flags and
links name the catalog dataset (§3, §5). A package Label builds for download names Label's own install and
dataset, so a dataset Bench makes from it can't send flags back.

### 2.2 `files.csv`

As in the platform's `version.md` §3. A file listed in `files.csv` but not present under `audio/` must already be
known to the receiver by its `sha256`, in the Dataset Catalog's store or in the receiver's own data. Otherwise
the import fails and lists the missing files. A file under `audio/` whose bytes don't match its `sha256` fails
the import.

### 2.3 References

As in the platform's `version.md` §4. Every format the receiver's annotated upload reads is accepted (templates:
`fixtures/generated/templates`). Label writes one of these three:

| File | When | Each record |
|---|---|---|
| `refs/segments.seglst.json` | speaker and text (`label_types` asr + diar) | SegLST: `{"session_id": stem, "speaker", "start_time", "end_time", "words": text}` |
| `refs/segments.jsonl` | text without speaker (`label_types` asr) | `{"file": path, "start", "end", "text"}` |
| `refs/refs.rttm` | speaker without text (`label_types` diar) | RTTM `SPEAKER <stem> 1 <start> <duration> <NA> <NA> <speaker> <NA> <NA>` |

Only confirmed segments are written, with their own (unpadded) times. From format version 2, a record in
`refs/segments.jsonl` or `refs/segments.seglst.json` may carry `"attributes"`: labels of that one segment, which
Bench breaks scores down by.

### 2.4 `refs/uids.jsonl` (optional)

`{"file": path, "start", "end", "uid"}` per reference segment: the writer's segment uid, so flags can come back
(§3). Matched to a received segment by file and times within 0.01 s.

### 2.5 `outputs/seg.jsonl` (optional)

Seg model (VAD or diarisation) outputs on whole files, one per line. A catalog version never has them, because the
Model Catalog's output index shares model outputs between the apps. A package zip may still carry them:

```
{"model": model id in the model catalog (the platform service name), "image": image tag (for display),
 "build_key": the catalog's build key of the build that produced it,
 "sha256": audio sha256,
 "params": {"adapter": "OIPSegmenter", ...the adapter's options},
 "latency_s": number,
 "output": {"segments": [[start, end], ...], "speakers": ["spk0", ...] or null}}
```

`build_key` is the model catalog's build key (16 hex characters: image, config and weights; see the
platform's `catalog/README.md`) of the model's build that produced the output. `params` is exactly the
writer's seg adapter params for that call, written out in full (not a hash): `adapter` is the adapter class
name, and any other key is an adapter option. Packages written before the model catalog have no
`build_key` and carry the image tag in `params` as `image`; readers ignore `params.image`.

The receiver computes its own cache key and stores an output only when `build_key` is the build of that
model the catalog runs now, `adapter` names its own adapter for that model, and every other key equals
its own call's option (`sample_rate`, if given, must be 16000). Otherwise the row is skipped, as is a row
without `build_key`.

### 2.6 Packages without a manifest

A folder without `manifest.json` is read like an annotated upload: audio anywhere in the folder,
annotation files anywhere, matched by stem. No provenance.

### 2.7 `refs/attributes.json` (optional, format version 2 and later)

Labels of whole files and of speakers, keyed by the file's path in `files.csv`, as in the platform's
`version.md` §5:

```
{"files": {"<path>": {"<name>": value, ...}, ...},
 "speakers": {"<path>": {"<speaker>": {"<name>": value, ...}, ...}, ...}}
```

Both keys are optional. A value is a string, a number or true / false, and nested objects are read with dotted
names. `<speaker>` is the speaker name as written in the references file.

- A file label, e.g. `{"condition": "noisy"}`, applies to the whole file and to every segment in it.
- A speaker label is read with the prefix `speaker.`, so `{"gender": "F"}` under speaker `agent` is the
  label `speaker.gender` on each of `agent`'s segments in that file.
- A label on a record (§2.3) wins over a file or speaker label of the same name on that segment.

A path that is not in `files.csv`, or a value that is not an object where one is expected, fails the
import.

---

## 3. Flags file (`concord-flags`, format version 2)

Sends "reference is wrong" flags from Bench back to Label, for a Bench dataset made from a catalog version or
published as one.

| Field | Meaning |
|---|---|
| `format` | `"concord-flags"` |
| `format_version` | `2` |
| `created_at` | when it was written |
| `catalog_dataset_id` | the Dataset Catalog dataset the flagged references belong to |
| `catalog_version` | the version `n` the flags refer to |
| `dataset_name` | the dataset's name, for messages |
| `flags` | list of `{"uid": string or null, "file_sha256", "start", "end", "note": string or null}` |

The receiver finds its working dataset by `catalog_dataset_id`: a Label dataset made from a version of that
catalog dataset, or published to it. When more than one Label dataset has that id, the one based on the highest
version wins, then the newest. The receiver then matches each flag by `uid`, else by `file_sha256` and time
overlap (IoU >= 0.5). Matched segments get `flagged = true` and the note. Flags are not a review. A flags file
whose catalog dataset no Label dataset has is refused (404).

Label still reads format version 1 (contract version 2), which named the dataset by `source` and
`export_version` as in §2.1, with Label's own `install_id` and `dataset_uid`.

---

## 4. Bench endpoints Label calls

| Endpoint | Request | Response |
|---|---|---|
| `GET /api/datasets/by-catalog/{dataset_id}/scores?normaliser=<id or key>` | none | `{"dataset": {"id", "name", "catalog_version"}, "scores": [{"model", "display_name", "wer", "normaliser", "run_id", "score_id", "created_at"}]}` for the newest version Bench has of that catalog dataset; `404` when Bench has none |

The newest version is the Bench dataset with the highest `catalog_version` among those imported from the catalog
dataset or published to it. `scores` holds, per model, its latest finished ASR run on that dataset, scored with
the requested normaliser when such a score exists, else its latest score.

Version 3 removed the endpoints that Label's "Send to Bench" pushed packages with (`fingerprints/missing`,
`uploads`, `chunk` and `reference-imports`). Bench's own pages still use its upload and import endpoints, but they
are no longer part of this contract.

---

## 5. Label endpoints Bench calls or links to

| Endpoint | Request | Response |
|---|---|---|
| `GET /api/files/by-sha/{sha256}?catalog_dataset_id=<id>` | none | `{"file_id", "dataset_id"}`; `404` when not found |
| Page `/label/file/by-sha/{sha256}?catalog_dataset_id=<id>&t=<seconds>` | browser link | opens the annotator on that file at `t` |
| `POST /api/flags/import` | a flags file (§3) | `{"matched", "unmatched"}`; `404` when no dataset has the file's `catalog_dataset_id` |

`catalog_dataset_id` picks the working dataset as in §3. Without it, the file is looked up in every dataset.
Bench's "Open in Label" and the Dataset Catalog UI's "Open in Label" both use the page.
