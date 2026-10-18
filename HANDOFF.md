# Concord — Development Handoff

A single application for evaluating, labelling and (later) improving speech models: ASR, VAD / speaker diarisation, and keyword spotting (KWS), on datasets the user uploads.

This file is the brief for building it. The interactive design is the visual reference; this file is the behavioural reference. Where they disagree, this file wins.

---

## 0. Read these first

| What | Where | Why |
|---|---|---|
| **Personal AI service platform — `README.md`** | The separate platform repository | **Source of truth for model API contracts and how model services are spun up.** Each model's server, protocol, route and launch settings come from that repo's per-model config. Do not re-implement or duplicate any of it here. |
| Design canvas | https://claude.ai/code/artifact/0e026853-3c35-482e-9cb8-30605607c086 (private to the owner) | Every screen, clickable in Play mode. Sample data only. |
| Design system | `speech_recognition_platform_design_system.md` (AeroAudio DS) | Colours, type, track layout, keyboard shortcuts. Summary in §7. |

### Model registration must be near-zero effort

The owner registers models personally and does not want to fill in many fields. Concord therefore **reads** model definitions from the platform repo's config instead of asking for them.

- Registering a model = **pick an entry from the platform registry** (+ optional display name and tags). That's it.
- Protocol, base URL, route, model id, auth, and server type all come from the platform config.
- Capabilities (ASR / VAD / diarisation / KWS) and timing level (word times / segment times / text only) are **detected** by the compatibility check, not typed in.
- Display name defaults to the registry name; tags default to any tags in the platform config.
- If the platform config and anything in this document or the design disagree about contracts, **the platform README wins**.

> The design's *Add model* and *Model service contract* artboards show server presets, image fields and a full contract page. Treat those as illustrations of what the check can detect and what the protocols look like. In the real app, Add model is a registry picker plus the compatibility check, and the contract page becomes a short page that links to the platform README.

---

## 1. Delivery order

| Phase | Scope (canvas row) | Outcome |
|---|---|---|
| **1 — MVP** | **1 · Evaluate** + **4 · Bring your own** | Upload datasets, register models from the platform, run benchmarks (single models and VAD/diarisation × ASR pipelines), drill into errors. Usable on its own. |
| **2** | **2 · Label** | Pseudo-label unlabelled audio with model ensembles, annotate, watch live metrics, import/export progress between installs. |
| **3** | **3 · Improve** | Retraining / fine-tuning. **Design is incomplete and will change**; build last and keep it isolated. |

### Two shipping variants (plan for this from day one)

The owner intends to ship two variants:

1. **Lite** — everything **except** 3 · Improve (no training infrastructure; lighter to deploy).
2. **Full** — Lite **plus** 3 · Improve.

Implementation rules so this stays cheap:

- Improve is a **separate module** behind a build-time/deploy-time flag (e.g. `FEATURE_IMPROVE`). Evaluate, Bring-your-own and Label must not import from it.
- With the flag off: hide the *Training* nav item, the "Use for retraining" / "Retrain the diariser" buttons (Live evaluation, Pipeline error analysis, Annotate side panels), and any training-related API routes. Nothing else should break.
- Data written by Label (confirmed segments, pseudo-labels) is the input Improve will consume later; keep it in a format Improve can read without Label depending on Improve.

---

## 2. Concepts and decisions already made

These came out of design review. Do not re-litigate them without the owner.

**Datasets**
- Every upload is its own dataset. **No train/dev/test splits inside a dataset.**
- Two upload modes: *audio + annotations* (any mix of ASR transcripts, speaker labels/RTTM, keyword events) and *audio only*.
- Datasets and models both carry **free-form tags** (e.g. `call-centre`, `en-SG`, `telephony-8k`). Tags drive suggestions: datasets suggest models to benchmark and vice versa, ranked by shared tags.

**Tasks and stages**
- **VAD and diarisation are one stage** ("VAD / diarisation"). A VAD is a diariser that returns no speakers. Speech-only models get VAD metrics and WER; speaker models get DER and cpWER.
- **A VAD's output is never fed into a diariser.** Each VAD/diarisation model is a complete first stage.
- A **pipeline** = one VAD/diarisation model → one ASR model. Benchmarks can run the full grid (*N* seg models × *M* ASR models) or each stage alone.
- **Oracle baselines:** "Reference segments + speakers" (the dataset's own labels) can stand in for the VAD/diarisation stage. This isolates ASR error and powers the error attribution views.

**Models that return only text** (e.g. audio LLMs via chat completions) are first-class: they're scored on text. Features needing timestamps (per-word audio, word-level ensemble voting) degrade gracefully. In pipelines the seg stage provides timing.

**Labelling**
- **Single annotator** for now. No user attribution, assignment or conflict between people. Multi-annotator is a future feature.
- Progress moves between installs by **manual package export/import**, including **partial** datasets. Files are matched by audio fingerprint (SHA-256).
- Merge rules on import: reviewed work is never overwritten by pseudo-labels. Segments both sides reviewed differently go to a **conflict list the user resolves**; there's no automatic "latest wins" yet. A snapshot is taken before merge so it can be undone.

---

## 3. Phase 1 — Evaluate + Bring your own (MVP)

Canvas artboards: `Main`, `Benchmarks-Pipelines`, `ASR-Errors`, `Diar-Errors`, `Pipeline-Errors`, `Upload-Dataset`, `Add-Model`, `Model-Contract`.

### 3.1 Upload dataset (`Upload-Dataset`)
- Mode choice: audio + annotations / audio only.
- Details: name, language, description, **tags** (free-form input + suggestions from tags already used in the project).
- Audio: folder/zip upload or bucket URI. Accept WAV, FLAC, MP3, OGG, M4A at any sample rate; keep originals, derive 16 kHz mono for models. Report file count, hours, sample rates, clipped/undecodable files.
- Annotations, per task: ASR (JSONL `audio_filepath,text`, CSV, or one `.txt` per file), diarisation (RTTM or JSON segments), KWS (CSV/JSON events). Match to audio by file name. Show match counts and a 3-row preview. Offer downloadable templates.
- **Benchmark plan** (side panel): models grouped by stage (VAD / diarisation, ASR, KWS), ranked by tag overlap and pre-ticked when tags match. The oracle option appears under VAD / diarisation when speaker labels exist. "Run as": full grid of pipelines or each stage alone. The button label reflects the count ("Upload and run 10 pipelines").
- Audio-only mode in Phase 1: store the dataset; the "After upload → pseudo-label" option can be hidden or disabled until Phase 2.

### 3.2 Register a model (`Add-Model`) — simplified for the platform
- **Pick from the platform registry** (searchable list of entries from the platform config). Show what the config says (protocol, server, endpoint) read-only.
- Optional: display name, tags, "derived from" (parent model, for lineage in benchmarks).
- **Compatibility check** runs on a ~10 s two-speaker reference clip and records:
  - detected capabilities (ASR / VAD / diarisation / KWS);
  - timing level (word times, segment times, text only);
  - pass/warn/fail per check, with plain-language reasons (e.g. "audio part ignored — model can't take audio").
- Block "Add" only if nothing usable was detected.
- Side panel: datasets to benchmark on, ranked by tag overlap, filtered to datasets that have labels for the detected tasks.

### 3.3 Benchmarks (`Main`, `Benchmarks-Pipelines`)
- Tabs: **ASR · Diarisation · Keyword spotting · ASR + diarisation pipelines**.
- **Dataset picker**: search + toggle chips for every dataset; datasets without labels for the current tab are dashed and listed as "hidden from this table".
- Single-task tabs: model × dataset table; best per dataset highlighted; Δ vs the previous run; "—" + **Run** when a model hasn't been evaluated on that dataset. Side panel shows the selected cell's breakdown (S/D/I for ASR; missed / false alarm / confusion for DER; hits / misses / false alarms for KWS) and links to the matching error-analysis page.
- Text normaliser selector for ASR (affects WER everywhere; store it with each run).
- **Pipelines tab:**
  - Ablation grid: rows = VAD/diarisation models (incl. oracle), columns = ASR models. Metric toggle: cpWER / WER. Speech-only rows show `n/a` for cpWER.
  - All-runs table ranked by cpWER (speech-only runs after, by WER) with WER, DER and RTF.
  - Detail panel: chain, cpWER, error attribution (ASR / segmentation / speaker attribution, each measured by swapping that stage for the oracle), and a "with reference segments" counterfactual.

### 3.4 Error analysis (shared ASR · VAD/diarisation · Pipeline switcher)
- **ASR** (`ASR-Errors`):
  - Summary tiles (WER, CER, S, D, I with Δ vs a comparison model).
  - **The centrepiece is a sortable utterance table**: reference vs hypothesis with inline S/D/I, N, S, D, I and WER. Sortable by any column; text search; "only with errors".
  - Expand a row for an audio player with error regions marked, word-alignment grid, an error list with play-from buttons, other models' output for the same utterance, "Open in annotator" and "Flag reference as wrong".
  - Below the table: most mis-transcribed words (click to filter the table), top substitution pairs, WER by slice.
- **VAD / diarisation** (`Diar-Errors`):
  - Model switcher (speaker models vs speech-only).
  - Tiles: DER split, JER, speaker-count accuracy, or the VAD equivalents.
  - Sortable file table. Expand a file for reference vs model speaker lanes, an error lane (missed = rose, false alarm = amber, confusion = violet), the speaker mapping, and the largest errors.
  - Cards: error by condition, turns found by length, boundary-offset histogram.
- **Pipeline** (`Pipeline-Errors`):
  - All cpWER errors traced to a stage, consistently coloured: ASR (sky), segmentation (emerald), speaker (violet). Wrong-speaker words count twice in cpWER; say so in the UI.
  - "If one stage were perfect" counterfactuals and wrong-speaker word pairs.
  - Sortable file table. Expand a file for speaker lanes plus a side-by-side speaker-attributed transcript excerpt.

### 3.5 Contract page (`Model-Contract`)
Replace the long design page with a short page:
- what Concord sends and expects;
- the capability / timing detection rules;
- a link to the **platform README** for everything else.

The design version documents both protocols (OpenAI-compatible and KServe OIP v2, with fixed tensor names). Keep that page only as reference for what the compatibility check must recognise, and only if the platform README doesn't already cover it.

### 3.6 Metric definitions (implement once, use everywhere)
| Metric | Definition / notes | Suggested library |
|---|---|---|
| WER, CER, S/D/I | After the selected text normaliser | `jiwer` |
| DER | Missed + false alarm + confusion; 0.25 s collar by default (selectable) | `pyannote.metrics` |
| JER | Per-speaker Jaccard error | `pyannote.metrics` |
| cpWER | Concatenated minimum-permutation WER (speaker-attributed) | `meeteval` |
| VAD error | Missed speech + false-alarm speech | — |
| KWS | F1, precision, recall, FA/hour at the operating threshold; DET curve from threshold 0 | — |
| RTF | Processing time ÷ audio duration, per stage and summed for pipelines | — |

Store per-utterance/per-file results, not just aggregates: every drill-down view sorts and filters them.

### 3.7 Phase 1 acceptance
- Upload a labelled dataset; register ≥2 ASR models and ≥2 VAD/diarisation models from the platform registry with ≤3 fields typed.
- Run single-model benchmarks and a full pipeline grid incl. oracle; numbers match the reference libraries on a fixture set.
- Every error-analysis view loads, sorts, expands and plays audio for the fixture set.

---

## 4. Phase 2 — Label

Canvas artboards: `Label-Console`, `Pseudo-Label`, `Annotate`, `Live-Eval`, `Sync-Progress`.

- **Review console**: one table of datasets with label source, tasks, a stacked progress bar (confirmed / auto-accepted / needs review / rejected / running / not labelled) and the next action per row. Status filter and name/#tag search. "Import / export progress" and "Upload dataset" in the header.
- **Ensemble pseudo-labelling** (no batch selector; the dataset comes from the console):
  - Choose tasks.
  - Choose ASR models from the registry. Models that already ran on this dataset reuse **saved outputs** at no cost; only new ones need inference. The button reflects this ("Re-fuse 4 saved outputs" vs "Run 1 new model, then fuse 5").
  - Starting hypothesis: best-aligned single hypothesis (default), ROVER fusion, or one model.
  - Confidence triage thresholds: ≥0.85 auto-accept, 0.50–0.84 review, <0.50 reject. Auto-accepted labels can feed training but never become a benchmark reference unless a person confirms them.
  - Results: triage bar, member-agreement matrix (pairwise WER), share chosen as starting hypothesis, review queue.
- **Annotate** (annotation only; no metrics on this screen):
  - Focus switch: *Transcript + speakers* / *Keywords* / *Both*. It hides the lanes and columns that aren't in focus.
  - Whole-file timeline: waveform (no spectrogram), **one lane per speaker** so overlaps are visible (overlaps hatched across speaker lanes), keyword lane, flags lane.
  - **Segment grid with multi-select** (checkboxes, shift-click ranges) and a bulk bar: assign speaker, merge, accept/reject keywords, confirm. Inline transcript editing, speaker pill cycling, keyword chips cycling, "Confirm all high-confidence".
  - Keyboard shortcuts per §7.
- **Live evaluation** (separate screen): WER of each model vs number of confirmed segments, with "too few to rank" below 30. Standings with bootstrap 95% intervals next to the locked-benchmark score; DER and KWS cards; latest confirmations feed. "Promote to benchmark dataset" (and "Use for retraining" in Full only).
- **Import / export progress**:
  - Export: dataset; changed-since-last-export / all / picked files; include pseudo-labels, raw model outputs, audio (optional).
  - Import: package summary; merge target suggested by fingerprint match; the segment-level outcome table with rules; the conflict resolver; a snapshot before merge; history.
  - Package = plain files: `manifest.json`, `files.csv` (path, sha256, duration, sample rate), `segments.jsonl` (file_sha256, start, end, speaker, text, status, confidence, source, updated_at), `keywords.jsonl`, optional `runs/`, optional `audio/`.

---

## 5. Phase 3 — Improve (incomplete; Full variant only)

Canvas artboard: `Training`. Current design, expected to change:
- New run for ASR / diarisation / KWS:
  - base model;
  - data mix (confirmed annotations, auto-accepted pseudo-labels at reduced weight, existing datasets);
  - replay of base-domain data;
  - evaluate-on datasets with a leakage check;
  - method / LR / epochs / compute.
- Automation: retrain when confirmed data grows by *X* h; promote only if every evaluation dataset improves.
- Runs: live run (loss curves, checkpoint dev metrics) and completed runs with Δ vs parent and promoted / held-back status.

How training jobs are launched should follow the platform repo's conventions; confirm with the owner before building.

---

## 6. Suggested domain model

`Project` → `Dataset` (tags, label types) → `AudioFile` (sha256, duration) → `Segment` (start, end, speaker?, text?, status: pseudo | auto | confirmed | rejected, confidence, source_run, updated_at) + `KeywordEvent`.
`Model` (platform registry key, display name, tags, parent, detected capabilities, timing level) → `EvalRun` (model or pipeline, dataset, normaliser, collar, created_at) → per-file / per-utterance `Result` rows → aggregates.
`Pipeline` (seg model or oracle, ASR model). `PseudoLabelRun` (members, strategy, thresholds; reuses saved `ModelOutput` per file). `SyncPackage` (direction, scope, counts, snapshot id).
Phase 3 adds `TrainingRun`; nothing above may reference it.

---

## 7. Design system essentials (AeroAudio DS)

- Dark slate UI:
  - canvas `#0A0F1D`; panel `#111827`; track `#1E293B`; active row `#283548`; borders `#334155`;
  - text `#F8FAFC` / `#94A3B8` / `#64748B`;
  - focus / playhead `#38BDF8`; primary action `#06B6D4`; secondary / comparison `#6366F1`.
- Fonts: Inter for UI; **JetBrains Mono with tabular numbers for all metrics, times and tokens**.
- Speakers, up to 8: teal `#14B8A6`, orange `#F97316`, violet `#A855F7`, yellow `#FACC15`, sky `#38BDF8`, fuchsia `#EC4899`, lime `#84CC16`, lavender `#8B5CF6`. Overlap is a crimson 45° hatch. Never recolour waveform peaks per speaker.
- Confidence: ≥0.85 emerald `#10B981`, 0.50–0.84 amber `#F59E0B`, <0.50 rose `#F43F5E`, pending slate `#64748B`.
- ASR diffs: insertion emerald `+tok`, deletion rose strike-through `−tok`, substitution amber `ref → hyp`. DER: missed rose, false alarm amber, confusion violet. Improvement emerald ↓, degradation rose ↑.
- Shortcuts: Space play/pause · Tab next low-confidence · 1–8 assign speaker · V split · M merge · Enter confirm.

---

## 8. Open questions for the owner

1. Exact field names and lookup API of the platform registry. Read the platform README; ask if registry discovery isn't documented there.
2. Where audio and results live (local disk vs object store) and expected dataset sizes.
3. Whether benchmark jobs are queued and run by the platform or by this app.
4. Variant naming and packaging (separate images vs one image with a flag).
5. Phase 3 scope and how training jobs are launched.

All numbers, model names, dataset names and transcripts in the design are illustrative sample data.
