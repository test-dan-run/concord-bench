# Concord — Split into Bench and Label

Planned 2026-10-21, not built. Kept apart from [PLAN.md](PLAN.md) (Phases 1–3) and
[OFFLINE_EXPORT_PLAN.md](OFFLINE_EXPORT_PLAN.md); references to them are written as "PLAN.md §N" and
"OFFLINE_EXPORT_PLAN.md §N". Section numbers without a file name are in this document.

Concord today is one app with two jobs: benchmarking models on labelled data (Phase 1) and producing
labels with pseudo-labelling and review (Phase 2). This plan splits it into two applications:

- **Concord Bench**: benchmark models on reference datasets and analyse their errors.
- **Concord Label**: pseudo-label audio, review it, and hand finished references to Bench.

Annotation output from Label must reach Bench in a few clicks, on the same machine or on another one.

---

## 1. Where the code splits today

The backend already divides along this line. `label/` imports only shared layers, and the benchmark side
reaches into labelling in two places.

| Area | Today | After the split |
|---|---|---|
| `core/` settings, paths, db, models | one SQLite schema | **core** package: shared tables; each app adds its own |
| `datasets/` audio, ingest, parsers | used by both | **core** |
| `registry/`, `platform/`, `adapters/` | used by both | **core** |
| `runs/cache.py` (output cache) | used by both | **core** |
| `runs/executor.py` `prepare_cuts`, `usable_files` | pipelines and pseudo-labelling | **core** (`concord_core.cuts`) |
| `runs/jobs.py`, `runs/worker.py` | one `HANDLERS` dict naming every job type | **core** job framework; each app registers its handlers |
| `metrics/` | Bench scores; Label's live evaluation uses `wer`, `der`, `normalisers` | **core** |
| `offline/` (kits) | one app | **core**; both apps can write a kit (§7) |
| `runs/` executor, scoring; `analysis/`; `api/benchmarks.py`, `api/errors.py`, `api/runs.py` | | **Bench** |
| `label/` pseudo, fusion, edit, console, live, sync; `api/label.py`, `api/sync.py` | | **Label** |
| `label/promote.py` | copies confirmed segments into a new dataset in the same database | **replaced** by the reference package (§4) |
| `concord_improve` (Phase 3, on hold) | reads `segment`, `dataset`, `model` | **Label** (it trains from labels; Q3) |

Couplings that do not survive separate databases:

| Coupling | Where | After the split |
|---|---|---|
| Promote to benchmark dataset | `label/promote.py` | Reference package export in Label, import in Bench (§4) |
| Locked benchmark score in live evaluation | `label/live.py` `_locked()` reads `Score` | Label asks Bench over HTTP when `BENCH_URL` is set; hidden otherwise (§5.3) |
| "Open in annotator" | `AsrErrors.tsx`, `PipelineErrors.tsx` | Deep link to Label when `LABEL_URL` is set and the dataset came from Label (§5.2) |
| "Pseudo-label" on an audio-only dataset | `DatasetDetail.tsx` | Audio-only datasets exist in Label only; Bench requires references |
| Flagged references | Bench sets `segment.flagged`; the annotator shows a flags lane | Flags go back to Label in a small file (§4.5) |
| Pseudo-labelling reuses benchmark outputs (cache hits) | `model_output` shared | Each app has its own cache. A reference package can carry seg-model outputs (§4.4) |
| One worker decides which services run | `ensure_services` batches under `util_limit` | Two workers share one GPU and could evict each other's services (§6) |

---

## 2. Repository and packaging

One repository with three Python packages and three frontend packages. Changes to the shared core
land in both apps in one commit, and one `just test` covers everything.

```
Concord/
├── SPLIT_PLAN.md, PLAN.md, OFFLINE_EXPORT_PLAN.md, README.md
├── pyproject.toml              # uv workspace: core, bench, label
├── Justfile                    # dev-bench, dev-label, dev (both), test, lint, build-bench, build-label
├── Dockerfile                  # targets: bench, label, label-full
├── packages/core/              # concord_core: settings base, paths, db, shared tables, datasets, registry,
│                               #   platform, adapters, cache, cuts, metrics, jobs framework, offline kits
├── apps/bench/                 # concord_bench: runs, scoring, analysis, reference-package import, API
├── apps/label/                 # concord_label: pseudo, fusion, edit, console, live, sync, reference export, API
├── apps/label/improve/         # concord_improve (label-full only)
├── frontend/                   # npm workspaces
│   ├── shared/                 # components (AudioPlayer, Timeline, SortableTable, DiffText, MetricTile, TagInput,
│   │                           #   icons, ui), lib (api, format, colors, upload), tokens (index.css),
│   │                           #   shared pages (Models, Add model, Contract, Jobs, Upload, Offline export)
│   ├── bench/                  # Vite app
│   └── label/                  # Vite app
├── fixtures/
└── tests/{core,bench,label,e2e}
```

- **import-linter**: `concord_core` imports neither app; `concord_bench` and `concord_label` never import
  each other; `concord_improve` stays out of everything except Label's app factory (as today).
- **Images**: `concord-bench`, `concord-label`, `concord-label-full`. Lite/full only applies to Label once
  Improve moves there (Q3). Each image runs its own API and worker.
- **Ports**: Bench 8600 (unchanged), Label 8610. Development: Vite 5173 (Bench) and 5174 (Label).
- **Data**: separate data directories and databases: `BENCH_DATA` (default `data/bench`) and
  `LABEL_DATA` (default `data/label`). Nothing is shared on disk, so the two can run on different machines.

### 2.1 Database migrations

Shared tables are defined once in core. Alembic gets one branch per package (`version_locations`): a
`core` branch and an app branch, applied together by each app's `migrate`. A change to a shared table
is written once and reaches both apps.

| Owner | Tables |
|---|---|
| core | `dataset`, `audio_file`, `segment`, `keyword_event`, `model`, `model_output`, `job`, `offline_export` |
| Bench | `pipeline`, `run` (asr/seg/pipeline/kws), `score`, `result`, new `reference_import` |
| Label | `run` (label task), `sync_package` (snapshots stay files under the data directory), new `reference_export` |

`run` exists in both apps with different task sets. It stays app-owned so each app can change it on its own.

---

## 3. The two apps

### 3.1 Concord Bench

For: benchmarking models on reference datasets and reading their errors.

| Nav | Pages | Change from today |
|---|---|---|
| Benchmarks | ASR, Diarisation, Pipelines tabs; Run cells; normaliser and collar selectors | unchanged |
| Errors | ASR, diarisation, pipeline error pages | "Open in annotator" becomes "Open in Label" (§5.2) |
| Datasets | list, upload (annotated only), detail, **Import from Label** | audio-only upload removed; import added (§4.3); a dataset shows where its references came from and its version |
| Models | registry, Add model, contract page | shared page |
| Jobs | queue, logs | shared page |
| Offline export | kit builder | shared page (§7) |

Bench keeps "Flag reference as wrong". Flagged segments are listed on the dataset page and can be sent
back to Label (§4.5).

### 3.2 Concord Label

For: turning audio into reviewed references.

| Nav | Pages | Change from today |
|---|---|---|
| Console | label progress per dataset, next action | unchanged |
| Pseudo-label | members, fusion, thresholds, cost preview, agreement matrix | unchanged |
| Annotate | timeline, segment grid, shortcuts | flags lane also shows flags returned from Bench (§4.5) |
| Live evaluation | WER vs confirmed count, CIs, DER card | "locked score" comes from Bench when linked (§5.3); **Promote** becomes **Send to Bench** (§4) |
| Datasets | upload audio (with or without partial annotations), list, detail | the dataset detail page gets the Send to Bench history |
| Sync | label packages between Label installs | unchanged (PLAN.md §6.5) |
| Models, Jobs, Offline export | | shared pages |
| Training | | label-full only (Q3) |

---

## 4. Handoff: the reference package

### 4.1 Format

A reference package is a folder or zip that Bench's **existing** ingest can read, plus a manifest. No new
parser is needed in Bench, and a person can open one with any tool that reads SegLST or RTTM.

```
<dataset>-refs-v<N>/
  manifest.json      format "concord-reference-package", version 1; source (Label install id, dataset uid,
                     name), export version N, created_at, scope, label types, counts (files, segments,
                     hours), review stats, member models of the label runs, normaliser hint
  audio/             originals at their dataset paths (optional when Bench already has every file, §4.2)
  files.csv          path, sha256, duration, sample_rate
  refs/segments.seglst.json   speaker-attributed transcripts (cpWER, WER, DER)
  refs/refs.rttm              diarisation only (scope `complete`)
  refs/refs.jsonl             per-file text, for ASR-only datasets (scope `any`)
  refs/uids.jsonl             segment uid per (file, start, end), so flags can come back (§4.5)
  outputs/           optional: seg-model outputs on whole files (§4.4)
```

What goes in follows today's promotion rules (PLAN.md §6.6):
- only **confirmed** segments; auto-accepted segments never become references;
- scope `complete` (default): fully reviewed files only, so DER is meaningful;
- scope `any`: every file with a confirmed segment, ASR only;
- pseudo-label segments keep their unpadded turn times.

### 4.2 Sending (Label)

"Send to Bench" on the Live evaluation and dataset pages. The form has name, scope, tags, include audio
(auto / always / never), include model outputs, and a preview of counts. Two ways out:
- **Push**, when `BENCH_URL` is set and Bench answers `/api/config`. Label first asks Bench which sha256s it
  already has (`POST /api/fingerprints/missing`) and leaves those files out. It then uploads the package
  with the existing chunked upload API.
- **Download**, otherwise. Label writes the zip under its data directory and offers it for download. This is
  for another machine or the offline setup.

Every send is recorded (`reference_export`): version, scope, counts, destination, Bench's dataset id if pushed.

### 4.3 Receiving (Bench)

- Pushed packages and uploaded zips both arrive in **Datasets → Import from Label** as staged imports with a
  preview: source, version, files, hours, label types, how many files are new, and differences from the
  previous version.
- **Accept** runs the normal ingest job on the package folder, then stores the provenance on the dataset.
- **Versions.** References that have been scored are never changed, so benchmark numbers stay reproducible.
  A package from the same Label dataset becomes a new dataset (`<name> v2`) linked to the earlier versions.
  Accepting offers "Run the models benchmarked on v1 against v2" (the existing benchmark-plan panel,
  pre-ticked).
- Audio files Bench already has (same sha256) are not stored again; derived files and the cache are reused.

### 4.4 Model outputs in the package (optional)

Each app has its own output cache after the split. Seg-model outputs on whole files are keyed on (model,
audio sha256, whole file, params including image tag), so they mean the same in both apps. The package can
carry them, and Bench imports them into its cache when the image tag matches. Bench then does not re-run
diarisation it would get the same answer from. ASR outputs are not carried: Label transcribed padded cuts,
and Bench runs ASR on the reference spans anyway (PLAN.md §6.6).

### 4.5 Flags back to Label

Bench's "Flag reference as wrong" is useful to the person who labelled the data. A dataset that came from
Label gets "Send flags to Label": a small JSON of (segment uid, file sha256, note), pushed to `LABEL_URL` or
downloaded. Label imports it as flags on the matching segments, which then show in the annotator's flags
lane. The next reference package from that dataset is a new version (§4.3).

---

## 5. Links between the apps

Both links are optional. Each app works fully on its own.

### 5.1 Settings

| Setting | App | Meaning |
|---|---|---|
| `BENCH_URL` | Label | Where to push reference packages and read locked scores |
| `LABEL_URL` | Bench | Where "Open in Label" and "Send flags to Label" go |

`GET /api/config` reports the other app's URL and whether it answered, so the UI shows or hides each link.

### 5.2 Open in Label

Bench's error pages show "Open in Label" for a dataset whose references came from Label. The link is
`LABEL_URL/label/file/by-sha/<sha256>?t=<start>`, so it does not depend on Label's database ids. Label
resolves the sha256 within the source dataset named in the provenance.

### 5.3 Locked score

Label's live evaluation shows each model's locked benchmark score next to its live WER. After the split,
Label calls `GET BENCH_URL/api/datasets/by-source/<label dataset uid>/scores?normaliser=…`. Bench answers
with the latest score per model on the newest version. Without a reachable Bench the column is hidden.

---

## 6. Sharing one GPU between two workers

Today one worker starts what a job needs with `stack.py up` and batches jobs so services fit under
`util_limit`. With two apps, Label's worker could start a service that makes `stack.py` stop one that a
Bench job is using mid-run.

- **Leases.** Before a job, each worker writes the services it is using to
  `$PLATFORM_DIR/run/leases/<app>-<pid>.json` and removes the file after the job. Stale leases (dead pid)
  are ignored.
- **Platform: `stack.py up --keep NAME...`**. Services named here are never stopped. If the request does
  not fit next to them, `up` refuses with the usual message. `ensure_services` in core passes every leased
  service as `--keep`.
- The job is then `blocked` (existing behaviour) and retried when the other app's job ends.
- The platform change is small: one flag, applied before the eviction step in `stack.py up`, with a test.

---

## 7. Offline export after the split

The offline kit (OFFLINE_EXPORT_PLAN.md) moves to core, and the export page is shared, so either app can
write a kit. A full kit holds the app images that are ticked: Bench, Label, or both, with one
`concord/compose.yml` that starts what is in the kit and sets `BENCH_URL` / `LABEL_URL` between them. Model
kits are unchanged.

---

## 8. Moving existing data

The current install has data (756 MB in the main checkout). A one-off command splits it:

`python -m concord_core.split --from data --bench data/bench --label data/label`

- **Bench gets**: datasets with references; their `run`, `score`, `result`, `pipeline` rows; models; the
  output cache.
- **Label gets**: audio-only datasets and every dataset with label work (pseudo or edited segments); label
  runs; sync tables; models; the output cache.
- A dataset made by Promote goes to Bench, with provenance pointing at its Label dataset (export version 1).
- Audio, derived files and peaks are hardlinked when both targets are on the same filesystem, else copied.
- The old database is left untouched. The command is safe to re-run into empty targets.

Starting fresh is the alternative (Q5).

---

## 9. Milestones

Each one ends with `just test`, `just lint` and the e2e tests green.

| # | Work | Exit | Size |
|---|---|---|---|
| S1 | Workspace layout: uv and npm workspaces, move code into `packages/core`, `apps/bench`, `apps/label` with no behaviour change; one app still serves everything | Every existing test passes from the new layout | 2–3 d |
| S2 | Core extraction: shared tables and the Alembic core branch; job framework with handler registration; `cuts` into core; import-linter contracts | Core has no import of either app | 3–4 d |
| S3 | Bench app: own app factory, settings, database, nav and routes; audio-only upload and label routes removed | Bench runs alone; Phase 1 acceptance test and smoke e2e pass | 3–4 d |
| S4 | Label app: own app factory, settings, database, nav and routes; promote removed | Label runs alone; label tests and label e2e pass | 3–4 d |
| S5 | Reference package: export (download, push), Bench import with preview and versions, optional outputs | Round trip on the fixture set (§10) | 4–5 d |
| S6 | Links: `BENCH_URL` / `LABEL_URL`, locked score, Open in Label, flags back | Both links work with the other app up, and hide with it down | 2–3 d |
| S7 | GPU leases and `stack.py --keep` | Orchestration tests (§10) | 2 d |
| S8 | Images, offline kit with both apps, data split command, README | Both images build; split command run on a copy of the current data | 3 d |

About 4–5 weeks for one developer. S5 can start once S2 is done, alongside S3 and S4.

---

## 10. Testing

- **Moved tests**: today's tests go to `tests/core`, `tests/bench` and `tests/label` unchanged in substance.
- **Round trip** (the main new test, on the fixture set with recorded responses):
  - pseudo-label and confirm in Label;
  - send to Bench, both as a downloaded zip and as a push;
  - accept in Bench and benchmark one ASR model;
  - the WER must equal what today's Promote path gives;
  - send a second version, and check that v1's scores are unchanged and v2 is a new dataset.
- **Missing-audio push**: Bench already has every file, so the package carries no audio and the import
  still succeeds.
- **Links**: live evaluation with Bench up shows the locked score, and with Bench down hides it. Flags
  sent from Bench show in Label's flags lane.
- **Leases**: `ensure_services` with `stack.py` mocked; a Label job does not evict a service leased by
  Bench and ends up `blocked`.
- **Architecture**: import-linter contracts from §2; each image starts with only its own routes.
- **e2e**: the current smoke and label Playwright specs, split across the two apps, plus one spec for Send
  to Bench → Import from Label.
- **Data split**: on a copy of the current data, row counts per table match the rules in §8.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Two workers evict each other's services | Leases and `stack.py up --keep` (§6). |
| Benchmark numbers change when references are re-sent | A new package from the same source is a new dataset version; scored references are never edited (§4.3). |
| Losing cache reuse between benchmarking and pseudo-labelling costs GPU time | Seg-model outputs travel in the package (§4.4); Label's own cache still serves re-fusing. |
| Shared-table changes drift between apps | One Alembic core branch used by both apps (§2.1); one test run covers both. |
| The workspace move breaks imports and paths (`CONCORD_HOME`, fixtures, `frontend/dist`) | S1 moves code only and must pass every existing test before anything else changes. |

---

## 12. Still open

| # | Question | Default meanwhile |
|---|---|---|
| Q1 | Names and ports. | Concord Bench on 8600, Concord Label on 8610. |
| Q2 | One repository with shared core, or two repositories? | One repository (§2). |
| Q3 | Does Improve (Phase 3, on hold) go with Label? | Yes: it trains from labels. `concord-label-full` replaces `concord-full`. |
| Q4 | Offline export: in both apps, with a kit holding either or both images? | Yes (§7). |
| Q5 | Split the existing data, or start both apps empty? | Provide the split command (§8). |
| Q6 | Should Bench accept a reference package from any source, such as one written by hand, without a Label install? | Yes: the format is plain SegLST/RTTM/JSONL plus a manifest; provenance is optional. |
