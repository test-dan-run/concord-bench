# Concord

Evaluate speech models (ASR, VAD / speaker diarisation) served by the personal AI platform, on
datasets you upload. Phase 1 (Evaluate + Bring your own) is built; Label (Phase 2) and Improve
(Phase 3) are not. [HANDOFF.md](HANDOFF.md) says what to build, [PLAN.md](PLAN.md) how.

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

## Status

Phase 1 milestones M0–M7 are implemented; see "Known limits" below and PLAN §10 for open questions.

Known limits:
- KWS is not built (Q6). Only its hooks exist: the enums, the `keyword_event` table, task-keyed
  registries and a task-driven benchmark tab bar.
- Phase 2 items are visible but disabled: Label nav entries, "Open in annotator".
- The fixture set is synthesised speech (kokoro-tts): good for correctness, not realism.
