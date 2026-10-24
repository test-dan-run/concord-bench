# Concord Bench: Evaluate redesign

Planned 2026-10-23 and 2026-10-24. Kept apart from [PLAN.md](PLAN.md) and [SPLIT_PLAN.md](SPLIT_PLAN.md).
Section numbers without a file name are in this document.

The design canvas shows the revised Evaluate screens (benchmarks and error analysis) and the parts of the
Bring your own screens that change with them. Every screen is clickable, and all data on it is sample data.

| What | Where |
|---|---|
| Design canvas | https://claude.ai/artifact/HN5frmSLm82zUCZ8hn32vP (private to the owner) |
| Evaluate row | `Main` (Benchmarks ASR), `Benchmarks-Diarisation`, `Benchmarks-Pipelines`, `ASR-Errors`, `ASR-Label-Slices`, `Diar-Deep-Concept`, `Enhance-Deep-Concept`, `Pipeline-Deep-Concept` |
| Bring your own row | `Upload-Dataset`, `Import-References`, `Datasets`, `Dataset-Detail`, `Models` |
| Shared | `Jobs`, `Nav` (the sidebar) |

The redesign adds four things the app does not have today:

- **Speech enhancement as an ablation.** An enhancement model writes a cleaned copy of a dataset, and every
  ASR and diarisation model can then be scored on the original audio and on each cleaned copy. In
  pipelines, the cleaned copy goes in before or after diarisation.
- **Richer ASR metrics and compute details.** MER, WIL and WIP next to WER and CER, and the hardware each
  run used.
- **Slicing by label.** Scores broken down by attributes that come with the references, e.g., gender or
  ethnicity in a NeMo manifest.
- **Deeper error analysis** for diarisation, pipelines and enhancement.

The work is split into milestones E0 to E7, plus one platform milestone P1 (§10). The owner's decisions are
in §1, and the few questions still open are in §12. As of 2026-10-24, every Bench milestone is built except
E5b, and §14 lists where the build differs from this plan.

---

## 1. Decisions

Made by the owner on 2026-10-23 and 2026-10-24. Each one is worked into the section it shapes.

| Topic | Decision | Section |
|---|---|---|
| Offline export | Remove all of it from Bench: the page, the API, the `offline/` module, the CLI commands, the export job and table, and their tests. Leave Concord Label's copy alone, because the Label repository is being edited. | §9 |
| Where labels come from | Labels come with the references, as extra fields on each record. For example, a NeMo manifest line can carry `"attribute": {"gender": ..., "ethnicity": ...}`. Later, the reference package carries them too. There is no separate label file. | §7.1, §7.2 |
| Dataset updates | Not in this plan. A dataset that needs new labels or references is uploaded again as a new dataset, and the old dataset keeps its scores. | §7.1 |
| Hardware details per run | They come from the model catalog, with as much as the catalog knows. Bench does not probe the hardware itself. The platform repository was read on 2026-10-24 to see what the catalog reports today. | §3.4 |
| Platform fields | The platform adds the hardware fields and the generative flag (P1) in its own time. Bench doesn't wait for them, and shows each field once the catalog returns it. | §3.4, §5.1 |
| Enhancement contract | The platform already has it. An enhancer is an `oip-v2` service with AUDIO and SAMPLE_RATE in and AUDIO out, and the catalog verifies it. Bench needs no new contract. | §5.1 |
| Generative enhancers | Add a `generative` flag to the catalog's model record (part of P1). Bench reads it, marks the enhancer and its runs, and treats a missing flag as false. | §5.1, §6 |
| Enhancement quality metrics | Not built for now. That covers DNSMOS (SIG, BAK, OVRL), the WADA-SNR estimate, and SI-SDR and PESQ on Bench's own datasets. | §6 |
| Saved slices | A saved slice belongs to the one dataset it was made on. | §7.4 |
| Features the design leaves out | Keep them. That covers the diarisation files table, the boundary-offset histogram, "turns found", "If one stage were perfect" and "Words given to the wrong speaker". | §8 |

---

## 2. Where the build stands against the design

Read from the working tree on 2026-10-23, including the uncommitted model-catalog changes.

| Area | Design | Today | Work |
|---|---|---|---|
| ASR metrics | WER, CER, MER, WIL, WIP, S, D, I, hits | WER, CER, S, D, I (`metrics/wer.py`); hits stored but not returned | §3.1 |
| Pipeline metrics | cpWER, WER, DER, tcpWER with a collar | cpWER, WER, DER (`metrics/cpwer.py`, `runs/scoring.py`) | §3.2 |
| Overlap scoring | "Score overlapping speech" toggle | `skip_overlap` exists in `metrics/der.py` but nothing sets it | §3.3 |
| Compute per run | GPU, peak VRAM, precision, batch size, CPU, node, driver, wall time, RTF | RTF per stage and the image only (`Run.rtf_by_stage`, `Run.builds`) | §3.4 |
| Run evaluation | Popup | Inline panel (`BenchmarksPage.tsx` `RunEvaluation`); no dialog component exists | §4 |
| Leaderboards | Sortable by any column, Mean column, metric picker, "All metrics, one dataset", hardware filter | Fixed order, one metric per tab | §4 |
| Speech enhancement | Everywhere | Nothing in Bench (no capability, task or table); the platform has two enhancers and verifies them | §5, §6 |
| Models page | Tabs for ASR, VAD / Diarisation, Enhancement | One table | §4 |
| Labels for slicing | Read at upload, Label view, "by label" on every error page | Flat extra fields become file metadata; nested objects are dropped (`datasets/parsers.py` `_record_from_obj`) | §7 |
| Diarisation errors | Regions, speaker-count and speaker-confusion matrices, tagged timeline, compute strip | Tiles, files table, timeline, condition, turns found, boundary offsets | §8.1 |
| Pipeline errors | Stage cards, waterfall to tcpWER, "Which stage to upgrade", "Where errors start", tagged files | Tiles, attribution bar, counterfactuals, files with transcripts | §8.2 |
| Jobs | "waiting for compute #N", leases | Built | none |
| Offline export | Gone from the sidebar | Page, API, module, CLI, job and tests | §9 |

Some details on the canvas are sample data that differ from the platform. The enhancers on the canvas
(frcrn-16k, demucs-dns64, sgmse-plus, mossformer2-se) don't exist; the platform has `deepfilternet3`
(denoising) and `wpe-dereverb` (dereverberation), and both run on CPU. The canvas Models page shows the
contract as "concord-enh / v1", and the build shows the catalog's real contract, "oip-v2 / infer".

---

## 3. Metrics and run data (E1)

### 3.1 MER, WIL and WIP

Compute the new ASR metrics from summed counts, and never as a mean of per-utterance values, so that they
agree with WER on the same corpus.

- N is the number of reference words, and P is the number of hypothesis words, so P = H + S + I.
- MER = (S + D + I) / (H + S + D + I).
- WIP = (H / N) x (H / P), and WIL = 1 - WIP.

Changes:

- `metrics/wer.py`: add `p` to `WerResult`, and add `mer`, `wil` and `wip` to `aggregate`. jiwer already
  returns per-utterance MER, WIL and WIP, so use them in the utterance table.
- `api/benchmarks.py` and `api/errors.py`: return `h`, `p`, `mer`, `wil` and `wip` wherever WER is returned,
  and accept `metric=wer|cer|mer|wil|wip` on `/benchmarks/asr`.
- Normalisers apply to all of them, as they do to WER.

### 3.2 tcpWER

tcpWER is cpWER with a time constraint, so a word only matches if it falls within a collar of the reference
word's time. meeteval computes it, and the design offers collars of 2, 5 and 10 seconds (default 5).

- `metrics/cpwer.py`: add `tcpwer(collar)`. Store it in pipeline scores, keyed by the collar the same way
  DER is keyed by its collar today (`Score` is unique per run, normaliser and collar).
- Timing error is tcpWER minus cpWER. The pipeline waterfall in §8.2 shows it as its own step.

### 3.3 Score overlapping speech

Wire the existing `skip_overlap` parameter to a query parameter on the diarisation endpoints, and add it to
the `Score` key so both versions can be stored. The default stays "score overlap" (today's behaviour).

### 3.4 Hardware and wall time from the model catalog

Bench shows the hardware each run used, taken from the catalog, and shows "-" for anything the catalog
doesn't report.

What the catalog reports today (platform repository, `catalog/README.md` and `catalog/app.py`, read
2026-10-24):

| Where | Fields | Useful for |
|---|---|---|
| Model record, `GET /api/models/{id}` | `gpu` (`fraction` and `gb`, the declared VRAM footprint, or `null` for CPU models), `engine`, `image`, `build_key`, `hosted` (a provider endpoint, or `null`), `capabilities.rtf` (from verification), `cold_start_s` | GPU or CPU, declared VRAM, engine, image, hosted or local |
| `GET /api/platform` | `gpu_total_gb` (the configured size of the one GPU, 32), `util_limit`, the models using the GPU now | The GPU's size |
| Lease, `POST /api/leases` | `id`, `holder`, `models`, `state`, `position`, `waiting_for`, `error`, timestamps | Nothing about hardware |

The platform runs on one host with one GPU, and it doesn't report the GPU's name, the driver or CUDA
version, the CPU, the host name, precision, batch size or measured peak VRAM. `config.yaml` names the GPU
(RTX 5090) only in a comment.

The work is split between the platform and Bench.

**Platform (P1).** These are requests to the platform repository, which the platform will add in its own
time (§1). Bench works without them. P1 also adds the `generative` flag (§5.1).

1. Add a `host` object to `GET /api/platform`, filled once at catalog start: GPU name, GPU count, driver
   version, CUDA version, CPU model, CPU core count, host name.
2. Add optional `runtime` fields to the service manifests, `precision` and `batch_size`, and return them on
   the model record. vLLM services can fill precision from their dtype.
3. Measure peak VRAM per model while a lease is active, and return the running peak as `peak_vram_gb`
   (model id to GB) on the lease. Bench reads it just before it releases the lease.

**Bench (E1).**

1. Add `Run.hardware` (JSON). When a run's lease becomes active, copy onto it, per model: GPU or CPU, the
   declared VRAM, the engine, the image, whether the model is hosted (and the provider host), and the
   `host` object and `runtime` fields when the platform has them. A pipeline stores one entry per stage.
2. Just before the lease is released, add `peak_vram_gb` per model if the lease reports it.
3. Add `Run.wall_s` (from `started_at` to `finished_at`), and keep cold start out of RTF as today. Cold
   start stays a catalog attribute of the model.
4. API: return `hardware` and `wall_s` with every leaderboard cell and inspector. The hardware filter runs
   in the page over the cells' `hardware.device`, so the API needs no filter parameter.

Accuracy compares fairly across hardware, and RTF does not. With one host, runs differ mainly by whether a
model ran on the GPU, on the CPU or at a hosted provider, so the ASR leaderboard's hardware filter offers
those three, and the GPU name once P1 reports it. The leaderboard warns when the shown rows mix them
(canvas `Main`).

---

## 4. UI changes that need no new backend (E2)

These follow the canvas and only use data the API already returns, or returns after E1.

- **Dialog component** in `components/ui.tsx`: modal, focus trap, Escape to close, return focus to the
  button that opened it. Every popup in this plan uses it.
- **Run evaluation popup** (`BenchmarksPage.tsx`): move `RunEvaluation` into the dialog. Keep `PlanPanel` as
  it is, with Dataset and Compute selects at the top and the run count and button in the footer.
- **Leaderboard sorting**: click any column header to sort, best first, and click again for worst first.
  Add a Mean column (blank when a dataset is missing) and an RTF column. Show "Sorted by ..." under the
  title.
- **ASR tab controls**: View (Across datasets, or All metrics on one dataset), Metric (WER, CER, MER, WIL,
  WIP), Dataset (in the one-dataset view) and Hardware (§3.4). Show the selected metric's name, formula and
  direction under the controls.
- **ASR inspector**: tiles for WER, CER, MER, WIL and WIP; a bar of hits, substitutions, deletions and
  insertions with N and P; a Compute panel with RTF, speed, wall time and the hardware fields from §3.4,
  each shown as "-" when missing; the existing "Recent evaluation runs".
- **Text normaliser**: segmented buttons instead of a select. The shared store in `lib/config.tsx` stays.
- **Models page**: tabs for ASR, VAD / Diarisation and Enhancement, with counts, and a sentence under the tabs
  about how each type is scored. The Enhancement tab is empty until E3. Show the declared VRAM (`gpu.gb`),
  which the API returns today but the page doesn't show.
- **ASR error analysis**: add the "Utterances" and "Label" sub-tabs. The Label tab arrives in E5.

---

## 5. Speech enhancement backend (E3)

An enhancement model takes audio and returns cleaned audio of the same length. Bench runs it once over a
dataset and keeps the result as an enhanced copy, and any ASR or diarisation run can then read that copy
instead of the original audio. The originals are never changed.

### 5.1 The platform's enhancement contract

The platform defines enhancers already, so Bench adds an adapter and no new contract.

- **Contract**: `oip-v2`, `api: infer`, with inputs `AUDIO` (FP32, one channel) and `SAMPLE_RATE` (INT32),
  and outputs `AUDIO` and `SAMPLE_RATE` (platform `contracts/README.md` §4, and
  `services/deepfilternet3.yaml` for an example signature).
- **Verification** (platform `catalog/verifier/probes.py` `probe_enhancer`): the catalog sends the check
  clip mixed with babble at 5 dB SNR (or a synthetic room response for dereverberation), and fails a model
  whose output has a different sample rate, a length more than 10 ms off, NaN values or clipping. It passes
  when SI-SDR against the clean clip improves, and warns when it doesn't.
- **Catalog record**: a verified enhancer has `capabilities.usable_as: "enhancer"`, `tasks: ["enhance"]` and
  `output_detail: ["enhanced_audio"]`, and `report.sample` holds `si_sdr_input_db`, `si_sdr_output_db` and
  `length_diff_samples` for the check clip.
- **Models today**: `deepfilternet3` (denoising) and `wpe-dereverb` (dereverberation), both on CPU.

Changes in Bench:

1. Add `enh` to `Capability` (`core/models.py`), and map the catalog's `enhance` task to it in
   `platform/catalog.py` (`tasks_of`, `stages_of`, `unusable_reason`).
2. Add `adapters/enhance.py`, built on `aicore.oip.OIPClient` the way `adapters/oip_segmenter.py` is. It
   sends 16 kHz mono audio and expects 16 kHz back.
3. Bench fixtures: a recorded or fake enhancer for tests, e.g., one that applies a fixed gain, and one that
   adds a few samples to test the length check.

A generative enhancer can shift timing or invent speech, so its scores need a warning. The catalog has no
field for this today, so P1 adds a `generative` boolean to the model record, declared in the service
manifest (§1). Bench copies it onto `Model` with the other catalog fields, treats a missing flag as false,
and marks each run that read a copy made by a generative enhancer. Neither platform enhancer today is
generative.

### 5.2 Enhanced copies

New tables:

| Table | Columns |
|---|---|
| `enhanced_copy` | `id`, `dataset_id`, `model_id`, `build_key`, `status` (building, ready, failed, cancelled, deleted), `progress`, `size_bytes`, `job_id`, `created_at`, `finished_at`, `length_check` (ok, trimmed, failed), `max_shift_ms` |
| `enhanced_file` | `id`, `copy_id`, `audio_file_id`, `rel_path`, `sha256`, `duration`, `shift_ms` |

Files live under `data/derived/enh/<copy_id>/`, with the same relative paths as the derived 16 kHz files.
A copy is reused by every run that asks for the same dataset, model and build key.

The `enhance` job builds a copy in these steps:

1. Lease the enhancement model from the catalog.
2. Send each usable file of the dataset. OIP sends audio as JSON tensors, so a 6 minute file is about 50 MB
   of JSON each way. Send files up to 60 s whole, and send longer files in 30 s chunks with 0.5 s of
   overlap, joined with a linear crossfade. Store the chunking in the copy's parameters, so a change to it
   gives a different copy.
3. Check the length. The catalog only verifies the length on its check clip, so Bench checks every file.
   If an output is within 10 ms (160 samples) of its input, pad or trim it to the exact length and record
   `shift_ms`. If it is further off, the file fails, and a copy with failed files is marked `failed` with the
   files named.
4. Write each output with its own sha256, then mark the copy `ready` with its size on disk and the largest
   shift.

The job reports progress the way ingest does, so the sidebar, Jobs and Datasets show "building 62%".
Cancelling a build stops the job and removes the partial files.

Deleting a copy removes its files and keeps the `enhanced_copy` row as `deleted`, so runs that used it still
name it and keep their scores. A new run that needs a deleted copy builds it again. Error analysis can't
play enhanced audio from a deleted copy, and says so.

### 5.3 Runs on an enhanced copy

- Add `Run.enh_copy_id` (null means original audio). A model with enhancement is a separate run, so scores,
  error analysis and the leaderboard need no other key.
- The output cache needs no change. It keys on the audio's sha256, and an enhanced file has its own sha256,
  so outputs on enhanced audio are cached like any other output.
- RTF of a run on an enhanced copy is shown as two parts, enhancer plus model. The enhancer part comes from
  the copy's build and is shared by every run that reads the copy.

### 5.4 Pipelines with enhancement

A pipeline gains an optional enhancement stage with a placement:

- **Before diarisation.** Diarisation and ASR both read the enhanced copy.
- **After diarisation.** Diarisation reads the original, and ASR transcribes the enhanced copy cut at the
  diarised segments. The copy has the same length as the original, so `prepare_cuts` can use the original's
  segment times on the enhanced file.

Add `Pipeline.enh_copy_id` and `Pipeline.enh_placement` (before, after or null). A diarisation output on the
enhanced copy is a different output from one on the original, which the sha256 already handles.

### 5.5 Planning and API

- `/benchmarks/plan` and `/benchmarks/plan/preview` take `enhancers` (catalog ids), `include_original` and,
  for pipelines, `placements`. The preview lists copies to build or to wait for, runs already scored (reused,
  not re-run) and new runs.
- Grid size is (number of VAD / diarisation models) x (number of ASR models) x (1 for the original + number
  of enhancers x number of placements).
- Runs that need a copy still building wait for it with `job.after`, as stages wait today.
- `GET /datasets/{id}/enhanced`, `POST /datasets/{id}/enhanced` with `{models}`, and
  `DELETE /enhanced/{id}` (deletes a ready copy or cancels a build).
- `/benchmarks/{tab}` takes `enh` (off, best, all) and the enhancers to show, and returns each row's change
  against the same model on original audio.

---

## 6. Enhancement UI (E4)

- **Benchmarks, ASR and Diarisation tabs** (canvas `Main`, `Benchmarks-Diarisation`):
  - Enhancement control with Off, Best per model and All variants; Arrange by Group by model or Rank all;
    one chip per enhancer.
  - Rows named "model + enhancer", with the change against original audio under each score.
  - "Enhancement effect" grid in the aside (enhancer by dataset, coloured by the change, click to inspect).
  - An enhancement section in the run popup, with "No enhancement" as the baseline and the copy status of
    each enhancer.
- **Benchmarks, Pipelines tab** (canvas `Benchmarks-Pipelines`):
  - A leaderboard of every enhancement, VAD / diarisation and ASR combination, with columns for placement,
    cpWER, change against original audio, WER, DER and RTF. Filters for each stage, top 15 with "Show all".
    Speech-only pipelines follow, ranked by WER.
  - An inspector for the selected pipeline, and the "How pipelines are scored" panel.
  - A "New pipeline run" popup with four columns (enhancement, placement, VAD / diarisation, ASR) and a
    footer with total, already scored and new.
  - The current ablation grid moves to the pipeline error page as "Which stage to upgrade" (§8.2).
- **Datasets** (canvas `Datasets`): an "Enhanced copies" column with ready and building chips, "+ Add" per
  dataset, and an "Add enhancement" popup with an estimate of the build time.
- **Dataset detail** (canvas `Dataset-Detail`): the "Enhanced copies" section with status, size, length check,
  runs that used it, Delete and Cancel build, and a line in the Report panel.
- **Upload** (canvas `Upload-Dataset`): a "Speech enhancement" fieldset in the plan and a placement choice
  for pipelines. The ingest job builds the ticked copies.
- **Models**: the Enhancement tab lists the enhancers with the catalog's verification result, including the
  check clip's SI-SDR before and after (from `report.sample`), and the length difference it measured. That
  SI-SDR is the catalog's number on its own clip, so it is labelled "on the check clip". A generative
  enhancer gets the "generative" chip from the canvas (§5.1).
- **Generative runs**: a "model + enhancer" row whose enhancer is generative shows the same chip on
  Benchmarks, Pipelines and the enhancement page, with a tooltip saying the enhancer can shift timing or
  invent speech.
- **Enhancement error analysis**, a new page at `/errors/enh/...` (canvas `Enhance-Deep-Concept`):
  - Placement cards (original, before, after) and the enhancer by placement grid for one pipeline.
  - "Listen, original vs enhanced": the clips where enhancement changed the transcript most, in either
    direction, with an A/B switch.
  - "Where the cpWER change comes from": the ASR part (the WER change) and the rest (segmentation and
    speaker attribution).
  - The enhancement models table with length shift, RTF, enhanced copy and best placement. The DNSMOS
    columns are left out (§1).
  - "Where enhancement helps" by label arrives with E5. By SNR is left out (§1).

---

## 7. Labels and slicing (E5)

### 7.1 Reading labels at upload

Any field on a reference record besides the file, times, speaker and text becomes a label.

- `datasets/parsers.py`: flatten nested objects to dotted keys, so `"attribute": {"gender": "F"}` becomes
  `attribute.gender`. Lists are ignored. Keep the existing list of NeMo fields that are not labels.
- `datasets/ingest.py`: store a record's labels on each of its segments (`Segment.meta`, which exists).
  A label whose value is the same on every record of a file is also stored on the file (`AudioFile.meta`).
  Today ingest merges every record onto the file, which is wrong for files with many segments.
- Decide the kind of each label from its values:
  - Discrete: text or true/false values, or numbers with at most 20 different values.
  - Continuous: numbers with more than 20 different values.
  - Not used: a different value on almost every record (an ID), or free text (long or mostly unique
    strings). The upload page shows these struck through with the reason.
- The upload summary lists the labels found, their values and how many records have a value (canvas
  `Upload-Dataset`, "Labels for slicing").
- Dataset detail shows the same list, read-only (canvas `Dataset-Detail`). Labels can't be edited on an
  existing dataset (§1).

The NSC-Scripted dataset in the local database lost its `attribute` fields when it was added, and Bench does
not keep the uploaded manifest. After this change, upload NSC-Scripted again as a new dataset to get gender
and ethnicity. The old dataset keeps its scores and can be deleted once the new one is benchmarked.

### 7.2 Labels in the reference package

The reference package (CONTRACTS.md §2) has no place for labels today. The contract is shared with Concord
Label and kept identical in both repositories, so this step waits until the Label repository is free.

1. Bump the reference package format to version 2 and keep reading version 1.
2. Add an optional `attributes` object to each record in `refs/segments.jsonl` and
   `refs/segments.seglst.json`.
3. Add an optional `refs/speakers.json`, `{"<file stem>": {"<speaker>": {attributes}}}`, for speaker labels
   such as gender or seat.
4. Update `contracts/samples/`, `contracts/check.py` and the Label exporter in the same sitting, and run
   `just check-contracts`.

### 7.3 Statistics

Put these in a new `analysis/slices.py`, seeded so the same data gives the same numbers.

- A 95% interval for each slice's score from 1000 bootstrap resamples. ASR resamples utterances and
  recomputes the corpus score from summed counts. Diarisation and pipelines resample files, because they're
  scored per file, so their intervals are wider.
- A slice is "reliably worse" when its interval lies wholly above the model's overall score.
- A slice with fewer than 20 utterances (or 5 files) is "too few to call".
- "Hard audio" means the comparison model is also reliably worse on the same slice.
- Continuous labels are binned by even width, by quantiles (equal counts) or by custom edges.
- Two-label heatmap cells with fewer than 10 utterances are not coloured.

Cache the results per run, normaliser and label, because the bootstrap is slow on large datasets.

### 7.4 Views

- **ASR Label view** (canvas `ASR-Label-Slices`): label chips (discrete and continuous) with the number of
  weak values, the "Where it fails" table, a chart for the chosen label, the two-label heatmap, and the
  selected slice with its worst utterances.
- **Saved slices**: "Save as a slice" stores a name and the label filters in a new `slice` table with a
  `dataset_id`, because a slice belongs to the one dataset it was made on (§1). On Benchmarks, a saved slice
  shows as an extra column next to its dataset's column, scored for every model from its existing results.
  Deleting the dataset deletes its slices.
- **Existing "WER by slice" card** on ASR errors reads segment labels as well as file metadata, and links
  to the Label view.
- **"By label"** sections on the diarisation and pipeline error pages (§8), and speaker labels in the
  diarisation "Who was mistaken for whom" matrix once the reference package carries them (§7.2).
- **"Where enhancement helps"** on the enhancement page, by any label.

---

## 8. Diarisation and pipeline error analysis (E6)

Every panel these pages have today stays (§1). The work below adds the panels from the canvas.

### 8.1 Diarisation (canvas `Diar-Deep-Concept`)

- Header: Collar, "Score overlapping speech" (§3.3) and Compare with.
- Tiles for DER, missed, false alarm, confusion, JER, VAD error and speaker count right, with the comparison
  model's value under each.
- A compute strip from the run's hardware (§3.4).
- **Where the errors are**: classify each moment of every file by the reference, as overlapped speech,
  within 0.5 s of a speaker change, a turn under 1 s, or non-speech. For each kind, show its share of speech,
  its share of errors, the lift (share of errors divided by share of speech), the error mix (missed, false
  alarm, confusion) and the DER inside it. Clicking a row lists its worst moments with a play button. The
  "Around events" rows on the canvas need event annotations, so they wait for labels that mark events.
- **How many speakers it finds**: files by true speaker count and found speaker count. The per-file counts
  exist already.
- **Who was mistaken for whom**: seconds of each reference speaker's speech by the model speaker it went to,
  per file or pooled, from the optimal mapping. Speaker labels show next to each reference speaker (§7.4).
- **Timeline errors tagged by cause**: each error region gets a type (missed, false alarm, confusion) and the
  kind of moment it falls in. The free-text notes on the canvas ("Cough while nobody was talking") are
  sample text and are not generated.

### 8.2 Pipeline (canvas `Pipeline-Deep-Concept`)

- Header: pipeline, normaliser and tcpWER collar (§3.2).
- **Stage cards**: enhancement (when used), VAD / diarisation, ASR and the output, each with its metric,
  hardware and RTF, and what it hands to the next stage, e.g., the number of segments and speakers.
- **How the errors add up**: a waterfall that starts from ASR on the reference segments and speakers, adds
  segmentation and speaker attribution to reach cpWER, and adds timing to reach tcpWER. The parts come from
  `metrics/attribution.py` and §3.2.
- **Which stage to upgrade**: the grid of every VAD / diarisation and ASR pair already run, including the
  "Reference segments + speakers" row, with Run buttons for missing pairs. It moves here from the Pipelines
  tab.
- **Where errors start**: cpWER by kind of moment (near a speaker change, overlap, segments over 30 s, short
  turns), split by the stage that caused the errors. This needs each error word tagged with its cause, which
  `attribution.py` doesn't do yet. Tag a word by comparing its alignment in the three runs it already makes
  (oracle segments and speakers, model segments with reference speakers, the full pipeline).
- **By label** (§7.4).
- **Files**: each file's cpWER split by cause, and an open file shows the transcript with every error tagged
  by its cause.

---

## 9. Removing offline export (E0)

All of the offline export leaves Bench (§1). Concord Label's copy of the same code stays as it is.

| Part | What to remove or change |
|---|---|
| Frontend | The `/offline` route (`app/routes.tsx`), the "Export for offline" sidebar entry (`app/Nav.tsx`), `features/offline/` (`ExportOffline.tsx`, `AppChoice.test.tsx`), `lib/offline.ts` and `lib/offline.test.ts`, the offline types and calls in `lib/api.ts`, and "Export for offline" in the sidebar check in `e2e/smoke.spec.ts` |
| API | `api/offline.py`, and its router in `api/app.py` |
| Module | `offline/` (`export.py`, `kit.py`, `merge.py`, `install.sh`) and `platform/tools.py`, which only runs the platform's tools for kits |
| CLI | `export-offline`, `verify-kit` and `add-kit` in `cli.py`, and the `export-offline`, `verify-kit` and `test-offline` recipes in the `Justfile` |
| Jobs | The `export` job type and its handler in `runs/jobs.py`, and the export label in `api/runs.py` |
| Database | The `OfflineExport` model in `core/models.py`, dropped by a new migration (`offline_export` table) |
| Settings and paths | `paths.offline_kits()`, the Concord Bench image setting for kits in `core/settings.py`, and the kit comment in `.env.example`. `PLATFORM_DIR` stays, because Bench still reads the aicore endpoint map from it. |
| Image | In the `Dockerfile`, the docker CLI and compose plugin, the platform tools' environment and `UV_OFFLINE`, which only kits needed. Check that nothing else uses them first. |
| Tests | `tests/test_offline.py`, `tests/test_offline_airgap.py`, `tests/airgap_flow.py`, the `offline` marker in `pyproject.toml` and its handling in `tests/conftest.py` |
| Contracts | CONTRACTS.md §6 and `contracts/samples/kit/` (see below) |
| Docs | The offline sections of `README.md`, including the `just test-offline` limitation note |

Two more points:

- **CONTRACTS.md §6 and `contracts/samples/kit/`** describe the apps in a kit. Concord Label had already
  removed both in its commit f6ee7e4, so removing them from Bench in E0 made the two copies identical again
  (`just check-contracts`), with no change to the Label repository.
- **Shipping Bench to an air-gapped machine.** The platform's catalog export bundles models only
  (`POST /api/exports`), not app images. The README describes the manual way instead: `docker save` the
  Bench image, `docker load` it on the target, and import a catalog bundle for the models.

E0 also commits the model-catalog refactor that is in the working tree, before any other milestone starts.

---

## 10. Milestones

Each Bench milestone ends with `just test`, `just lint` and the e2e tests green. Sizes are rough, for one
developer.

| # | Repo | Work | Needs | Exit | Size |
|---|---|---|---|---|---|
| E0 | Bench | Commit the model-catalog refactor; remove offline export (§9) | none | Tests and e2e pass with no offline code | 1 to 2 d |
| E1 | Bench | MER, WIL, WIP, tcpWER, overlap toggle, hardware from the catalog and wall time on runs (§3) | E0 | Metric unit tests; hardware shows what the catalog reports, "-" for the rest | 3 to 4 d |
| P1 | Platform | `host` on `/api/platform`, `runtime` fields and the `generative` flag on models, peak VRAM on leases (§3.4, §5.1); done by the platform in its own time | none | The catalog returns the new fields | 2 to 3 d |
| E2 | Bench | Dialog, run popup, sortable leaderboards, ASR controls and inspector, normaliser buttons, Models tabs (§4) | E1 | e2e covers the popup and sorting | 4 to 5 d |
| E3 | Bench | Enhancer capability and adapter, enhanced copies and the job, runs and pipelines on copies, planning (§5) | E0 | A fake enhancer builds a copy, and a run and a pipeline score on it | 5 to 7 d |
| E4 | Bench | Enhancement UI on Benchmarks, Pipelines, Datasets, Dataset detail, Upload, Models, and the enhancement error page (§6) | E2, E3 | e2e builds a copy and shows a model + enhancer row | 5 to 7 d |
| E5a | Bench | Labels read at upload, shown on upload and dataset detail (§7.1); re-upload NSC-Scripted as a new dataset | E0 | NeMo `attribute` fields arrive as labels | 1 to 2 d |
| E5b | Bench + Label | Reference package version 2 with attributes (§7.2) | E5a; the Label repository free | Contract tests pass in both; `just check-contracts` | 2 d |
| E5c | Bench | Slice statistics, Label view, saved slices, "by label" sections (§7.3, §7.4) | E5a | Slice tests; e2e opens the Label view | 4 to 5 d |
| E6 | Bench | Diarisation and pipeline error analysis additions (§8) | E1; E5c for "by label" | Region and cause tests; e2e opens both pages | 6 to 8 d |
| E7 | Bench | Docs: HANDOFF.md §3.6 metric table and canvas link, README, PLAN.md | all | Docs match the build | 1 d |

About 7 to 9 weeks in total for Bench. E5a can start right after E0, and E1, E2, E5 and E6 can run side by
side with E3. E4 needs E3. P1 is independent, and Bench shows more hardware fields once it lands.

Status on 2026-10-24: E0, E1, E2, E3, E4, E5a, E5c, E6 and E7 are built and committed. E5b waits until the
Concord Label repository is free to change, because it needs the reference package to change in both
repositories at once. P1 is the platform's to do, and Bench shows its fields once the catalog returns them.

---

## 11. Testing

- **Metrics**: MER, WIL and WIP against hand-computed values from H, S, D, I, N and P, and corpus values
  from summed counts. tcpWER against meeteval directly, at each collar. Overlap on and off give different
  DER on a fixture with overlap.
- **Hardware**: a recorded catalog with a GPU model, a CPU model and a hosted model, with and without the
  P1 fields, all shown correctly.
- **Enhancement**:
  - A copy built with the fake enhancer has one file per usable original, with new sha256 values.
  - An output 5 ms too long is trimmed and its shift recorded, and one 20 ms too long fails the file.
  - A long file is chunked and joined back to the exact original length.
  - A second run on the same copy hits the output cache.
  - Plan counts match §5.5, and runs wait for a copy that is still building.
  - After-placement cuts the enhanced audio at the original's diarised segments.
  - Deleting a copy keeps its runs' scores, and a new run rebuilds it.
  - One live test (`just test-live`) builds a copy with `deepfilternet3` on a few fixture files.
- **Labels**:
  - A NeMo manifest with nested `attribute` fields gives dotted labels on segments and on files.
  - The kind rules sort discrete, continuous and unused fields correctly.
  - The bootstrap gives the same interval twice with the same seed, and "reliably worse", "too few" and
    "hard audio" match small hand-made cases.
  - A saved slice shows only on its own dataset.
  - Reference package version 2 round-trips attributes, and version 1 still imports.
- **Error analysis**: region classification on a hand-made reference; per-word cause tags add up to the
  attribution totals.
- **Offline removal**: the app starts, migrates an existing database with an `offline_export` table, and
  the e2e smoke spec passes with the new sidebar.
- **e2e**: extend the smoke spec for the run popup, leaderboard sorting, an enhancement row, the enhancement
  page, the Label view and the new diarisation and pipeline panels.

---

## 12. Open questions

None. Every question from 2026-10-23 and 2026-10-24 is answered and worked into §1, §3.4, §5.1, §7 and
§9. Dataset updates are out of this plan and wait for their own design (§1).

---

## 13. Risks

| Risk | Mitigation |
|---|---|
| Enhanced copies fill the disk, because each one is as large as the derived audio of its dataset | Show sizes on Datasets and Dataset detail, allow deleting copies, and keep scores after a delete (§5.2) |
| Large JSON requests to enhancers are slow or hit size limits | Chunk files over 60 s (§5.2) |
| Chunk joins change the audio slightly | Overlap with a crossfade, and record the chunking in the copy's parameters so it can be changed later without mixing results (§5.2) |
| A generative enhancer shifts timing or invents speech, so its scores mislead | Bench checks the length of every file and fails files more than 10 ms off (§5.2), and marks runs of enhancers the catalog flags as generative (§5.1) |
| The grid grows fast (models x enhancers x placements) | The plan preview shows totals before running, reuses scored pipelines, and shows the top 15 by default |
| Hardware fields are missing for some runs, so speed comparisons are unfair | Show "-" for missing fields, warn on mixed GPU, CPU and hosted rows, and offer a filter (§3.4) |
| The bootstrap is slow on large datasets | Cache per run, normaliser and label (§7.3) |
| Labels with many values or IDs make useless slices | The kind rules in §7.1 skip them, and the upload page shows what was skipped |
| The reference package change breaks Concord Label | Version 2 with version 1 still read, sample files tested in both repositories, `just check-contracts`, done only when the Label repository is free (§7.2) |
| Removing offline code leaves Bench's and Label's shared copies further apart | Expected, since Label keeps its copy of the code for now. The contract files are identical again (§9) |

---

## 14. What was built, and where it differs from the plan

Written on 2026-10-24, after E0 to E6 (without E5b) were built. The sections above still describe the plan,
and the list below records each place where the code took a different route.

- **Where enhanced audio is stored (§5.2).** Enhanced files are written to the same store as every other
  derived file, `data/derived/<sha256>.flac`, with peaks next to them, and the plan's
  `data/derived/enh/<copy_id>/` folders are not used. Each `enhanced_file` row points at its original with
  `audio_file_id` and has no `rel_path`. Storing by sha256 means the output cache, playback and peaks work
  on enhanced audio with no change.
- **Unchanged output (§5.2).** When an enhancer returns audio identical to its input, the copy's file points
  at the original's audio, and the copy's size leaves that file out. The e2e tests use an identity enhancer
  for this reason, so the recorded model outputs replay on the enhanced copy.
- **Deleting a copy (§5.2).** A delete keeps any audio that another live copy or an original also uses.
- **More columns on `enhanced_copy` (§5.2).** The table also stores the image, hardware, chunking parameters
  and their hash, the error, the file count, the audio and processing seconds, and the RTF. Each
  `enhanced_file` stores its latency.
- **Enhancement fields live on the run (§5.3, §5.4).** `Run.enh_model_id`, `Run.enh_copy_id` and
  `Run.enh_placement` replace the planned `Pipeline.enh_copy_id` and `Pipeline.enh_placement`. A pipeline
  stays a pair of a diariser and an ASR model, and one pipeline has a run per enhancer and placement.
- **Run evaluation dialog (§4).** The dialog has no Compute select, because the catalog decides where each
  model runs.
- **New pipeline run (§6).** The button opens the same plan panel as Run evaluation, with pipelines chosen
  and the enhancement ablation fieldset, in place of the planned four-column popup.
- **Scores are versioned.** `SCORE_VERSION` is stored with each score's aggregates, and a score from an
  older version is computed again when it is read. Version 2 added MER, WIL, WIP, tcpWER and the pipeline
  error causes. Version 3 decides "within 0.5 s of a speaker change" per word, because per segment it
  counted nearly every word in conversations with quick turn-taking. Reference words have no times, so each
  word's time is spread evenly over its segment.
- **Peak VRAM (§3.4).** Bench reads it from the lease just before releasing the lease. Reading it afterwards
  made the fake catalog in the tests activate the lease again.
- **Upload plan.** Building the upload page's plan showed that the upload API had been dropping the catalog
  ids of the chosen diarisation and ASR models, and the fix is in the E3 commit.
- **E5b (§7.2) is not built.** Reference package version 2 needs changes in Concord Label as well, and the
  Label repository is being edited, so it waits. Labels reach Bench today through the annotation files of
  an upload, e.g., the nested `attribute` of a NeMo manifest.
