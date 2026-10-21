# Concord

Evaluate and label speech models (ASR, VAD / speaker diarisation) served by the personal AI platform,
on datasets you upload. Phase 1 (Evaluate + Bring your own) and Phase 2 (Label) are built; Improve
(Phase 3) is not. [HANDOFF.md](HANDOFF.md) says what to build, [PLAN.md](PLAN.md) how.

Concord does not run models. It reads the platform's endpoint map through `aicore`, starts the
services a job needs with the platform's own `tools/stack.py`, and calls them over their contracts.

## Quickstart

```bash
# prerequisites: uv, just, Node 22+, ffmpeg; the platform repo at ../../platform (rendered)
just setup            # uv sync + npm install
just dev              # API :8600 + worker + Vite :5173 -> open http://localhost:5173
just test             # backend + frontend unit tests, no GPU or platform needed
just lint             # ruff, import-linter (Evaluate must not import Improve), tsc
```

Without a GPU, `REPLAY_DIR=fixtures/recorded AICORE_ENDPOINTS=fixtures/recorded/endpoints.generated.yaml just dev`
serves recorded model responses for the fixture set, so the whole UI works offline.

To try it on the fixture set: Upload dataset → Local path → `<repo>/fixtures/generated` (or drop the
`dataset/audio` folder plus one file from `refs/stm`), then Add model for each speech model in the
registry, then Run evaluation.

## Layout

```
backend/concord/
  core/        settings, paths (the only module that knows where files live), SQLite models, db
  datasets/    audio (ffmpeg probe/decode, clipping, 16 kHz derive, peaks), annotation parsers, ingest
  registry/    platform registry reader, compatibility check
  platform/    health, image tags, ensure_services() -> stack.py up
  adapters/    openai_asr, openai_chat_audio, oip_segmenter; record / replay transport
  runs/        job queue + worker, executor (ASR, seg, pipelines), output cache, scoring
  metrics/     normalisers, wer (jiwer), der/jer + vad (pyannote.metrics), cpwer (meeteval), rtf, attribution
  analysis/    top words, substitution pairs, slices, diarisation cards, tag-overlap suggestions
  label/       Phase 2: ensemble fusion, pseudo-label runs, console, annotate edits, live evaluation,
               promotion, label packages (import / export)
  api/         FastAPI routers
  migrations/  Alembic
backend/concord_improve/   Phase 3 placeholder router; only in concord-full
frontend/src/  React + TanStack Query/Table/Virtual + Tailwind (AeroAudio tokens in index.css)
fixtures/      synth/ (kokoro-tts dialogue specs + committed turns), generated/ (audio, refs, templates),
               recorded/ (model responses for replay)
tests/         pytest; `-m live` for tests against running services
```

## How it works

- **Datasets.** Upload a folder or zip (chunked), or point at a local folder. Originals are copied to
  `CONCORD_DATA/originals/<sha256>`; models and playback use a derived 16 kHz mono FLAC. Files over
  5 minutes are flagged and kept out of runs. Annotations are matched to audio by file stem: ASR
  (JSONL / NeMo manifest / CSV / `.txt`), diarisation (RTTM / JSON segments / NeMo diarisation
  manifest plus its RTTMs), speaker-attributed transcripts for cpWER (JSON/JSONL segments, STM, SegLST,
  NeMo diarisation output JSON). Templates are under `fixtures/generated/templates`.
- **Models.** Pick a registry entry; the compatibility check runs it on a synthesised 10 s
  two-speaker clip and detects capabilities (ASR / VAD / diarisation) and timing (word / segment /
  text). Nothing has to be typed.
- **Runs.** One worker process works through the `job` table. Before a job it asks the platform
  for the job's services (plus those of queued jobs that fit next to them under `util_limit`). If the
  platform can't start one, the job is `blocked` with the platform's message. Every model call goes
  through the output cache (model, audio, span, params incl. image tag), so pipelines reuse stage
  outputs and rescoring with another normaliser or collar needs no inference.
- **Pipelines.** Seg stage (or the dataset's own segments, "oracle") → merge short segments, pad
  0.1 s → transcribe each cut → speaker-attributed hypothesis. cpWER error attribution:
  ASR = A, segmentation = B − A, speaker = C − B (PLAN §5 M4).
- **Metrics.** WER/CER via jiwer after a normaliser (`basic` default, `whisper_en`, `none`, or any
  platform `text.transform` service followed by `basic`). DER/JER/VAD via pyannote.metrics with a
  NIST-style collar (seconds each side, default 0.25 s). cpWER via meeteval.

## Label (Phase 2)

- **Review console** (`/label`): label progress per dataset in audio time (confirmed / auto-accepted /
  needs review / rejected / running / not labelled) and the next action.
- **Pseudo-label**: segments come from one VAD / diarisation model, cut exactly like a pipeline's, and
  every ASR member transcribes each cut. Outputs saved by earlier runs (benchmarks, pipelines, other
  label runs) are reused, so re-fusing needs no service at all. Fusion: best-aligned hypothesis
  (default), ROVER over word-timed members, or one model. Confidence = member agreement; triage
  >= 0.85 auto-accept, 0.50–0.84 review, below rejected. Pseudo-labels go into `segment` with the
  label run as `source_run_id`.
- **Annotate**: whole-file timeline (one lane per speaker, overlap hatch, flags lane), segment grid with
  shift-click multi-select and a bulk bar, inline transcript editing with autosave, speaker pill
  cycling, merge / split / add. Shortcuts: Space, Tab, 1–8, V, M, Enter. Any edit marks the segment
  as touched by a person; pseudo-labelling never replaces it afterwards.
- **Live evaluation**: WER of each model against confirmed segments (from saved outputs only),
  "too few to rank" below 30, bootstrap 95% intervals, the locked benchmark score, DER on fully
  reviewed files, the latest confirmations. **Promote** copies confirmed segments into a new,
  benchmarkable dataset; auto-accepted labels never become references.
- **Import / export**: label packages (`manifest.json`, `files.csv`, `segments.jsonl`,
  `keywords.jsonl`, optional `runs/` and `audio/`). Import is staged and previewed; files match by
  SHA-256, segments by `uid` or time overlap. Reviewed work is never overwritten by pseudo-labels,
  reviewed-on-both-sides differences become conflicts to resolve, and a snapshot taken before the
  merge makes it undoable.

## Settings

Environment variables (or `.env`, see [.env.example](.env.example)): `CONCORD_DATA`,
`PLATFORM_DIR`, `AICORE_ENDPOINTS`, `AI_HOST`, `NO_EVICT`, `FEATURE_IMPROVE`, `REPLAY_DIR`,
`RECORD_DIR`, `CONCORD_HOME`.

## Fixtures and recordings

- `just fixtures` rebuilds `fixtures/generated` from `fixtures/synth/dialogues.yaml`. Turns already
  in `fixtures/synth/turns/` are reused (kokoro on a GPU is not bit-exact), so the output is
  identical; delete a turn file to re-synthesise it (needs `kokoro-tts` up).
- `just record` runs the acceptance flow against live services from whatever state the platform is
  in, starting what it needs, and rewrites `fixtures/recorded/` (responses, the endpoint map they
  were taken with, and `summary.json`).

## Images

`just build-lite` / `just build-full` build `concord-lite` and `concord-full` from one Dockerfile.
Lite has no `concord_improve`, no Training nav, button or route. To run one next to the platform:

```bash
docker run -d --name concord --network ai-net -p 127.0.0.1:8600:8600 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "$(realpath ../../platform)":/platform -v concord-data:/data concord-lite:0.1.0
```

`AI_HOST={name}` is set in the image, so `aicore` calls services by name on `ai-net`. stack.py runs
compose against the host's Docker daemon with paths from the mounted platform directory, so mount
the platform at the same absolute path it has on the host if your compose file uses relative bind
mounts. In development, run Concord on the host instead.

## Offline kits

Concord and chosen models can go to a machine with no internet access ([OFFLINE_EXPORT_PLAN.md](OFFLINE_EXPORT_PLAN.md)).
**Export for offline** (Bring your own) lists every platform service with its image and weight sizes;
tick the models, pick a kit kind and a destination folder, and the worker writes the kit there
(progress on the Jobs page). From the command line:

```bash
just export-offline silero-vad whisper-large-v3 --kind full --out /mnt/usb   # Concord + platform + models
just export-offline whisper-large-v3-turbo --kind models --out /mnt/usb       # models only, for an existing install
just verify-kit /mnt/usb/concord-offline-...                                  # checksums against manifest.json
```

- A kit is a folder: `images/` (one `docker save` of the models' images, plus `concord.tar` in a full
  kit), `assets/` (weights in `AI_ASSETS` layout; Hugging Face models as a fresh cache entry with real
  files), `platform/` (a runnable platform tree holding only the chosen services), `install.sh`,
  `README.md`, `manifest.json` and `SHA256SUMS`. The platform's `tools/bundle.py` writes the first three.
- Missing images or weights stop the export (`just build NAME`, `docker pull`, `just fetch-weights NAME`,
  or start an engine model once so it downloads). Not fitting under `util_limit` together is only a warning.
- On the offline machine, `./install.sh` (full kit) verifies the checksums, loads the images, writes
  `platform/.env` (`AI_ASSETS=<kit>/assets`, `HF_HUB_OFFLINE=1`) and starts Concord from
  `concord/compose.yml` with `platform/` and `assets/` mounted at their host paths. `./install.sh --add
  <install>` (model kit) merges models into it; a service already there with another image is refused
  unless `--replace`. Register models there with Add model: no registrations, scores or datasets travel.
- Exporting from a containerised Concord needs the platform and `$AI_ASSETS` mounted read-only at their
  host paths and the destination folder mounted; on the host none of this applies.
- `just test-offline` exports a full `silero-vad` kit, installs it on an `--internal` Docker network,
  benchmarks VAD there and compares with the recorded score, then merges a `text-normalizer` model kit
  (Docker only, no GPU; about a minute).

## Status

Phase 1 milestones M0–M7, Phase 2 (PLAN §6) and the offline export (O1–O3 of OFFLINE_EXPORT_PLAN.md) are implemented; see "Known limits" below and PLAN
§10 for open questions.

Known limits:
- KWS is not built (Q6). Only its hooks exist: the enums, the `keyword_event` table, task-keyed
  registries, a task-driven benchmark tab bar, the disabled Keywords focus in Annotate and an empty
  `keywords.jsonl` in label packages.
- Label packages do not carry deletions: a segment deleted on one side comes back if the other side
  still has it.
- "Use for retraining" (full image only) is shown but disabled until Phase 3 is scoped (Q5).
- The fixture set is synthesised speech (kokoro-tts): good for correctness, not realism.
