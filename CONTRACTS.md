# Concord contracts

**Contract version 1.** This file and `contracts/` are kept identical in the Concord Bench and Concord Label
repositories (SPLIT_PLAN.md §2.3). They are the only things the two apps must agree on: the apps share no
code. `just check-contracts` validates the samples and, when the other repository sits next to this one,
fails on any difference between the two copies.

A change to anything below bumps the contract version and lands in both repositories. An app refuses a
format version it does not know, with a message naming the versions it does know. Fields an app does not
know are ignored, so a newer writer can add fields without a version bump.

| Contract | Written by | Read by | Samples |
|---|---|---|---|
| 1. `GET /api/config` | both | both | `samples/api/config.*.json` |
| 2. Reference package, format version 1 | Label, people, other tools | Bench | `samples/reference-package-*` |
| 3. Flags file, format version 1 | Bench | Label | `samples/flags.json` |
| 4. Bench endpoints Label calls | Bench serves | Label | `samples/api/bench.*.json` |
| 5. Label endpoints Bench calls or links to | Label serves | Bench | `samples/api/label.*.json` |
| 6. Apps in an offline kit | either app | the kit's `install.sh` and compose | `samples/kit/compose.yml` |

Times are seconds as JSON numbers. Timestamps are ISO 8601 in UTC with a `Z`. Paths inside packages are
relative POSIX paths without `..`. JSON files are UTF-8.

---

## 1. `GET /api/config`

Both apps serve it. Fields used by the other app:

| Field | Meaning |
|---|---|
| `app` | `"concord-bench"` or `"concord-label"` |
| `version` | the app's version |
| `contract_version` | the version of this file the app implements (`1`) |
| `peer` | `{"url": "<other app's base URL>" or null, "reachable": bool, "contract_version": int or null}` |

An app calls the other only when `peer.reachable` is true and both contract versions match. Otherwise the
links of SPLIT_PLAN.md §5 are hidden and a push is refused before any upload starts.

---

## 2. Reference package (`concord-reference-package`, format version 1)

Hands reviewed references from Label to Bench. A package is a folder, or a `.zip` of that folder (the
folder may be the zip's root or its only top-level directory).

```
<name>/
  manifest.json        optional (see "Packages without a manifest")
  files.csv            required with a manifest
  audio/<path>         originals; may be left out for files the receiver already has
  refs/<one file>      the references
  refs/uids.jsonl      optional
  outputs/seg.jsonl    optional
```

### 2.1 `manifest.json`

| Field | Required | Meaning |
|---|---|---|
| `format` | yes | `"concord-reference-package"` |
| `format_version` | yes | `1` |
| `name` | yes | dataset name suggested to the receiver |
| `created_at` | yes | when the package was written |
| `language`, `description` | no | as on a dataset |
| `tags` | no | list of strings |
| `source` | no | `{"app", "install_id", "dataset_uid", "dataset_name"}`: where the references came from. Packages with the same `source.install_id` and `source.dataset_uid` are versions of one dataset. Without `source` the dataset has no provenance, no Label links and no flags round trip. |
| `export_version` | with `source` | 1, 2, ... for each package sent from that source |
| `scope` | no | `"complete"` (fully reviewed files; ASR and diarisation) or `"any"` (any file with a confirmed segment; ASR only) |
| `label_types` | yes | subset of `["asr", "diar"]`; what the refs file feeds |
| `counts` | yes | `{"files", "segments", "hours"}` |
| `audio_included` | yes | `"all"`, `"partial"` or `"none"` |
| `review` | no | free-form review statistics, shown in the import preview |
| `models` | no | registry names of the models that produced the pseudo-labels |
| `normaliser` | no | suggested WER normaliser, as an id (`basic`) or a key (`basic@1`) |

### 2.2 `files.csv`

Header `path,sha256,duration,sample_rate`; one row per audio file in the package, which may be fewer than
the source dataset has (scope `complete` leaves out files that are not fully reviewed). `path` is the file's
path in the dataset, and its location under `audio/` when included. `sha256` is the hex SHA-256 of the
original bytes. File stems (the file name without its extension) are unique, ignoring case, because
references are matched to audio by stem.

A file listed in `files.csv` but not present under `audio/` must already be known to the receiver by
its `sha256`; otherwise the import fails and lists the missing files. A file under `audio/` whose bytes do
not match its `sha256` fails the import.

### 2.3 References

Exactly one references file under `refs/` (besides `uids.jsonl`), matched to audio by stem. Every
format the receiver's annotated upload reads is accepted (templates: `fixtures/generated/templates`).
Label writes one of these three:

| File | When | Each record |
|---|---|---|
| `refs/segments.seglst.json` | speaker and text (`label_types` asr + diar) | SegLST: `{"session_id": stem, "speaker", "start_time", "end_time", "words": text}` |
| `refs/segments.jsonl` | text without speaker (`label_types` asr) | `{"file": path, "start", "end", "text"}` |
| `refs/refs.rttm` | speaker without text (`label_types` diar) | RTTM `SPEAKER <stem> 1 <start> <duration> <NA> <NA> <speaker> <NA> <NA>` |

Only confirmed segments are written; times are the segments' own (unpadded) times.

### 2.4 `refs/uids.jsonl` (optional)

`{"file": path, "start", "end", "uid"}` per reference segment: the writer's segment uid, so flags can come
back (§3). Matched to a received segment by file and times within 0.01 s.

### 2.5 `outputs/seg.jsonl` (optional)

Seg-model (VAD / diarisation) outputs on whole files, one per line:

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

---

## 3. Flags file (`concord-flags`, format version 1)

Sends "reference is wrong" flags from Bench back to Label.

| Field | Meaning |
|---|---|
| `format` | `"concord-flags"` |
| `format_version` | `1` |
| `created_at` | when it was written |
| `source` | the `source` object of the reference package the flagged dataset came from |
| `export_version` | the package version the flags refer to |
| `flags` | list of `{"uid": string or null, "file_sha256", "start", "end", "note": string or null}` |

The receiver looks for the dataset with `source.dataset_uid`, then matches each flag by `uid`, else by
`file_sha256` and time overlap (IoU >= 0.5). Matched segments get `flagged = true` and the note. Flags
are not a review. A flags file whose `source.install_id` is not the receiver's own install is refused (404).

---

## 4. Bench endpoints Label calls

| Endpoint | Request | Response |
|---|---|---|
| `POST /api/fingerprints/missing` | `{"sha256": [hex, ...]}` | `{"missing": [hex, ...]}`: the ones Bench has no original for |
| `POST /api/uploads` | none | `{"upload_id"}` |
| `PUT /api/uploads/{upload_id}/chunk?path=<name>.zip&offset=<n>` | raw bytes of the zip from `offset` | `{"path", "size"}`; `409` when `offset` is not the current size |
| `POST /api/reference-imports` | `{"upload_id"}` | `201` `{"id", "status": "staged", "preview": {"name", "source", "export_version", "files", "new_files", "hours", "segments", "label_types", "previous_version": {"dataset_id", "name", "export_version"} or null}}`; `400` with `{"detail"}` when the package is invalid |
| `GET /api/datasets/by-source/{dataset_uid}/scores?install_id=<id>&normaliser=<id or key>` (`install_id` optional) | none | `{"dataset": {"id", "name", "export_version"}, "scores": [{"model", "display_name", "wer", "normaliser", "run_id", "score_id", "created_at"}]}` for the newest version; `404` when Bench has no dataset from that source |

A pushed package stays staged until a person accepts it in Bench. `scores` holds, per model, its latest
finished ASR run on that dataset version, scored with the requested normaliser when such a score exists,
else its latest score.

---

## 5. Label endpoints Bench calls or links to

| Endpoint | Request | Response |
|---|---|---|
| `GET /api/files/by-sha/{sha256}?dataset_uid=<uid>` | none | `{"file_id", "dataset_id"}`; `404` when not found |
| Page `/label/file/by-sha/{sha256}?dataset_uid=<uid>&t=<seconds>` | browser link | opens the annotator on that file at `t` |
| `POST /api/flags/import` | a flags file (§3) | `{"matched", "unmatched"}`; `404` when no dataset has `source.dataset_uid` |

---

## 6. Apps in an offline kit

Either app can write a full kit holding one or both app images. The kit's `concord/compose.yml` has one
service per app, as in `samples/kit/compose.yml`:

| | Bench | Label |
|---|---|---|
| Compose service and hostname | `concord-bench` | `concord-label` |
| Image | `concord-bench:<version>` | `concord-label:<version>` |
| Port inside the container and on `127.0.0.1` | 8600 | 8620 |
| Image tar in the kit | `images/concord-bench.tar` | `images/concord-label.tar` |
| Data volume | `bench-data` at `/data` | `label-data` at `/data` |
| Peer URL set when both are in the kit | `LABEL_URL=http://concord-label:8620` | `BENCH_URL=http://concord-bench:8600` |

Both services also get `PLATFORM_DIR=${KIT_DIR}/platform`, `AICORE_ENDPOINTS=${KIT_DIR}/platform/clients/aicore/src/aicore/endpoints.generated.yaml`,
`UV_OFFLINE=1`, the Docker socket, and `platform/` and `assets/` mounted at the same absolute path they
have on the host (the app runs `docker compose` against the host's Docker daemon, which resolves the
platform's bind mounts on the host). The kit's `manifest.json` lists the apps it holds as
`"apps": [{"app", "image", "tar"}]`.
