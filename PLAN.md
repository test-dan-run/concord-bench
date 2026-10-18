# Concord — Implementation Plan

Companion to [HANDOFF.md](HANDOFF.md). HANDOFF says *what* to build; this says *how and in what order*.
Platform facts below were checked against `dev/README.md`, `platform/contracts/README.md`,
`platform/services/*.yaml` and `platform/clients/aicore` on 2026-10-17, after the platform changes
listed in §1.1.

---

## 1. What the platform gives us

| Need | Platform | Consequence for Concord |
|---|---|---|
| Model registry | `aicore` endpoint map (`clients/aicore/src/aicore/endpoints.generated.yaml`): name, alias, contract, api, ports, image tag, `aiplatform.*` labels, OIP `signature`, and now `description`, `tasks`, `gpu_fraction`. `aicore.available()` / `endpoint()` read it. | Registry picker = `aicore.available()` deduplicated by `name` (aliases appear as separate keys). Base URL, route, protocol, default tags (`tasks`) all come from `endpoint()`. Nothing typed by the user. |
| Auth | None; ports bound to 127.0.0.1. | No auth fields at all. |
| ASR | `openai` / `audio.transcriptions`. `whisper-large-v3` (vLLM) returns segment times only; `faster-whisper-large-v3` returns `words[]` with `timestamp_granularities[]=word`. | Timing-level detection has a real word / segment split to test against. |
| VAD / diarisation | `oip-v2` / `infer`, inputs `AUDIO` FP32 [-1] + `SAMPLE_RATE` INT32 [1]; outputs `SEGMENTS` FP32 [-1,2] (+ `SPEAKERS` INT32 [-1] for diarisers). | Capability = presence of `SPEAKERS` in the signature, confirmed by the check run. One adapter for silero, pyannote, nemotron. |
| KWS | No service, no API type. | Out of Phase 1 scope (§1.2). |
| Text normalisation | `text-normalizer` (`text.transform`) does capitalisation/punctuation, the opposite of WER normalisation. | WER normalisers live in Concord. Platform `text.transform` services are offered as extra normalisers, not the default. |
| Starting services | `just up NAME...` starts any services by name or alias. Running GPU services stay up if everything fits under `util_limit`, otherwise they are stopped first (`--no-evict` refuses instead). CPU services are never stopped. | Concord starts what a job needs itself (§5 M4). The ASR + diarisation benchmark set (both Whispers, pyannote, nemotron, silero) is 0.339 of the GPU, so it runs together with room to spare. |
| Reproducibility | Endpoint map records pinned image tags and labels. | Store image tag + labels on every run. |
| OIP payload size | JSON tensors only. A 5 min file at 16 kHz = 4.8 M floats ≈ 45–50 MB of JSON per request. | Acceptable at the 5 min cap (§1.2); measure once in M4. |

### 1.1 Platform changes made for Concord (2026-10-17)

- **Endpoint map** (`tools/render.py`): each entry now has `description`, `tasks` and `gpu_fraction`.
- **Profiles removed** from the schema, all manifests, compose, labels, endpoint map, catalog, bundle and tests.
  - `just up NAME...` / `just down [NAME...]` / `just vram [NAME...]` / `just bundle NAME... [--name X]`.
  - `just validate` now rejects a service that is over `util_limit` on its own.
  - The catalog shows one VRAM bar per GPU service.
- Platform tests: 183 passed, 4 skipped (same skips as before). Pre-change snapshot of the platform is in the session scratchpad if a rollback is ever needed.

### 1.2 Owner decisions (2026-10-17)

| # | Topic | Decision |
|---|---|---|
| Q1 | Registry fields | Done in the platform (§1.1). |
| Q2 | File length and storage | Files are at most **5 minutes**. Longer files are flagged at ingest; nothing longer is tested. Audio, derived files and the database live on **local disk** under `CONCORD_DATA`. No object-store support. |
| Q3 | Starting services | No profiles. Concord asks the platform for the services each job needs. |
| Q4 | Variants | **Split**: two images, `concord-lite` and `concord-full`. |
| Q5 | Phase 3 scope | Not answered yet. Phase 3 stays unscheduled. |
| Q6 | KWS | **Not in Phase 1.** Keep the design KWS-ready (§5, "KWS hooks") so it can be added later without migrations or UI rework. |
| Q7 | Speaker-attributed reference formats | Support JSON/JSONL segments, STM and SegLST. |
| Q8 | Error attribution | Use the A / B−A / C−B definition in §5 M4. |
| Q9 | Default WER normaliser | Basic (lowercase, strip punctuation, collapse whitespace). Whisper English normaliser offered. |
| Q10 | Check clip and fixtures | **Synthesised** with `kokoro-tts` (§5 M0). |

---

## 2. Tech stack

Match the platform so one person can maintain both.

| Layer | Choice | Why |
|---|---|---|
| Backend | Python 3.12, **uv**, **just**, FastAPI, pydantic v2 | Same as platform; metric libraries are Python. |
| Model calls | `aicore` (editable dependency on `../../platform/clients/aicore[oip]`) | The platform's own client; no re-implementation of contracts. |
| Starting services | `uv run --project $PLATFORM_DIR python tools/stack.py up ...` as a subprocess | The platform's own tool; no duplicated compose logic. |
| Metrics | `jiwer`, `pyannote.metrics`, `meeteval`, numpy | As specified in HANDOFF §3.6. |
| Audio | ffmpeg (decode anything) + `soundfile` + `soxr` (resample) | Keep originals; derive 16 kHz mono FLAC once at ingest. |
| DB and files | SQLite (WAL) via SQLAlchemy 2 + Alembic; files on local disk under `CONCORD_DATA` (`originals/`, `derived/`, `peaks/`, `exports/`, `concord.db`) | Single user, local, zero ops (Q2). All paths go through one small `paths` module, which is the only thing to change if storage ever moves. |
| Jobs | One worker process reading a jobs table in SQLite | No Redis/Celery. Enough for one GPU and one user. |
| Frontend | React + TypeScript + Vite, TanStack Query + TanStack Table (virtualised), Tailwind with AeroAudio tokens | Tables are the centrepiece; TanStack handles sort/filter/expand. |
| Waveform / lanes | Server-computed peaks (JSON) drawn on canvas; lanes in SVG; native `<audio>` with range requests | Full control over speaker and error lanes. Reused by Phase 2 Annotate. |
| Packaging | One Dockerfile with two targets, `lite` and `full`, giving images `concord-lite` and `concord-full`. FastAPI serves the built frontend. | Q4. |

**Running Concord in a container** needs the Docker socket and the platform directory mounted (for `stack.py`), `AI_HOST={name}`, and the `ai-net` network. In development Concord runs on the host and none of this applies.

---

## 3. Repository layout

```
Concord/
├── HANDOFF.md, PLAN.md, README.md
├── pyproject.toml          # concord deps; [full] extra pulls concord_improve deps
├── Justfile                # dev, test, lint, migrate, fixtures, build-lite, build-full
├── Dockerfile              # targets: lite, full
├── .importlinter           # forbids concord.* -> concord_improve
├── backend/
│   ├── concord/
│   │   ├── core/           # settings, db, storage, feature flags, tags
│   │   ├── datasets/       # ingest, audio, fingerprint, annotation parsers, templates
│   │   ├── registry/       # platform registry reader, compatibility check
│   │   ├── platform/       # ensure_services(): health checks + stack.py up
│   │   ├── adapters/       # openai_asr, openai_chat_audio, oip_segmenter
│   │   ├── runs/           # job queue, worker, pipeline executor, output cache
│   │   ├── metrics/        # wer, der/jer, cpwer, vad, rtf, attribution, normalisers
│   │   ├── analysis/       # top words, substitution pairs, slices, histograms
│   │   ├── label/          # Phase 2
│   │   └── api/            # routers: config, datasets, models, runs, benchmarks, errors, audio
│   └── concord_improve/    # Phase 3; only in the full image
├── frontend/
│   └── src/{app,components,features/{benchmarks,errors,datasets,models,label,improve}}
├── fixtures/
│   ├── synth/              # script + dialogue specs that generate the fixture set with kokoro-tts
│   └── generated/          # the generated audio + references (committed, small)
└── tests/
```

**Variants (`FEATURE_IMPROVE`)**
- Backend: `concord/api/app.py` mounts `concord_improve.router` only when the flag is on and the package imports; `GET /api/config` reports `features.improve`.
- Frontend: `features/improve` is lazy-loaded. The nav item and the "Use for retraining" / "Retrain the diariser" buttons render through one `<IfFeature name="improve">` wrapper. Build-time `VITE_FEATURE_IMPROVE` drops the chunk from the lite build.
- `import-linter` in `just lint` makes "Evaluate/Label must not import Improve" a failing check.
- `concord-lite` installs neither `concord_improve` nor training dependencies. `concord-full` adds both and sets `FEATURE_IMPROVE=1`.

---

## 4. Data model (SQLite)

Follows HANDOFF §6, with one deliberate split: **inference outputs are stored separately from scores**. Changing the normaliser or collar then rescores without re-running models.

| Table | Key fields | Notes |
|---|---|---|
| `dataset` | id, name, language, description, tags[], label_types[] (`asr`, `diar`, `kws`), mode, created_at | One upload = one dataset. No splits. `kws` is in the enum from day one. |
| `audio_file` | id, dataset_id, rel_path, sha256 (of original bytes), duration, orig_sr, channels, status (ok/clipped/undecodable/too_long) | Derived 16 kHz mono at `derived/<sha256>.flac`; peaks at `peaks/<sha256>.json`. |
| `segment` | id, audio_file_id, start, end, speaker?, text?, status (pseudo/auto/confirmed/rejected), confidence?, source_run_id?, flagged, updated_at | Reference labels from upload are `confirmed`. Phase 2 writes the same table; Improve reads it later. |
| `keyword_event` | id, audio_file_id, keyword, start, end, status, confidence?, source_run_id? | Created now (KWS hook), unused in Phase 1. |
| `model` | id, registry_name, display_name, tags[], parent_model_id?, capabilities[], timing_level, check_report (json), registry_snapshot (json) | `capabilities` enum includes `kws`. `registry_snapshot` = endpoint entry at registration time. |
| `pipeline` | id, seg_model_id (null = oracle), asr_model_id | |
| `run` | id, task (`asr`/`seg`/`pipeline`/`kws`), model_id or pipeline_id, dataset_id, status, image_tags (json), params (json), started/finished_at, rtf_by_stage (json) | One inference job. |
| `model_output` | id, model_id, audio_sha256, span_start, span_end, params_hash, output (json), latency_s | **The cache.** Keyed on (model, audio, span, params). Shared across runs, pipelines and Phase 2 pseudo-labelling. |
| `score` | id, run_id, normaliser, collar, aggregates (json), created_at | One per (run, normaliser, collar). "Δ vs previous run" compares scores. |
| `result` | id, score_id, audio_file_id, utterance_key, metrics (json), alignment (json) | Per-utterance / per-file rows that every drill-down sorts and filters. |
| `job` | id, type, payload, status, required_services[], attempts, error, progress | Worker queue. |

**Utterance unit:** if references have segment-level text, an utterance is a reference segment; otherwise it is the whole file.

---

## 5. Phase 1 — milestones

Each milestone ends in something runnable. The riskiest parts come first: metric correctness, then service orchestration.

**KWS hooks (Q6).** These are kept throughout Phase 1 so KWS can be added later as a new adapter, parser, metric module and tab, with no schema or layout changes:
- `kws` in the label-type, capability and run-task enums;
- the `keyword_event` table;
- the parser, metric and adapter registries are keyed by task;
- the benchmark tab bar is built from a task list, so KWS is one more entry.

Nothing KWS-specific is built, shown or tested in Phase 1.

### M0 — Scaffold and fixtures
- uv project, Justfile, FastAPI app, SQLite + Alembic, settings (`CONCORD_DATA`, `FEATURE_IMPROVE`, `PLATFORM_DIR`, `AICORE_ENDPOINTS`, `AI_HOST`).
- Vite app shell: AeroAudio tokens (HANDOFF §7) as CSS variables + Tailwind theme, Inter / JetBrains Mono (`tabular-nums` on a `.num` class), left nav (Evaluate, Label, Training*, Bring your own), feature-flag plumbing, import-linter. Dockerfile with `lite` / `full` targets.
- **Synthesised fixtures (Q10).** `just fixtures` runs `fixtures/synth/make.py`, which needs `kokoro-tts` up:
  - Dialogue specs (YAML) list turns: speaker → Kokoro voice, text, start offset. Overlaps and pauses are written in the spec, so references are exact by construction.
  - Each turn is synthesised alone (24 kHz), then mixed at its offset, with optional low-level noise.
  - Writes references in every supported format: RTTM, JSON segments, STM, SegLST, JSONL/CSV/`.txt` transcripts.
  - **Check clip:** ~10 s, two voices, one short overlap, known transcript and turns.
  - **Fixture dataset:** about 6 files of 20–90 s, 2–3 speakers. Ingest edge cases come from conversion, not synthesis: one 8 kHz file, one 44.1 kHz stereo MP3, one OGG, one clipped file, one corrupt file, one file over 5 min (to test the flag).
  - Generated files are committed (a few MB), so tests never need the platform.
  - Recorded model responses per adapter are captured once against live services and committed, so adapter tests need no GPU (same approach as the platform's custom-service tests).
  - Caveat: TTS speech is cleaner than real speech, so these fixtures check correctness, not realism.
- Exit: `just dev` serves the empty shell; `just test` and `just lint` pass; `just fixtures` regenerates the same files.

### M1 — Metrics core (pure functions, no I/O)
- `normalisers`: none, **basic (default)**, Whisper English; plus any platform `text.transform` service. The normaliser id and version are stored on every score.
- `wer` (jiwer): WER, CER, S/D/I/N, word alignment ops for inline diffs.
- `der`/`jer` (pyannote.metrics): missed / FA / confusion, collar default 0.25 s, optional skip-overlap; optimal speaker mapping returned for the UI.
- `vad`: missed speech + false-alarm speech (speaker-agnostic DER).
- `cpwer` (meeteval): per-file cpWER with the speaker permutation and per-word errors, including wrong-speaker words, which count twice.
- `rtf`: per stage, summed for pipelines.
- `attribution`: see M4.
- Exit: tests compare every number to the reference library called directly on the fixture set (half of acceptance item 2).

### M2 — Datasets and upload (`Upload-Dataset`)
- Ingest: folder/zip upload (chunked) or a local folder path. The design's "bucket URI" field becomes "local path" (Q2). Originals are copied into `CONCORD_DATA/originals/<sha256>.<ext>`, so the source folder can move or disappear. SHA-256, ffmpeg probe/decode, clipping check, 16 kHz mono derive, peaks. Runs as a job with progress.
- Report: file count, hours, sample-rate histogram, clipped / undecodable / **over 5 min** lists. Over-length files are stored but excluded from runs by default.
- Annotation parsers, matched by file stem:
  - ASR: JSONL (`audio_filepath`, `text`), CSV, one `.txt` per file.
  - Diarisation: RTTM, JSON segments.
  - Speaker-attributed transcripts, needed for cpWER (Q7): JSON/JSONL segments `{file, start, end, speaker, text}`, STM, SegLST.
- Match counts, unmatched list, 3-row preview per task, downloadable templates (static files, generated alongside the fixtures).
- Tags: free-form input with suggestions from `GET /api/tags`.
- Audio-only mode stores the dataset; the "pseudo-label after upload" option is hidden until Phase 2.
- Benchmark-plan side panel is added in M5 (it needs models).
- Exit: the fixture dataset uploads via UI and API with correct counts, previews and flags.

### M3 — Registry and model registration (`Add-Model`, `Model-Contract`)
- `registry.list()`: `aicore.available()` → dedupe by `name` → join with registered models. Entries are sorted by relevance from `tasks` (speech-recognition, voice-activity-detection, speaker-diarization first), but anything audio-capable can be checked.
- Read-only display: contract, api, engine, base URL, image tag, signature, description, VRAM share.
- Live status per entry (`/health`): up / down / loading. The check starts the service if it is down (via M4's `ensure_services`, which also lands here).
- **Compatibility check** on the synthesised two-speaker check clip. Rules:

  | Registry entry | Probe | Result |
  |---|---|---|
  | `openai` / `audio.transcriptions` | `verbose_json` + `timestamp_granularities[]=word` | `words[]` → ASR, word times. `segments[]` only → segment times. Text only → text only. Empty or garbage text (WER vs known transcript over a threshold) → warn. |
  | `openai` / `chat.completions` | Message with an `input_audio` part + "transcribe" instruction | Plausible transcript → ASR, text only. 4xx on the audio part, or text unrelated to the clip → fail: "audio part ignored — model can't take audio". |
  | `oip-v2` / `infer`, inputs `AUDIO`+`SAMPLE_RATE` | Infer on the clip | `SEGMENTS` only → VAD (segment times). `SEGMENTS`+`SPEAKERS` → diarisation; 2 speakers found → pass, otherwise warn. Segments outside clip bounds → fail. |
  | anything else (embeddings, speech, text.transform) | none | Not usable as a model. text.transform is offered as a normaliser instead. |

  Each check line is stored as pass/warn/fail with a plain-language reason. "Add" is blocked only if no capability was detected. A KWS row is added to this table when KWS lands.
- Form: registry pick + optional display name (default = registry name), tags (default = `tasks`), derived-from. At most 3 typed fields, usually none.
- Side panel: datasets ranked by tag overlap, filtered to those with labels for the detected tasks.
- Contract page: short static page covering what Concord sends and expects, the detection rules above, and links to `dev/README.md` and `platform/contracts/README.md`.
- Exit: register `whisper-large-v3`, `faster-whisper-large-v3`, `silero-vad`, `pyannote-diarization`, `nemotron-3-diarization` with zero typed fields.

### M4 — Run engine (the core)
- **`ensure_services(names)`**: probe `/health` for each; for any that are down, run `stack.py up <names>` (default eviction) in a subprocess and stream its output to the job log. The worker batches queued jobs so each batch's service set fits under `util_limit` (using `gpu_fraction` from the endpoint map), which keeps eviction to the minimum. If the platform tool fails (no Docker, missing weights), the job is marked `blocked` with the tool's message.
- **Adapters** (one interface: `run(audio, span?) -> ModelOutput`, built on `aicore.client(name)`):
  - `openai_asr`: multipart file of the derived 16 kHz audio (or a segment cut), best timing level the check found.
  - `openai_chat_audio`: base64 `input_audio`, text only.
  - `oip_segmenter`: numpy → `OIPClient.infer`; returns segments (+ speaker ints mapped to `spk0..`). Measure request time and memory once on a 5 min file.
- **Output cache** (`model_output`): every call checks the cache first. This makes the grid cheap (each seg model runs once per file, each ASR model once per segment set), and Phase 2 reuses it.
- **Single-model runs**: ASR on whole files (5 min max, so no client-side chunking) or on reference segments when the dataset is segment-level; VAD/diar on whole files. Retries with backoff on 503.
- **Pipeline runs** (seg → ASR):
  - Cut audio on the seg model's segments (merge segments shorter than ~0.5 s, pad 0.1 s), transcribe each, and build a speaker-attributed hypothesis.
  - Speech-only seg models give an unattributed hypothesis: WER only, cpWER `n/a`.
  - The oracle seg stage uses the dataset's own segments and speakers.
- **Error attribution (Q8).** Three cpWER values per (seg model, ASR model, file):
  - A = cpWER(oracle segments + oracle speakers → ASR)
  - B = cpWER(model segments, speakers set to the reference speaker with the largest overlap → ASR)
  - C = cpWER(model segments + model speakers → ASR), the reported score
  - ASR = A, segmentation = B − A, speaker attribution = C − B; the "with reference segments" counterfactual = A.
  - B and C reuse the same ASR outputs, so attribution costs no inference beyond the oracle row. Per-file negatives are clipped to 0 in charts and shown raw in tables, with a note on the page.
- **Scoring job** after inference writes `score` + `result` rows for the default normaliser and collar. Other normalisers and collars are scored on demand (seconds, no inference).
- Exit: from a cold platform (nothing running), the full grid (3 seg models incl. oracle × 2 ASR) on the fixtures runs from the API, starting the services it needs. Numbers match the M1 reference tests and RTF is recorded.

### M5 — Benchmarks UI (`Main`, `Benchmarks-Pipelines`)
- Tabs: **ASR · Diarisation · ASR + diarisation pipelines** (the Keyword spotting tab is added with KWS).
- Dataset picker: search + toggle chips; datasets without labels for the tab are dashed and listed under "hidden from this table".
- Single-task tables:
  - model × dataset, best per column highlighted;
  - Δ vs the previous score for the same (model, dataset, normaliser);
  - "—" + **Run** in empty cells (enqueues a job; services start automatically);
  - normaliser selector (ASR) and collar selector (DER) rescore through the API.
- Side panel: S/D/I or missed/FA/confusion for the selected cell, with a link to its error page.
- Pipelines tab:
  - ablation grid: rows = seg models incl. oracle, columns = ASR models; cpWER/WER toggle; speech-only rows show `n/a` for cpWER;
  - all-runs table: ranked by cpWER, then speech-only runs by WER; columns WER, DER, RTF;
  - detail panel: chain, cpWER, attribution bar, "with reference segments" counterfactual.
- **Suggestions**: one tag-overlap ranking shared by two panels:
  - the Upload benchmark-plan panel: models grouped by stage, pre-ticked on tag match, oracle under VAD/diar when speaker labels exist, "Run as" grid or each stage alone, button count "Upload and run N pipelines";
  - the Add-model datasets panel.
- Exit: every number on the benchmark screens traces to a `score` row; Run buttons work from a cold platform.

### M6 — Error analysis (`ASR-Errors`, `Diar-Errors`, `Pipeline-Errors`)
Build the shared components first; Phase 2 reuses them.
- Components:
  - `SortableTable` (virtualised; server-side sort/filter/search);
  - `DiffText` (inline +ins / −del / ref→hyp);
  - `AudioPlayer` (range requests, play-from, region markers);
  - `Timeline` (waveform peaks + per-speaker lanes + error lane + overlap hatch);
  - `MetricTile` (value + Δ arrow);
  - stage colour legend.
- **ASR**:
  - Tiles: WER, CER, S, D, I with Δ vs a comparison model.
  - Utterance table: ref vs hyp diff, N/S/D/I/WER, sort any column, text search, "only with errors".
  - Expanded row: player with error regions (word-timed models only, otherwise whole-utterance); alignment grid; error list with play-from; other models' output for the same utterance (from the cache); "Open in annotator" (disabled until Phase 2); "Flag reference as wrong" (sets `segment.flagged`).
  - Below the table: top mis-transcribed words (click to filter), top substitution pairs, WER by slice (dataset tags/metadata, duration buckets, speaker).
- **VAD / diarisation**:
  - Model switcher.
  - Tiles: DER split, JER, speaker-count accuracy (or the VAD equivalents).
  - File table. Expanded file: reference vs model speaker lanes, error lane (rose/amber/violet), speaker mapping, largest errors.
  - Cards: error by condition, turns found by length, boundary-offset histogram.
- **Pipeline**:
  - Every cpWER error assigned to ASR (sky), segmentation (emerald) or speaker (violet), with a note that wrong-speaker words count twice.
  - "If one stage were perfect" counterfactuals and wrong-speaker word pairs.
  - File table. Expanded file: lanes + side-by-side speaker-attributed transcript.
- Exit: every view loads, sorts, expands and plays audio on fixtures (acceptance item 3).

### M7 — Acceptance and hardening
- Scripted acceptance run (HANDOFF §3.7) as an end-to-end test against recorded responses, plus one manual run against live services on the 5090.
- Performance check at realistic size: a few hundred 5 min files. Table queries under 300 ms; scoring 1,000 utterances under 10 s.
- Both images build. `concord-lite` has no Training nav, buttons or routes, and nothing else breaks.

**Rough sizing (one developer):** M0 3–4 d · M1 3–4 d · M2 4–5 d · M3 4–5 d · M4 7–9 d · M5 5–7 d · M6 10–12 d · M7 3 d. About 8–9 weeks. M1 can run alongside M2/M3, and M6 component work can start once M4's result schema is fixed.

---

## 6. Phase 2 — Label (outline)

Builds on M4's output cache and M6's timeline/table components.

1. **Review console**: one dataset table with a stacked status bar from `segment.status` counts, next action, filters, search.
2. **Ensemble pseudo-labelling**:
   - Choose tasks and ASR members, with a cost preview from the cache ("Re-fuse 4 saved outputs" vs "Run 1 new model, then fuse 5").
   - Fusion strategies: best-aligned single hypothesis (lowest summed WER to the other members); ROVER (word-timed members only, others fall back to best-aligned); single model.
   - Confidence = member agreement, with configurable thresholds (0.85 / 0.50). Writes `segment` rows with `pseudo`/`auto` status.
   - Results: agreement matrix and review queue.
3. **Annotate**:
   - Focus switch and whole-file timeline (per-speaker lanes, overlap hatch, flags lane).
   - Segment grid with multi-select and bulk bar, inline editing.
   - Shortcuts: Space, Tab, 1–8, V, M, Enter. Autosave per segment.
4. **Live evaluation**:
   - WER vs confirmed count (hidden under 30), bootstrap 95% CIs, standings next to the locked benchmark score, DER card, confirmations feed.
   - "Promote to benchmark dataset" copies confirmed segments into a new dataset. "Use for retraining" appears in `concord-full` only.
5. **Import / export**:
   - Package format exactly as HANDOFF §4; fingerprint matching.
   - Merge rules: reviewed work is never overwritten by pseudo-labels; segments both sides reviewed differently go to a conflict list.
   - Snapshot before merge, with undo; history.

**KWS in Phase 2**: the keyword lane, *Keywords* focus, keyword chips and KWS cards depend on the KWS decision. Revisit before Phase 2 starts. Until then, the package format keeps `keywords.jsonl` (empty) so packages stay forward-compatible.

Rule kept from day one: confirmed and auto segments live in `segment` with `source_run_id`, so Improve can read them without Label importing Improve.

---

## 7. Phase 3 — Improve (on hold)

Separate `concord_improve` package and router, in `concord-full` only. It reads `segment`, `dataset` and `model`, and owns `training_run`. Scope and launch mechanism are unanswered (Q5), so nothing is built until the owner confirms.

---

## 8. Testing

- **Metrics**: golden tests against the reference libraries on the synthesised fixtures (M1).
- **Adapters**: recorded responses per service (no GPU); a `live` marker runs them against real services, started through `ensure_services`.
- **Orchestration**: `ensure_services` with `stack.py` mocked (batching, blocked state, log streaming), plus one live test.
- **Parsers**: one fixture per format, generated by `just fixtures`, plus malformed files.
- **API**: FastAPI TestClient end-to-end on the fixture set.
- **Frontend**: Vitest for components (DiffText, Timeline maths); Playwright smoke test per screen for acceptance item 3.
- **Architecture**: import-linter (no Improve imports); a lite-image test that the app starts and every Phase 1/2 route works.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Starting services from Concord evicts something the owner is using (e.g. a chat model) | Benchmark sets are small (≈0.34 of the GPU), so eviction is rare. The job log names every service stopped. A setting switches Concord to `--no-evict` (jobs then wait as `blocked`). |
| Concord in a container can't reach Docker | Documented mounts (§2); in development Concord runs on the host. |
| TTS fixtures are cleaner than real speech | Used for correctness only. The M7 manual run uses one real dataset the owner supplies. |
| cpWER needs speaker-attributed references that plain ASR + RTTM uploads don't provide | The three formats from Q7. For RTTM + per-file text only, cpWER shows as unavailable rather than guessed. |
| Attribution can go negative on a file (B < A) | Clipped in charts, raw in tables, documented on the page. |
| ASR servers differ on long input | Capped at 5 min, which every current ASR service handles in one request. Behaviour is recorded per run. |

---

## 10. Still open

| # | Question | Default meanwhile |
|---|---|---|
| Q5 | Phase 3 scope and how training jobs are launched. | Phase 3 not started. |
| Q6b | When KWS comes in, and whether the platform gets a KWS OIP signature or KWS is first scored from ASR word output. | Hooks only (§5). |
