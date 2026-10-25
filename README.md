# Concord Bench

Concord Bench measures how well speech models work on reference datasets that you upload, and it shows
you each error that a model makes. It was built to compare the speech recognition (ASR), voice activity
detection (VAD), and speaker diarisation models that the personal AI platform serves. Speaker diarisation
means working out who spoke when. You can also add a speech enhancement model to any benchmark to see how
enhanced audio changes the scores, and you can break the scores down by the labels that come with the
references, e.g., speaker gender.

Concord Bench works alongside a second app, Concord Label, which turns unlabelled audio into reviewed
references through pseudo-labelling, annotation, and live evaluation. The two apps share no code and keep
separate databases, and they agree only on the formats and endpoints in [CONTRACTS.md](CONTRACTS.md).
Sample files for the contract are in `contracts/`. [SPLIT_PLAN.md](SPLIT_PLAN.md) describes how the
original Concord app was split in two. [PLAN.md](PLAN.md) §5 (Phase 1) and [HANDOFF.md](HANDOFF.md)
describe what Bench does.

Concord Bench doesn't register, verify, start, or call models itself, because the platform's Model Catalog
(`../../platform/catalog/README.md`) handles models. Bench lists the catalog's models and leases the models
that a job needs. It sends every inference call through the catalog's gateway (`aicore` with
`AICORE_CATALOG`), and it shares its outputs with Concord Label through the catalog's output index.

The same catalog service also runs the platform's Dataset Catalog (`../../platform/catalog/README.md`,
Dataset Catalog section), which stores datasets for the platform's apps. When `DATASET_STORE` is set, Bench
keeps its original audio in the Dataset Catalog's store. Bench also imports the datasets that Concord Label
published there, and it publishes its own datasets there (see Dataset Catalog under Features).

## Quickstart

```bash
# prerequisites: uv, just, Node 22+, ffmpeg; the platform repo at ../../platform (rendered), with the
# catalog service running (`just catalog-serve` there, http://127.0.0.1:8700) for anything that runs a model
# or uses the Dataset Catalog
just setup            # uv sync + npm install
just dev              # API :8600 + worker + Vite :5173, then open http://localhost:5173
just test             # backend and frontend unit tests, no GPU or platform needed
just lint             # ruff and tsc
just check-contracts  # the contract samples
```

Without a GPU or a catalog, `REPLAY_DIR=fixtures/recorded AICORE_ENDPOINTS=fixtures/recorded/endpoints.generated.yaml just dev`
serves recorded model responses for the fixture set, with an in-process stand-in for the catalog built from
the recording, so the whole UI works offline.

To try it on the fixture set: Upload dataset, Local path, `<repo>/fixtures/generated` (or drop the
`dataset/audio` folder plus one file from `refs/stm`). Then Run evaluation: the plan lists the catalog's
speech models (register and verify models in the catalog's UI, linked from the Models page).

## Ports

| | Port |
|---|---|
| API (and the built frontend) | 8600 (`CONCORD_PORT`) |
| Vite dev server | 5173 (`CONCORD_WEB_PORT`) |
| Concord Label, for reference | 8620, Vite 5174 |
| Catalog service (platform): the Model Catalog and the Dataset Catalog, for reference | 8700 (`CATALOG_URL`) |

`GET /api/config` reports `app` (`concord-bench`), `version`, `contract_version` and `peer`
(CONTRACTS.md §1). `peer` describes Concord Label at `LABEL_URL`: Bench asks its `/api/config` with a
1 s timeout and keeps the answer for 30 s. Label links show only when Label answers with the same
contract version.

## Two apps

Concord Bench (this repository) and Concord Label (`../concord-label`) run side by side, each with its
own data directory and database.

- **Development.** `just dev` here (API 8600, Vite 5173) and `just dev` in Label (API 8620, Vite 5174).
  Each finds the other through one setting in its `.env`: `LABEL_URL=http://localhost:8620` here and
  `BENCH_URL=http://localhost:8600` in Label. Unset, the links between them are hidden and flags are
  downloaded as a file.
- **Handoff** (contract version 3). Datasets move between the apps through the platform's Dataset
  Catalog. Label publishes a dataset as a catalog version. Here, **Import reference package**, From
  Dataset Catalog, stages a version with a preview, and accepting it makes a new dataset (a later version
  of the same catalog dataset becomes `<name> v<N>`). Back the other way, for a dataset that is a catalog
  version: "Open in Label" on the error pages, the locked score in Label's live evaluation, and "Send
  flags to Label" on the dataset page all name the catalog dataset id. `just e2e-handoff` in the Label
  repository runs this round trip with both images and the catalog service.
- **Offline.** Bench no longer writes offline kits (EVALUATE_PLAN.md §9). Models go to an air-gapped
  machine as a Model Catalog bundle, and the Bench image goes with `docker save` (see Air-gapped machines).
  Concord Label still has its own copy of the old kit code.
- **Contracts.** `just check-contracts` checks `contracts/samples`. `CONTRACTS.md` and `contracts/` are
  kept identical to Label's copies by hand, and nothing compares the two repositories.
- **Fixtures.** Bench owns `fixtures/generated` and `fixtures/recorded`. When `just fixtures` or
  `just record` changes them, copy both folders into the Label repository too.

## Layout

```
backend/concord_bench/
  core/        settings, paths (the only module that knows where files live), SQLite models, db
  datasets/    audio (ffmpeg probe and decode, clipping, 16 kHz derive, peaks), annotation parsers, labels, ingest,
               reference packages, catalog.py (import from and publish to the Dataset Catalog) and store.py
               (`just move-originals`)
  platform/    catalog.py, the Model Catalog (records, leases, plans, index, peer reads; replay stand-in), and
               dataset_catalog.py, the Dataset Catalog (uploads, holds, uses)
  adapters/    openai_asr, openai_chat_audio, oip_segmenter, enhance; record and replay transport
  runs/        job queue and worker, executor (ASR, seg, pipelines), enhanced copies, output cache, references, scoring
  metrics/     normalisers, wer (jiwer), der, jer and vad (pyannote.metrics), cpwer (meeteval), rtf, attribution
  analysis/    top words, substitution pairs, slices by label (bootstrap intervals, heatmap), diarisation
               cards and regions, tag-overlap suggestions
  api/         FastAPI routers
  migrations/  Alembic (bench_0001...; bench_0004 moved models to the catalog; bench_0005 dropped offline export;
               bench_0006 to bench_0008 added metrics and hardware, enhanced copies and saved slices;
               bench_0009 added the dataset's catalog dataset id and version)
  acceptance.py, recording.py   the Phase 1 acceptance flow, and `just record` on top of it
frontend/src/  React, TanStack Query, Table and Virtual, Tailwind (AeroAudio tokens in index.css)
fixtures/      synth/ (kokoro-tts dialogue specs and committed turns), generated/ (audio, refs, templates),
               recorded/ (model responses for replay)
contracts/     sample files for CONTRACTS.md and check.py (`just check-contracts`)
tests/         pytest; `-m live` for tests against running services
```

Concord Label started as a copy of this repository. The modules both apps still have (core, datasets,
registry, platform, adapters, runs/cache.py, runs/jobs.py, runs/worker.py, and the frontend's shared
components and lib) are listed in SPLIT_PLAN.md §2.2. When you fix one of them here, check the same code in
Concord Label. (`registry/` and `platform/services.py` are gone since the Model Catalog, and `offline/`
is gone from Bench since EVALUATE_PLAN.md §9; Label keeps its copy for now.) The Dataset Catalog work
changed several of these modules in different ways in the two apps. Label's AGENTS.md, "Code shared with
Concord Bench", lists which ones now differ.

## Features

### Datasets

You add a dataset by uploading a folder or zip file, or by pointing Bench at a local folder. Bench sends
large uploads in chunks. Every dataset needs references, so Bench refuses an upload whose annotations match
no audio file. You can label such audio in Concord Label first.

Bench keeps each original audio file unchanged, and models and playback use a 16 kHz mono FLAC copy that
Bench derives from it. The originals go to the Dataset Catalog's store when `DATASET_STORE` is set (see
Dataset Catalog below), and to `CONCORD_DATA/originals/<sha256>` when it isn't.

Bench matches annotations to audio files by file stem, which is the file name without its extension. It
reads the following annotation formats:

- ASR: JSONL, NeMo manifest, CSV, and `.txt`.
- Diarisation: RTTM, JSON segments, and a NeMo diarisation manifest with its RTTM files.
- Speaker-attributed transcripts for cpWER: JSON or JSONL segments, STM, SegLST, and NeMo diarisation
  output JSON.

Templates for each format are in `fixtures/generated/templates`.

### Reference packages

A reference package is a folder or zip file that holds one references file and the audio it describes, in
the format that CONTRACTS.md §2 defines (see also SPLIT_PLAN.md §4). You can download a package from the
Dataset Catalog or Concord Label, export one from another tool, or write one by hand, and the template is
`reference_package.zip`. A package can leave out audio that Bench already stores, because Bench finds the
stored original by its sha256 hash.

To import a package, open Datasets and choose Import reference package. Bench stages the package and shows
a preview, and it ingests the package only after you accept it. Bench records where each dataset's
references came from. When you import a second package from the same source, Bench makes a new dataset
named `<name> v<N>` and links it to the earlier versions. The dataset page then offers to run the models
that were benchmarked on the previous version.

Bench reads each package format version:

- Version 1 is the first format.
- Version 2 adds labels for slicing, both on the records and in `refs/attributes.json`, which holds file
  and speaker labels (CONTRACTS.md §2.7).
- Version 3 allows a package that has audio but no references. Bench refuses such a package, because it
  can't score a dataset without references.

A package can also carry segmentation model outputs (CONTRACTS.md §2.5). Bench copies an output into its
output cache when the output's `build_key` matches the build that the catalog runs now, and it records the
output in the catalog's index. Bench skips rows that have no `build_key`.

### Dataset Catalog

Bench can keep its audio in the platform's Dataset Catalog, and it can move datasets to and from the
catalog (platform DATASET_CATALOG_PLAN.md §9 and §12). The feature is on when `DATASET_STORE` is set to the
store's folder as Bench sees it, and Bench only reads the folder. Bench talks to the catalog service at
`CATALOG_URL` as the app `concord-bench`. When the feature is off, Bench keeps its originals in
`CONCORD_DATA` and hides the Dataset Catalog's pages and panels.

#### Audio in the store

When Bench ingests a dataset, it uploads each original to the store and places a hold on it, which tells
the store that Bench uses the file. Bench sends only the files that the store doesn't already have, and it
reads originals back from the store. The derived FLAC files, waveform peaks, and enhanced copies stay in
`CONCORD_DATA`. Deleting a dataset releases the holds that no other Bench dataset needs.

While the catalog service is down, a new upload fails, and a delete returns 503 and keeps the dataset.
Datasets that are already in Bench keep working.

#### Importing from the catalog

To import a catalog version, choose Import reference package and then From Dataset Catalog. Bench lists the
catalog's datasets and their versions that have references, because it can't import a version that has
audio only. Bench stages the chosen version from its folder in the store and shows the same preview that a
zip file gets. No audio is copied.

When you accept the version, Bench makes a dataset that holds the version's audio. Bench also records a use
on the version, so the catalog can show where the version is used. A later version of the same catalog
dataset becomes `<name> v<N>`.

#### Publishing to the catalog

You publish a dataset from the Dataset Catalog panel on its dataset page. You can publish a dataset made
from an annotated upload or a zip file, either as version 1 of a new catalog dataset or as the next
version of an existing one. After a zip import, the import page links to the panel.

Bench writes the version from its own segments and labels, and it uploads any audio that the store lacks.
It then records a use on the version. If another app published a newer version after the page loaded, Bench
asks whether you want to publish anyway. Each dataset can be published once. Bench doesn't publish a
dataset that was made from a catalog version, because the references for such a dataset are corrected in
Concord Label.

#### Moving old audio to the store

`just move-originals` moves the audio that Bench stored before the Dataset Catalog existed. First, it
uploads every file in `CONCORD_DATA/originals/` to the store. Second, it places holds on the files that a
Bench dataset uses. Third, it renames the folder to `originals.moved/`.

The command never deletes audio, so you delete `originals.moved/` yourself once you're satisfied. If the
store refuses a file, the command lists the file and leaves `originals/` in place. You can run the command
again to finish the job.

### Models

Bench uses the models in the platform's Model Catalog. The catalog registers each model with a display
name, tags, and the model it was derived from. It also verifies each build of the model on its check clip,
and it records the build's capabilities (ASR, VAD, or diarisation) and the timing it returns (word,
segment, or text).

The Models page lists the catalog's speech models, and you can't change them from Bench. For each model,
the page shows the state (running, stopped, starting, or queued), capabilities, cold start time, and build
key. The page links to the catalog's UI at `CATALOG_UI_URL`.

Enhancement models, which the catalog lists under its `enhance` task, have their own tab. The tab shows the
SI-SDR that the catalog measured on its check clip, which is a score for how close the enhanced audio is to
clean audio. It also shows a "generative" chip when the catalog flags a model as generative.

Bench makes its own `model` row the first time it uses a model, so runs and pipelines keep pointing at the
model. It also keeps the last catalog record it saw, so old runs still display correctly while the catalog
is down. Model pickers rank catalog models by shared tags, and they show unverified models but don't let
you choose them.

### Runs and jobs

One worker process runs the jobs in the `job` table. A job that needs models takes one lease on them from
the catalog, named `bench:job-<id>`, and releases it when the job ends. While the job holds the lease,
neither the catalog nor Concord Label's jobs can stop the models. The `aicore` client renews the lease
every 30 seconds.

If the catalog can't grant the lease yet, the job is `waiting`, and the Jobs page says what the job waits
for, e.g., "waiting for compute #2". Meanwhile, the worker runs jobs that need no model. Among the jobs that
can run, Bench runs the ones whose models are already running first.

A job is `blocked` when the catalog doesn't know one of its models or hasn't verified the model's current
build. A failed lease also blocks the job. The Jobs page shows the reason and has a Retry button.

You can stop a job from the Jobs page, which calls `POST /api/jobs/{id}/stop`. A queued or waiting job ends
at once. A running job first finishes the model calls that are in flight and keeps the outputs it stored.
It then releases its lease, and its state becomes `stopped`. Later stages that need the stopped job are
blocked, and Retry carries on from the stored outputs.

Every model call goes through an output cache, so Bench doesn't compute an output again when it already has
one. The cache key is made of the model, the build key, the audio, the time span, and a hash of the
parameters. The build key is a hash of the catalog's image, config, and weights for the model. The
parameters hash comes from `aicore.catalog.params_hash`, which Concord Label also uses.

To find an output, Bench first looks in its local `model_output` table. If the output isn't there, Bench
checks the catalog's output index once per file or stage. When the index says that Concord Label already has
the output, Bench reads it from Label with `POST <owner>/api/outputs/batch` and copies it in with its
provenance, which records where the output came from. Bench records each new output in the index. Outputs
from before the Model Catalog have no build key, so old runs still score with them, but they never match a
new lookup.

Each run records the builds it used (`run.builds`), the hardware the catalog reports for each model
(`run.hardware`), and its wall time. The UI shows "-" for hardware fields that the catalog doesn't report
yet.

### Pipelines

A pipeline runs a segmentation model and then an ASR model, and the result is a transcript that records who
spoke each segment. The segments come from the segmentation model, or from the dataset's own reference
segments, which Bench calls "oracle". Bench merges short segments and pads each segment by 0.1 seconds.
The ASR model then transcribes each segment.

Pipelines run stage by stage, as described in MODEL_CATALOG_REDESIGN.md under "Job planning". When you
choose Run evaluation, Bench first shows the catalog's plan, which lists the stages, the model loads, the
outputs already in the index, and the estimated load time. Bench then runs the stages in order:

1. One segmentation job per segmentation model runs over every file in the dataset.
2. One ASR job per ASR model transcribes the segments from every segmentation source, including oracle,
   in one stretch.
3. One assemble job per pipeline builds the result from the stored outputs, without calling a model.

Because of the stage order, each model loads only once. A stage whose outputs are all stored already, in
Bench or in Concord Label, leases no models.

Bench splits each pipeline's cpWER errors between the stages (PLAN.md §5 M4). cpWER is a word error rate
that also counts a word as wrong when the transcript gives it to the wrong speaker. Bench computes three
cpWER values:

- A uses the reference segments and the reference speakers.
- B uses the model's segments, and it gives each segment the reference speaker it overlaps most.
- C uses the model's segments and the model's speakers, and it's the reported score.

The ASR model's share of the errors is A, segmentation's share is B minus A, and speaker attribution's
share is C minus B.

### Speech enhancement

You can add a speech enhancement model to a benchmark to see whether enhanced audio changes the scores
(EVALUATE_PLAN.md §5). The enhancement model builds an enhanced copy of the dataset once, and every ASR,
diarisation, and pipeline run that asks for the same enhancer reads the copy.

Files up to 60 seconds long go to the model whole. Longer files go in 30 second chunks, and Bench joins the
chunks with a crossfade. Each output must be within 10 ms of its input's length, and Bench pads or trims it
to the exact length. If an output is further off, the file fails. Bench stores enhanced files by their
sha256 hash like other derived audio, so the output cache works on enhanced files in the same way as on
originals.

In a pipeline, you can put the enhancer before diarisation, so both stages hear the enhanced audio. Or you
can put it after diarisation, so only ASR hears the enhanced audio, cut at the segments that were diarised
on the original audio.

You can list, add, cancel, and delete enhanced copies on the dataset page. Runs that used a deleted copy
keep their scores.

### Labels and slices

Bench can break every score down by the labels on the reference records (EVALUATE_PLAN.md §7). Any extra
field on a reference record becomes a label, including nested fields. For example, a NeMo manifest's
`"attribute": {"gender": ..., "ethnicity": ...}` becomes the labels `attribute.gender` and
`attribute.ethnicity`. Bench sorts each label as discrete, continuous, or unused, and it splits continuous
labels into bins.

The error pages show every score for each slice, which is the set of records that share one label value.
Each slice score comes with a 95% bootstrap interval, which is a range that Bench estimates by resampling
the records. The pages mark the slices that are reliably worse than the whole dataset, and they can cross
two labels in a heatmap. You can save a slice on its dataset.

### Metrics

Bench computes the standard metrics for each task:

- ASR: WER and CER with jiwer, after text normalisation. MER, WIL, and WIP come from the same alignment.
- Diarisation and VAD: DER, JER, and VAD error with pyannote.metrics, with or without overlapping speech.
  Scoring uses a NIST-style collar, which ignores a set number of seconds on each side of a reference
  boundary (0.25 s by default).
- Speaker-attributed transcripts: cpWER and tcpWER with meeteval. tcpWER uses a collar of 2, 5, or 10
  seconds.

The text normaliser is `basic` by default. You can also choose `whisper_en`, `none`, or any platform
`text.transform` service that the catalog knows. Bench calls such a service through the catalog's gateway
and then applies `basic`.

Each score stores a version (`SCORE_VERSION`). When Bench reads a score from an older version, it computes
the score again.

### Error pages

The error pages show where each model goes wrong. On the ASR, diarisation, pipeline, and enhancement error
pages, you can sort, filter, expand, and play every utterance or file.

- The diarisation page shows where errors gather, e.g., in overlapping speech, at speaker changes, or in
  short turns. It also shows speaker counts and which speakers the model confused with each other.
- The pipeline page traces each wrong word to the stage that caused it, and it shows which stage to
  upgrade.
- The enhancement page compares the places where the enhancer can go in a pipeline, and it plays the clips
  where enhancement changed the transcript.

On the ASR page, you can mark a reference segment with "Flag reference as wrong". The dataset page lists the
flagged references, and scores keep using them until someone corrects the references.

For a dataset that is a Dataset Catalog version, either imported from the catalog or published to it, the
error pages link to the file in Label with "Open in Label". The dataset page sends the flags back with "Send
flags to Label", or you can download the flags file (CONTRACTS.md §3). Both name the catalog dataset id. A
dataset that came from Label before contract version 3 keeps its flags in Bench, and its dataset page says
why.

### Sharing outputs with Concord Label

Bench and Concord Label share model outputs, so one app doesn't compute an output again when the other app
already has it. At startup, Bench registers itself with the catalog as `bench` at `PUBLIC_URL`, and it
still starts when the catalog is down. Bench serves `POST /api/outputs/batch`, so Label can read the outputs
that the index says Bench has. Label also reads a catalog dataset's scores from
`GET /api/datasets/by-catalog/<catalog dataset id>/scores` (CONTRACTS.md §4).

## Settings

Environment variables (or `.env`, see [.env.example](.env.example)): `CONCORD_DATA`, `PLATFORM_DIR`,
`AICORE_ENDPOINTS`, `AI_HOST`, `CATALOG_URL` (or `AICORE_CATALOG`; the catalog service, default
`http://127.0.0.1:8700`, `http://catalog:8700` in the image), `CATALOG_UI_URL` (linked from the Models
page and the Dataset Catalog panels; default `CATALOG_URL`), `DATASET_STORE` (the Dataset Catalog's store
as Bench sees it, read-only; unset means no Dataset Catalog), `PUBLIC_URL` (where Label reads Bench's
outputs, registered with the catalog; default `http://127.0.0.1:8600`), `REPLAY_DIR`, `RECORD_DIR`,
`CONCORD_IMAGE`, `CONCORD_HOME`, `LABEL_URL` (Concord Label's base URL, e.g. `http://localhost:8620`;
unset means no Label links), `INFER_CONCURRENCY`
(model calls a job keeps in flight, as JSON by catalog model id, engine or `default`; built in:
`{"vllm": 8, "default": 2}`). Because calls overlap, RTF is wall time: each output's `latency_s` is its
share of the seconds the job spent computing, not its request's own latency. Bench sets
`AICORE_CATALOG` for `aicore` from `CATALOG_URL`, so every model call goes through the catalog's gateway.
Each app has its own data directory and database. The only thing the apps share on disk is the Dataset
Catalog's store, which both of them only read. Bench's schema versions
start with `bench_`, and Bench refuses to start (the API, the worker and `just migrate`) on a database
it did not write — Concord Label's, or one from the single Concord app before the split — without
changing it. Point `CONCORD_DATA` at an empty folder to start fresh.

## Fixtures and recordings

Bench owns the fixtures. Concord Label keeps copies of `fixtures/generated` and `fixtures/recorded`, so
copy them there again when they change.

- `just fixtures` rebuilds `fixtures/generated` from `fixtures/synth/dialogues.yaml`. Turns already in
  `fixtures/synth/turns/` are reused (kokoro on a GPU is not bit-exact), so the output is identical;
  delete a turn file to synthesise it again (needs `kokoro-tts` up).
- `just record` runs the acceptance flow through the live Model Catalog (which starts what it needs) and
  rewrites `fixtures/recorded/` (responses, the endpoint map they were taken with, and `summary.json` with
  the catalog's capabilities of each model, which the replay stand-in serves). Recordings are keyed by
  params hash without the image; the check clip now lives with the catalog (`platform/catalog/fixtures`).

## Tests

- `just test`: backend (pytest) and frontend (vitest) unit tests, including the Phase 1 acceptance flow
  on recorded responses (`tests/test_acceptance.py`). No catalog is needed: tests use aicore's
  `FakeCatalog` (leases, index, plans in memory) or the replay stand-in, and `FakeDatasetCatalog` for the
  Dataset Catalog.
- `just e2e`: Playwright smoke tests on replayed fixtures. The API runs in the `concord-bench:0.2.0` image
  (build it first with `just build`) with this checkout's backend, tests and `frontend/dist` mounted;
  browsers run in the Playwright image.
- `just e2e-handoff` in the Label repository: the round trip between the two images through the catalog
  service (upload in Label, publish, import here, flag back to Label).
- `just test-live`, `just test-perf`: against the live catalog and its services, and at realistic size.

## Image

`just build` builds `concord-bench:0.2.0`. The platform's `aicore` client comes in as a named build
context. To run it next to the platform (the catalog answers at `http://catalog:8700` on `ai-net`):

```bash
docker run -d --name concord-bench --network ai-net -p 127.0.0.1:8600:8600 \
  -v "$(realpath ../../platform)":/platform -v bench-data:/data concord-bench:0.2.0
```

The image sets `CATALOG_URL=http://catalog:8700`, `PUBLIC_URL=http://concord-bench:8600` (where Label reads
Bench's outputs) and `AI_HOST={name}`. The platform is mounted for the aicore endpoint map
(`AICORE_ENDPOINTS`). To use the Dataset Catalog, also mount its store read-only and point `DATASET_STORE`
at it, e.g. `-v "$HOME/ai-data/datasets":/datasets:ro -e DATASET_STORE=/datasets`. In development, run
Bench on the host instead.

## Air-gapped machines

Bench does not write offline kits any more (EVALUATE_PLAN.md §9). To run it on a machine with no internet
access:

1. Export the models from the Model Catalog (`POST /api/exports` or its UI) and import the bundle on the
   target with `python -m catalog import <bundle>` (platform `catalog/README.md`, Export and import).
2. Save the Bench image here with `docker save concord-bench:0.2.0 -o concord-bench.tar`, copy it over, and
   load it there with `docker load -i concord-bench.tar`.
3. Start it as in Image above, next to the target's platform. Datasets, runs and scores do not travel.

## Status

Phase 1 (PLAN.md §5, milestones M0 to M7) and the offline export (O1 to O3) are implemented, renamed and
trimmed to Concord Bench (SPLIT_PLAN.md milestone S3), with Bench's side of reference package import
(S4), the links to Concord Label (S5), GPU leases shared with Label (S6), and offline kits holding either
or both apps (S7). The split (SPLIT_PLAN.md) is built.

Since then, models come from the platform's Model Catalog (MODEL_CATALOG_REDESIGN.md): registration and
checks moved there, jobs lease models from it, outputs are keyed by build and shared with Label through
its index, and pipeline grids run stage by stage. The offline export is removed (EVALUATE_PLAN.md §9).

The revised Evaluate design (EVALUATE_PLAN.md) is built, with MER, WIL, WIP, tcpWER, hardware per run,
sortable leaderboards, speech enhancement as an ablation, slices by label, and the deeper diarisation,
pipeline and enhancement error pages, and reference packages that carry labels (contract version 2).
EVALUATE_PLAN.md §14 lists where the build differs from that plan.

Bench's side of the platform's Dataset Catalog (DATASET_CATALOG_PLAN.md in the platform repository,
milestones D4 and D6) is built: originals in the store, import from and publish to the catalog,
`just move-originals`, and contract version 3, where datasets move between the apps through the catalog.
That plan's "What was built" section lists where the build differs from it.

## Limitations

- Models and audio
  - Bench sends each file or reference segment to a model in one request, so a very long file works only
    if the model accepts it. For example, VAD and diarisation models receive the audio as JSON numbers,
    which come to about 1.25 GB per hour of audio. Bench itself sets no length limit.
  - Bench doesn't compute enhancement quality scores on its own datasets, e.g., DNSMOS, SNR estimates,
    SI-SDR, or PESQ (EVALUATE_PLAN.md §1). The Models page shows only the SI-SDR that the catalog measured
    on its own check clip.
  - Bench needs the catalog service to run models and to use the Dataset Catalog. While the service is
    down, uploads to the store fail and deletes return 503, but existing datasets and old runs still
    display.
- Datasets
  - You can't update a dataset in place. To change a dataset's labels or references, you upload the
    dataset again, and you can publish the new upload as the next version of the same Dataset Catalog
    dataset.
  - Datasets, runs, and scores don't move to an air-gapped machine. Only the models and the Bench image do
    (see Air-gapped machines).
- Unbuilt features and test data
  - Keyword spotting (KWS) isn't built (Q6). The code has only the parts that KWS will plug into, e.g.,
    the enums, the `keyword_event` table, the registries keyed by task, and a benchmark tab bar that
    changes with the task.
  - The fixture set is synthesised speech from kokoro-tts. It's good for checking correctness, but it
    doesn't sound like real recordings.
