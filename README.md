# Concord Bench

Benchmark speech models (ASR, VAD and speaker diarisation) served by the personal AI platform on
reference datasets you upload, and read their errors. Speech enhancement models can be added to any
benchmark as an ablation, and scores can be broken down by the labels that come with the references.

Concord Bench is one of two apps. Its partner, **Concord Label**, turns unlabelled audio into reviewed
references (pseudo-labelling, annotation, live evaluation). The two apps share no code and keep separate
databases. They agree only on the formats and endpoints in [CONTRACTS.md](CONTRACTS.md), with sample
files in `contracts/`. [SPLIT_PLAN.md](SPLIT_PLAN.md) describes the split. [PLAN.md](PLAN.md) §5 (Phase 1)
and [HANDOFF.md](HANDOFF.md) describe what Bench does.

Concord Bench does not register, verify, start or call models directly. The platform's **model catalog**
(`../../platform/catalog/README.md`) does: Bench lists the catalog's models, leases the ones a job needs,
sends every inference call through the catalog's gateway (`aicore` with `AICORE_CATALOG`), and shares its
outputs with Concord Label through the catalog's output index.

## Quickstart

```bash
# prerequisites: uv, just, Node 22+, ffmpeg; the platform repo at ../../platform (rendered), with the
# model catalog running (`just catalog-serve` there, http://127.0.0.1:8700) for anything that runs a model
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
| Model catalog (platform), for reference | 8700 (`CATALOG_URL`) |

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
  machine as a model catalog bundle, and the Bench image goes with `docker save` (see Air-gapped machines).
  Concord Label still has its own copy of the old kit code.
- **Contracts.** `just check-contracts` checks `contracts/samples`. `CONTRACTS.md` and `contracts/` are
  kept identical to Label's copies by hand, and nothing compares the two repositories.
- **Fixtures.** Bench owns `fixtures/generated` and `fixtures/recorded`. When `just fixtures` or
  `just record` changes them, copy both folders into the Label repository too.

## Layout

```
backend/concord_bench/
  core/        settings, paths (the only module that knows where files live), SQLite models, db
  datasets/    audio (ffmpeg probe and decode, clipping, 16 kHz derive, peaks), annotation parsers, labels, ingest
  platform/    the model catalog (records, leases, plans, index, peer reads; replay stand-in)
  adapters/    openai_asr, openai_chat_audio, oip_segmenter, enhance; record and replay transport
  runs/        job queue and worker, executor (ASR, seg, pipelines), enhanced copies, output cache, references, scoring
  metrics/     normalisers, wer (jiwer), der, jer and vad (pyannote.metrics), cpwer (meeteval), rtf, attribution
  analysis/    top words, substitution pairs, slices by label (bootstrap intervals, heatmap), diarisation
               cards and regions, tag-overlap suggestions
  api/         FastAPI routers
  migrations/  Alembic (bench_0001...; bench_0004 moved models to the catalog; bench_0005 dropped offline export;
               bench_0006 to bench_0008 added metrics and hardware, enhanced copies and saved slices)
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
Concord Label. (`registry/` and `platform/services.py` are gone since the model catalog, and `offline/`
is gone from Bench since EVALUATE_PLAN.md §9; Label keeps its copy for now.)

## How it works

- **Datasets.** Upload a folder or zip (chunked), or point at a local folder. Every dataset needs
  references: an upload whose annotations match no audio file is refused (label such audio in Concord
  Label). Originals are copied to `CONCORD_DATA/originals/<sha256>`; models and playback use a derived
  16 kHz mono FLAC. There is no length limit, but each file (or reference segment) goes to a model in one
  request, so a very long file works only if the model takes it: VAD and diarisation receive the audio as
  JSON numbers, about 1.25 GB per hour of audio. Annotations are matched to
  audio by file stem: ASR (JSONL, NeMo manifest, CSV, `.txt`), diarisation (RTTM, JSON segments, NeMo
  diarisation manifest plus its RTTMs), and speaker-attributed transcripts for cpWER (JSON or JSONL
  segments, STM, SegLST, NeMo diarisation output JSON). Templates are under
  `fixtures/generated/templates`.
- **Reference packages** (CONTRACTS.md §2, SPLIT_PLAN.md §4). Datasets -> Import reference package takes
  a package folder or zip, downloaded from the Dataset Catalog or Concord Label, from another tool or by hand (template:
  `reference_package.zip`). It is staged with a preview until accepted, then ingested from its audio
  (or Bench's stored originals, by sha256) and its one references file. The dataset keeps where the
  references came from; a package from the same source is a new dataset `<name> v<N>` linked to the
  earlier versions, and the dataset page offers to run the models benchmarked on the previous version.
  Format version 2 packages also carry labels for slicing, on the records and in `refs/attributes.json`
  (file and speaker labels, CONTRACTS.md §2.7). Format version 3 allows a package with audio only, which
  Bench refuses because it has no references; versions 1 and 2 are still read.
  Seg-model outputs in the package go into the output cache when their `build_key` is the build the catalog
  runs now (CONTRACTS.md §2.5; rows without one are skipped), and into the catalog's index. Label calls
  only `GET /api/datasets/by-catalog/<catalog dataset id>/scores` (CONTRACTS.md §4).
- **Models** come from the model catalog: it registers them (display name, tags, "derived from"),
  verifies them on its check clip and records their capabilities (ASR, VAD, diarisation) and timing
  (word, segment or text) per build. The Models page lists the catalog's speech models read-only, with
  state (running, stopped, starting, queued), capabilities, cold start and build key, and links to the
  catalog's UI (`CATALOG_UI_URL`). Enhancement models (the catalog's `enhance` task) have their own tab,
  with the SI-SDR the catalog measured on its check clip and a "generative" chip when the catalog flags one. Bench makes its own `model` row when a model is first used, so runs and
  pipelines keep pointing at it, and keeps the last record it saw so old runs read fine while the catalog is
  down. Pickers rank catalog models by shared tags; unverified ones are shown but not offered.
- **Runs.** One worker process works through the `job` table. A job that needs models takes one catalog lease
  on them as `bench:job-<id>` (the aicore client heartbeats it every 30 s), so neither the catalog nor
  Concord Label's jobs stop them mid-run, and releases it at the end. When the catalog cannot grant it yet
  the job is `waiting` ("waiting for compute #2", with what it waits for) and the worker runs jobs that need
  no model meanwhile; among runnable jobs, those whose models are already running go first. A model the
  catalog does not know or has not verified for its current build, or a lease that fails, makes the job
  `blocked` with the reason (Retry on the Jobs page). Every model call goes through the output cache, keyed
  on model, **build key** (the catalog's image + config + weights hash), audio, span and params hash
  (`aicore.catalog.params_hash`, the same in Label). A lookup tries the local `model_output` table, then the
  catalog's output index once per file or stage: an output Label already has is read from Label
  (`POST <owner>/api/outputs/batch`) and copied in with its provenance. New outputs are recorded in the
  index. Outputs from before the catalog have no build key: old runs still score, but they never match.
  A run records the builds it used (`run.builds`), the hardware the catalog reports for each model
  (`run.hardware`, with "-" in the UI for fields the catalog doesn't report yet) and its wall time.
- **Pipelines** run stage by stage (MODEL_CATALOG_REDESIGN.md, "Job planning"): Run evaluation first
  shows the catalog's plan (stages, model loads, outputs already in the index, estimated load time). Then
  one seg-stage job per segmenter runs over every file of the dataset, one ASR-stage job per ASR model
  transcribes the cuts of every seg source (oracle included) in one stretch, and one assemble job per
  pipeline puts it together from stored outputs with no model. Each model loads once; a stage whose outputs
  are all stored (locally or at Label) leases nothing. The seg stage (or the dataset's own segments,
  "oracle") gives segments; short ones are merged, each is padded by 0.1 s and transcribed, and the result
  is a speaker-attributed hypothesis. cpWER error attribution: ASR = A, segmentation = B − A, speaker =
  C − B (PLAN.md §5 M4).
- **Speech enhancement** (EVALUATE_PLAN.md §5). An enhancement model builds an enhanced copy of a dataset
  once, and every ASR, diarisation and pipeline run that asks for that enhancer reads the copy. Files up to
  60 s go to the model whole, and longer ones go in 30 s chunks joined with a crossfade. Every output must
  be within 10 ms of its input's length, and it is padded or trimmed to the exact length, or the file fails.
  Enhanced files are stored by sha256 like other derived audio, so the output cache works on them as it
  does on the originals. In a pipeline, the copy goes in before diarisation (both stages hear it) or after
  (only ASR hears it, cut at the segments diarised on the original). Copies are listed, added, cancelled
  and deleted on the dataset page, and a deleted copy's runs keep their scores.
- **Labels and slices** (EVALUATE_PLAN.md §7). Extra fields on the reference records become labels,
  including nested ones such as a NeMo manifest's `"attribute": {"gender": ..., "ethnicity": ...}`
  (read as `attribute.gender`). Each label is sorted into discrete, continuous (binned) or unused. Error
  pages break every score down by label with a 95% bootstrap interval, mark slices that are reliably worse
  than the whole dataset, and cross two labels in a heatmap. A slice can be saved on its dataset.
- **Outputs for Concord Label.** At startup Bench registers itself with the catalog as `bench` at
  `PUBLIC_URL` (a catalog that is down is tolerated), and serves `POST /api/outputs/batch` so Label reads
  outputs the index says Bench has instead of computing them again.
- **Metrics.** WER and CER via jiwer after a normaliser (`basic` by default, `whisper_en`, `none`, or any
  platform `text.transform` service the catalog knows, called through its gateway, followed by `basic`),
  with MER, WIL and WIP from the same alignment. DER, JER and VAD error via pyannote.metrics with a
  NIST-style collar (seconds each side, 0.25 s by default), with or without overlapping speech. cpWER and
  tcpWER (collar 2, 5 or 10 s) via meeteval. Scores store a version (`SCORE_VERSION`), and a score from an
  older version is computed again when it is read.
- **Errors.** ASR, diarisation, pipeline and enhancement error pages sort, filter, expand and play every
  utterance or file. The diarisation page shows where errors gather (overlap, speaker changes, short
  turns), speaker counts and who was mistaken for whom. The pipeline page traces each wrong word to the
  stage that caused it, and shows which stage to upgrade. The enhancement page compares placements and
  plays clips where enhancement changed the transcript. "Flag reference as wrong" on the ASR page marks a reference segment; flagged references are
  listed on the dataset page. Scores keep using them until the references are corrected. For a dataset
  from Concord Label, the error pages link to the file in Label ("Open in Label") and the dataset page
  sends the flags back ("Send flags to Label", or download the flags file, CONTRACTS.md §3).

## Settings

Environment variables (or `.env`, see [.env.example](.env.example)): `CONCORD_DATA`, `PLATFORM_DIR`,
`AICORE_ENDPOINTS`, `AI_HOST`, `CATALOG_URL` (or `AICORE_CATALOG`; the model catalog, default
`http://127.0.0.1:8700`, `http://catalog:8700` in the image), `CATALOG_UI_URL` (linked from the Models page;
default `CATALOG_URL`), `PUBLIC_URL` (where Label reads Bench's outputs, registered with the catalog;
default `http://127.0.0.1:8600`), `REPLAY_DIR`, `RECORD_DIR`, `CONCORD_IMAGE`, `CONCORD_HOME`, `LABEL_URL`
(Concord Label's base URL, e.g. `http://localhost:8620`; unset means no Label links), `INFER_CONCURRENCY`
(model calls a job keeps in flight, as JSON by catalog model id, engine or `default`; built in:
`{"vllm": 8, "default": 2}`). Because calls overlap, RTF is wall time: each output's `latency_s` is its
share of the seconds the job spent computing, not its request's own latency. Bench sets
`AICORE_CATALOG` for `aicore` from `CATALOG_URL`, so every model call goes through the catalog's gateway.
Each app has its own data directory and database; nothing is shared on disk. Bench's schema versions
start with `bench_`, and Bench refuses to start (the API, the worker and `just migrate`) on a database
it did not write — Concord Label's, or one from the single Concord app before the split — without
changing it. Point `CONCORD_DATA` at an empty folder to start fresh.

## Fixtures and recordings

Bench owns the fixtures. Concord Label keeps copies of `fixtures/generated` and `fixtures/recorded`, so
copy them there again when they change.

- `just fixtures` rebuilds `fixtures/generated` from `fixtures/synth/dialogues.yaml`. Turns already in
  `fixtures/synth/turns/` are reused (kokoro on a GPU is not bit-exact), so the output is identical;
  delete a turn file to synthesise it again (needs `kokoro-tts` up).
- `just record` runs the acceptance flow through the live model catalog (which starts what it needs) and
  rewrites `fixtures/recorded/` (responses, the endpoint map they were taken with, and `summary.json` with
  the catalog's capabilities of each model, which the replay stand-in serves). Recordings are keyed by
  params hash without the image; the check clip now lives with the catalog (`platform/catalog/fixtures`).

## Tests

- `just test`: backend (pytest) and frontend (vitest) unit tests, including the Phase 1 acceptance flow
  on recorded responses (`tests/test_acceptance.py`). No catalog is needed: tests use aicore's
  `FakeCatalog` (leases, index, plans in memory) or the replay stand-in.
- `just e2e`: Playwright smoke tests on replayed fixtures. The API runs in the `concord-bench:0.2.0` image
  (build it first with `just build`) with this checkout's backend, tests and `frontend/dist` mounted;
  browsers run in the Playwright image.
- `just e2e-handoff` in the Label repository: the round trip between the two images.
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
(`AICORE_ENDPOINTS`). In development, run Bench on the host instead.

## Air-gapped machines

Bench does not write offline kits any more (EVALUATE_PLAN.md §9). To run it on a machine with no internet
access:

1. Export the models from the model catalog (`POST /api/exports` or its UI) and import the bundle on the
   target with `python -m catalog import <bundle>` (platform `catalog/README.md`, Export and import).
2. Save the Bench image here with `docker save concord-bench:0.2.0 -o concord-bench.tar`, copy it over, and
   load it there with `docker load -i concord-bench.tar`.
3. Start it as in Image above, next to the target's platform. Datasets, runs and scores do not travel.

## Status

Phase 1 (PLAN.md §5, milestones M0 to M7) and the offline export (O1 to O3) are implemented, renamed and
trimmed to Concord Bench (SPLIT_PLAN.md milestone S3), with Bench's side of reference package import
(S4), the links to Concord Label (S5), GPU leases shared with Label (S6), and offline kits holding either
or both apps (S7). The split (SPLIT_PLAN.md) is built.

Since then, models come from the platform's model catalog (MODEL_CATALOG_REDESIGN.md): registration and
checks moved there, jobs lease models from it, outputs are keyed by build and shared with Label through
its index, and pipeline grids run stage by stage. The offline export is removed (EVALUATE_PLAN.md §9).

The revised Evaluate design (EVALUATE_PLAN.md) is built, with MER, WIL, WIP, tcpWER, hardware per run,
sortable leaderboards, speech enhancement as an ablation, slices by label, and the deeper diarisation,
pipeline and enhancement error pages, and reference packages that carry labels (contract version 2).
EVALUATE_PLAN.md §14 lists where the build differs from that plan.

Known limits:
- Enhancement quality scores (DNSMOS, SNR estimates, SI-SDR and PESQ on Bench's datasets) are not built
  (EVALUATE_PLAN.md §1). The Models page shows only the catalog's SI-SDR on its own check clip.
- Datasets can't be updated in place. A dataset that needs new labels or references is uploaded again.
- KWS is not built (Q6). Only its hooks exist: the enums, the `keyword_event` table, task-keyed
  registries and a task-driven benchmark tab bar.
- The fixture set is synthesised speech (kokoro-tts): good for correctness, not realism.
