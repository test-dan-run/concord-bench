# Concord Bench

Benchmark speech models (ASR, VAD and speaker diarisation) served by the personal AI platform on
reference datasets you upload, and read their errors.

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
just check-contracts  # contract samples, and a comparison with ../concord-label when it is there
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
  `BENCH_URL=http://localhost:8600` in Label. Unset, the links between them are hidden and packages are
  downloaded instead of pushed.
- **Handoff.** In Label, **Send to Bench** on a dataset writes a reference package (CONTRACTS.md §2):
  pushed straight here when Bench answers (only the audio Bench lacks travels), or downloaded as a zip.
  Here, **Import reference package** stages it with a preview; accepting it makes a new dataset (a later
  package from the same source becomes `<name> v<N>`). Back the other way: "Open in Label" on the error
  pages, the locked score in Label's live evaluation, and "Send flags to Label" on the dataset page.
  `just e2e-handoff` in the Label repository runs this round trip with both images.
- **Offline.** A full offline kit written by either app can hold Bench, Label or both; with both, they
  start from one compose file and already know each other's address (see Offline kits).
- **Contracts.** `just check-contracts` checks `contracts/samples` and compares `CONTRACTS.md` and
  `contracts/` with `../concord-label` (or `--peer PATH`); they must stay byte-identical.
- **Fixtures.** Bench owns `fixtures/generated` and `fixtures/recorded`. When `just fixtures` or
  `just record` changes them, copy both folders into the Label repository too.

## Layout

```
backend/concord_bench/
  core/        settings, paths (the only module that knows where files live), SQLite models, db
  datasets/    audio (ffmpeg probe and decode, clipping, 16 kHz derive, peaks), annotation parsers, ingest
  platform/    the model catalog (records, leases, plans, index, peer reads; replay stand-in), platform tools
  adapters/    openai_asr, openai_chat_audio, oip_segmenter; record and replay transport
  runs/        job queue and worker, executor (ASR, seg, pipelines), output cache, references, scoring
  metrics/     normalisers, wer (jiwer), der, jer and vad (pyannote.metrics), cpwer (meeteval), rtf, attribution
  analysis/    top words, substitution pairs, slices, diarisation cards, tag-overlap suggestions
  offline/     offline kits: export, kit files, install.sh, merge
  api/         FastAPI routers
  migrations/  Alembic (bench_0001...; bench_0004 moved models to the catalog)
  acceptance.py, recording.py   the Phase 1 acceptance flow, and `just record` on top of it
frontend/src/  React, TanStack Query, Table and Virtual, Tailwind (AeroAudio tokens in index.css)
fixtures/      synth/ (kokoro-tts dialogue specs and committed turns), generated/ (audio, refs, templates),
               recorded/ (model responses for replay)
contracts/     sample files for CONTRACTS.md and check.py (`just check-contracts`)
tests/         pytest; `-m live` for tests against running services
```

Concord Label started as a copy of this repository. The modules both apps still have (core, datasets,
registry, platform, adapters, runs/cache.py, runs/jobs.py, runs/worker.py, offline, and the frontend's
shared components and lib) are listed in SPLIT_PLAN.md §2.2. When you fix one of them here, check the
same code in Concord Label. (`registry/` and `platform/services.py` are gone since the model catalog.)

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
  a package folder or zip, from Concord Label (pushed or downloaded), another tool or by hand (template:
  `reference_package.zip`). It is staged with a preview until accepted, then ingested from its audio
  (or Bench's stored originals, by sha256) and its one references file. The dataset keeps where the
  references came from; a package from the same source is a new dataset `<name> v<N>` linked to the
  earlier versions, and the dataset page offers to run the models benchmarked on the previous version.
  Seg-model outputs in the package go into the output cache when their `build_key` is the build the catalog
  runs now (CONTRACTS.md §2.5; rows without one are skipped), and into the catalog's index. Label calls
  `POST /api/fingerprints/missing`, the chunked upload, `POST /api/reference-imports` and
  `GET /api/datasets/by-source/<uid>/scores`.
- **Models** come from the model catalog: it registers them (display name, tags, "derived from"),
  verifies them on its check clip and records their capabilities (ASR, VAD, diarisation) and timing
  (word, segment or text) per build. The Models page lists the catalog's speech models read-only, with
  state (running, stopped, starting, queued), capabilities, cold start and build key, and links to the
  catalog's UI (`CATALOG_UI_URL`). Bench makes its own `model` row when a model is first used, so runs and
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
  A run records the builds it used (`run.builds`).
- **Pipelines** run stage by stage (MODEL_CATALOG_REDESIGN.md, "Job planning"): Run evaluation first
  shows the catalog's plan (stages, model loads, outputs already in the index, estimated load time). Then
  one seg-stage job per segmenter runs over every file of the dataset, one ASR-stage job per ASR model
  transcribes the cuts of every seg source (oracle included) in one stretch, and one assemble job per
  pipeline puts it together from stored outputs with no model. Each model loads once; a stage whose outputs
  are all stored (locally or at Label) leases nothing. The seg stage (or the dataset's own segments,
  "oracle") gives segments; short ones are merged, each is padded by 0.1 s and transcribed, and the result
  is a speaker-attributed hypothesis. cpWER error attribution: ASR = A, segmentation = B − A, speaker =
  C − B (PLAN.md §5 M4).
- **Outputs for Concord Label.** At startup Bench registers itself with the catalog as `bench` at
  `PUBLIC_URL` (a catalog that is down is tolerated), and serves `POST /api/outputs/batch` so Label reads
  outputs the index says Bench has instead of computing them again.
- **Metrics.** WER and CER via jiwer after a normaliser (`basic` by default, `whisper_en`, `none`, or any
  platform `text.transform` service the catalog knows, called through its gateway, followed by `basic`). DER, JER and VAD error via pyannote.metrics
  with a NIST-style collar (seconds each side, 0.25 s by default). cpWER via meeteval.
- **Errors.** ASR, diarisation and pipeline error pages sort, filter, expand and play every utterance or
  file. "Flag reference as wrong" on the ASR page marks a reference segment; flagged references are
  listed on the dataset page. Scores keep using them until the references are corrected. For a dataset
  from Concord Label, the error pages link to the file in Label ("Open in Label") and the dataset page
  sends the flags back ("Send flags to Label", or download the flags file, CONTRACTS.md §3).

## Settings

Environment variables (or `.env`, see [.env.example](.env.example)): `CONCORD_DATA`, `PLATFORM_DIR`,
`AICORE_ENDPOINTS`, `AI_HOST`, `CATALOG_URL` (or `AICORE_CATALOG`; the model catalog, default
`http://127.0.0.1:8700`, `http://catalog:8700` in the image), `CATALOG_UI_URL` (linked from the Models page;
default `CATALOG_URL`), `PUBLIC_URL` (where Label reads Bench's outputs, registered with the catalog;
default `http://127.0.0.1:8600`), `REPLAY_DIR`, `RECORD_DIR`, `CONCORD_IMAGE`, `CONCORD_HOME`, `LABEL_URL`
(Concord Label's base URL, e.g. `http://localhost:8620`; unset means no Label links). Bench sets
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
- `just test-offline`: the air-gap test of an offline kit (Docker, no GPU, about two minutes): a kit with
  Bench alone (plus a model kit merged with `install.sh --add`) and a kit with both apps, each installed
  on an `--internal` network and benchmarked there. Needs `concord-bench:0.2.0` and, for the second,
  `concord-label:0.2.0` (`just build` in each repository), and ports 8600 and 8620 free.
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
Bench's outputs) and `AI_HOST={name}`. Writing offline kits also needs the Docker socket
(`-v /var/run/docker.sock:/var/run/docker.sock`) and the platform mounted at the same absolute path it has
on the host, because the platform's `tools/bundle.py` runs against the host's Docker daemon. In development,
run Bench on the host instead.

## Offline kits

Concord Bench, Concord Label or both, and chosen models, can go to a machine with no internet access
(CONTRACTS.md §6). **Export for offline** lists every
platform service with its image and weight sizes; tick the models, pick a kit kind, tick the apps a full
kit carries ("Include Concord Bench" is ticked; "Include Concord Label" shows Label's local image tag, or
says to run `just build` in that repository) and a destination folder, and the worker writes the kit
there (progress on the Jobs page). From the command line:

```bash
just export-offline silero-vad whisper-large-v3 --kind full --out /mnt/usb   # Bench + platform + models
just export-offline silero-vad --kind full --app concord-bench --app concord-label --out /mnt/usb  # both apps
just export-offline whisper-large-v3-turbo --kind models --out /mnt/usb       # models only, for an existing install
just verify-kit /mnt/usb/concord-offline-...                                  # checksums against manifest.json
```

`--app concord-label=concord-label:0.2.0` picks a tag; without one the other app's image is its contract
name, `concord-label:<this version>`, or else the first tag `docker images concord-label` lists.

- A kit is a folder: `images/` (one `docker save` of the models' images, plus `concord-bench.tar` and/or
  `concord-label.tar` in a full kit), `assets/` (weights in `AI_ASSETS` layout; Hugging Face models as a
  fresh cache entry with real files), `platform/` (a runnable platform tree holding only the chosen
  services), `concord/compose.yml`, `install.sh`, `README.md`, `manifest.json` (with `"apps"`) and
  `SHA256SUMS`. The platform's `tools/bundle.py` writes the first three.
- Missing images or weights stop the export (`just build NAME`, `docker pull`, `just fetch-weights NAME`,
  or start an engine model once so it downloads). Not fitting under `util_limit` together is only a
  warning.
- On the offline machine, `./install.sh` (full kit) verifies the checksums, loads the images, writes
  `platform/.env` (`AI_ASSETS=<kit>/assets`, `HF_HUB_OFFLINE=1`) and `concord/.env` (`BENCH_IMAGE`,
  `LABEL_IMAGE`), and starts the apps from `concord/compose.yml` (services `concord-bench` on
  http://127.0.0.1:8600 and `concord-label` on http://127.0.0.1:8620, with `platform/` and `assets/`
  mounted at their host paths). With both apps, `LABEL_URL` and `BENCH_URL` point at each other, so Send
  to Bench, Open in Label and the locked score work there too.
  `./install.sh --add <install>` (model kit) merges models into it; a service already there with another
  image is refused unless `--replace`. Register models there in the model catalog: no registrations,
  scores or datasets travel. Kits do not carry the catalog yet, so an installed kit can export and list
  but cannot run models until it does (`just test-offline` fails until then).
- Exporting from a containerised Bench needs the platform and `$AI_ASSETS` mounted read-only at their
  host paths and the destination folder mounted; on the host none of this applies.

## Status

Phase 1 (PLAN.md §5, milestones M0 to M7) and the offline export (O1 to O3) are implemented, renamed and
trimmed to Concord Bench (SPLIT_PLAN.md milestone S3), with Bench's side of reference package import
(S4), the links to Concord Label (S5), GPU leases shared with Label (S6), and offline kits holding either
or both apps (S7). The split (SPLIT_PLAN.md) is built.

Since then, models come from the platform's model catalog (MODEL_CATALOG_REDESIGN.md): registration and
checks moved there, jobs lease models from it, outputs are keyed by build and shared with Label through
its index, and pipeline grids run stage by stage.

Known limits:
- KWS is not built (Q6). Only its hooks exist: the enums, the `keyword_event` table, task-keyed
  registries and a task-driven benchmark tab bar.
- The fixture set is synthesised speech (kokoro-tts): good for correctness, not realism.
