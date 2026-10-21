# Concord Bench

Benchmark speech models (ASR, VAD and speaker diarisation) served by the personal AI platform on
reference datasets you upload, and read their errors.

Concord Bench is one of two apps. Its partner, **Concord Label**, turns unlabelled audio into reviewed
references (pseudo-labelling, annotation, live evaluation). The two apps share no code and keep separate
databases. They agree only on the formats and endpoints in [CONTRACTS.md](CONTRACTS.md), with sample
files in `contracts/`. [SPLIT_PLAN.md](SPLIT_PLAN.md) describes the split. [PLAN.md](PLAN.md) §5 (Phase 1)
and [HANDOFF.md](HANDOFF.md) describe what Bench does.

Concord Bench does not run models. It reads the platform's endpoint map through `aicore`, starts the
services a job needs with the platform's own `tools/stack.py`, and calls them over their contracts.

## Quickstart

```bash
# prerequisites: uv, just, Node 22+, ffmpeg; the platform repo at ../../platform (rendered)
just setup            # uv sync + npm install
just dev              # API :8600 + worker + Vite :5173, then open http://localhost:5173
just test             # backend and frontend unit tests, no GPU or platform needed
just lint             # ruff and tsc
just check-contracts  # contract samples, and a comparison with ../concord-label when it is there
```

Without a GPU, `REPLAY_DIR=fixtures/recorded AICORE_ENDPOINTS=fixtures/recorded/endpoints.generated.yaml just dev`
serves recorded model responses for the fixture set, so the whole UI works offline.

To try it on the fixture set: Upload dataset, Local path, `<repo>/fixtures/generated` (or drop the
`dataset/audio` folder plus one file from `refs/stm`). Then Add model for each speech model in the
registry, then Run evaluation.

## Ports

| | Port |
|---|---|
| API (and the built frontend) | 8600 (`CONCORD_PORT`) |
| Vite dev server | 5173 (`CONCORD_WEB_PORT`) |
| Concord Label, for reference | 8610, Vite 5174 |

`GET /api/config` reports `app` (`concord-bench`), `version`, `contract_version` and `peer`
(CONTRACTS.md §1). `peer` describes Concord Label at `LABEL_URL`: Bench asks its `/api/config` with a
1 s timeout and keeps the answer for 30 s. Label links show only when Label answers with the same
contract version.

## Layout

```
backend/concord_bench/
  core/        settings, paths (the only module that knows where files live), SQLite models, db
  datasets/    audio (ffmpeg probe and decode, clipping, 16 kHz derive, peaks), annotation parsers, ingest
  registry/    platform registry reader, compatibility check
  platform/    health, image tags, ensure_services() calling stack.py up
  adapters/    openai_asr, openai_chat_audio, oip_segmenter; record and replay transport
  runs/        job queue and worker, executor (ASR, seg, pipelines), output cache, references, scoring
  metrics/     normalisers, wer (jiwer), der, jer and vad (pyannote.metrics), cpwer (meeteval), rtf, attribution
  analysis/    top words, substitution pairs, slices, diarisation cards, tag-overlap suggestions
  offline/     offline kits: export, kit files, install.sh, merge
  api/         FastAPI routers
  migrations/  Alembic (one initial revision; Bench started empty)
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
same code in Concord Label.

## How it works

- **Datasets.** Upload a folder or zip (chunked), or point at a local folder. Every dataset needs
  references: an upload whose annotations match no audio file is refused (label such audio in Concord
  Label). Originals are copied to `CONCORD_DATA/originals/<sha256>`; models and playback use a derived
  16 kHz mono FLAC. Files over 5 minutes are flagged and kept out of runs. Annotations are matched to
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
  Seg-model outputs in the package go into the output cache when the image tag matches. Label calls
  `POST /api/fingerprints/missing`, the chunked upload, `POST /api/reference-imports` and
  `GET /api/datasets/by-source/<uid>/scores`.
- **Models.** Pick a registry entry. The compatibility check runs it on a synthesised 10 s two-speaker
  clip and detects capabilities (ASR, VAD, diarisation) and timing (word, segment or text). Nothing has
  to be typed.
- **Runs.** One worker process works through the `job` table. Before a job it asks the platform for the
  job's services (plus those of queued jobs that fit next to them under `util_limit`) with
  `stack.py up NAME... --holder bench:job-<id>`, renews that lease every minute while the job runs and
  releases it at the end, so Concord Label's worker never stops them mid-run (SPLIT_PLAN.md §6). If the
  platform can't start one, or it does not fit next to services leased by Label, the job is `blocked`
  with the platform's message. Every model call goes through the
  output cache (model, audio, span, params including the image tag), so pipelines reuse stage outputs
  and rescoring with another normaliser or collar needs no inference.
- **Pipelines.** The seg stage (or the dataset's own segments, "oracle") gives segments; short ones are
  merged, each is padded by 0.1 s and transcribed, and the result is a speaker-attributed hypothesis.
  cpWER error attribution: ASR = A, segmentation = B − A, speaker = C − B (PLAN.md §5 M4).
- **Metrics.** WER and CER via jiwer after a normaliser (`basic` by default, `whisper_en`, `none`, or any
  platform `text.transform` service followed by `basic`). DER, JER and VAD error via pyannote.metrics
  with a NIST-style collar (seconds each side, 0.25 s by default). cpWER via meeteval.
- **Errors.** ASR, diarisation and pipeline error pages sort, filter, expand and play every utterance or
  file. "Flag reference as wrong" on the ASR page marks a reference segment; flagged references are
  listed on the dataset page. Scores keep using them until the references are corrected. For a dataset
  from Concord Label, the error pages link to the file in Label ("Open in Label") and the dataset page
  sends the flags back ("Send flags to Label", or download the flags file, CONTRACTS.md §3).

## Settings

Environment variables (or `.env`, see [.env.example](.env.example)): `CONCORD_DATA`, `PLATFORM_DIR`,
`AICORE_ENDPOINTS`, `AI_HOST`, `NO_EVICT`, `REPLAY_DIR`, `RECORD_DIR`, `CONCORD_IMAGE`, `CONCORD_HOME`,
`LABEL_URL` (Concord Label's base URL, e.g. `http://localhost:8610`; unset means no Label links).
Each app has its own data directory and database; nothing is shared on disk.

## Fixtures and recordings

Bench owns the fixtures. Concord Label keeps copies of `fixtures/generated` and `fixtures/recorded`, so
copy them there again when they change.

- `just fixtures` rebuilds `fixtures/generated` from `fixtures/synth/dialogues.yaml`. Turns already in
  `fixtures/synth/turns/` are reused (kokoro on a GPU is not bit-exact), so the output is identical;
  delete a turn file to synthesise it again (needs `kokoro-tts` up).
- `just record` runs the acceptance flow against live services from whatever state the platform is in,
  starting what it needs, and rewrites `fixtures/recorded/` (responses, the endpoint map they were taken
  with, and `summary.json`).

## Tests

- `just test`: backend (pytest) and frontend (vitest) unit tests, including the Phase 1 acceptance flow
  on recorded responses (`tests/test_acceptance.py`).
- `just e2e`: Playwright smoke tests on replayed fixtures. The API runs in the `concord-bench:0.2.0` image
  (build it first with `just build`) with this checkout's backend, tests and `frontend/dist` mounted;
  browsers run in the Playwright image.
- `just test-offline`: the air-gap test of an offline kit (Docker, no GPU, about a minute; needs the
  image).
- `just test-live`, `just test-perf`: against live services, and at realistic size.

## Image

`just build` builds `concord-bench:0.2.0`. The platform's `aicore` client comes in as a named build
context. To run it next to the platform:

```bash
docker run -d --name concord-bench --network ai-net -p 127.0.0.1:8600:8600 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(realpath ../../platform)":/platform -v bench-data:/data concord-bench:0.2.0
```

`AI_HOST={name}` is set in the image, so `aicore` calls services by name on `ai-net`. stack.py runs
compose against the host's Docker daemon with paths from the mounted platform directory, so mount the
platform at the same absolute path it has on the host if your compose file uses relative bind mounts.
In development, run Bench on the host instead.

## Offline kits

Concord Bench and chosen models can go to a machine with no internet access
([OFFLINE_EXPORT_PLAN.md](OFFLINE_EXPORT_PLAN.md)). **Export for offline** lists every platform service
with its image and weight sizes; tick the models, pick a kit kind and a destination folder, and the
worker writes the kit there (progress on the Jobs page). From the command line:

```bash
just export-offline silero-vad whisper-large-v3 --kind full --out /mnt/usb   # Bench + platform + models
just export-offline whisper-large-v3-turbo --kind models --out /mnt/usb       # models only, for an existing install
just verify-kit /mnt/usb/concord-offline-...                                  # checksums against manifest.json
```

- A kit is a folder: `images/` (one `docker save` of the models' images, plus `concord.tar` in a full
  kit), `assets/` (weights in `AI_ASSETS` layout; Hugging Face models as a fresh cache entry with real
  files), `platform/` (a runnable platform tree holding only the chosen services), `install.sh`,
  `README.md`, `manifest.json` and `SHA256SUMS`. The platform's `tools/bundle.py` writes the first three.
- Missing images or weights stop the export (`just build NAME`, `docker pull`, `just fetch-weights NAME`,
  or start an engine model once so it downloads). Not fitting under `util_limit` together is only a
  warning.
- On the offline machine, `./install.sh` (full kit) verifies the checksums, loads the images, writes
  `platform/.env` (`AI_ASSETS=<kit>/assets`, `HF_HUB_OFFLINE=1`) and starts Bench from
  `concord/compose.yml` with `platform/` and `assets/` mounted at their host paths.
  `./install.sh --add <install>` (model kit) merges models into it; a service already there with another
  image is refused unless `--replace`. Register models there with Add model: no registrations, scores or
  datasets travel.
- Exporting from a containerised Bench needs the platform and `$AI_ASSETS` mounted read-only at their
  host paths and the destination folder mounted; on the host none of this applies.
- Kits with both apps (CONTRACTS.md §6) are not built yet.

## Status

Phase 1 (PLAN.md §5, milestones M0 to M7) and the offline export (O1 to O3) are implemented, renamed and
trimmed to Concord Bench (SPLIT_PLAN.md milestone S3), with Bench's side of reference package import
(S4), the links to Concord Label (S5) and GPU leases shared with Label (S6). Not built yet: kits holding
both apps (S7).

Known limits:
- KWS is not built (Q6). Only its hooks exist: the enums, the `keyword_event` table, task-keyed
  registries and a task-driven benchmark tab bar.
- The fixture set is synthesised speech (kokoro-tts): good for correctness, not realism.
