# Concord — Split into Bench and Label

Planned 2026-10-21, not built. Kept apart from [PLAN.md](PLAN.md) (Phases 1–3) and
[OFFLINE_EXPORT_PLAN.md](OFFLINE_EXPORT_PLAN.md); references to them are written as "PLAN.md §N" and
"OFFLINE_EXPORT_PLAN.md §N". Section numbers without a file name are in this document.

Concord today is one app with two jobs: benchmarking models on labelled data (Phase 1) and producing
labels with pseudo-labelling and review (Phase 2). This plan splits it into two applications in two
repositories:

- **Concord Bench**: benchmark models on reference datasets and analyse their errors.
- **Concord Label**: pseudo-label audio, review it, and hand finished references to Bench.

Annotation output from Label must reach Bench in a few clicks, on the same machine or on another one.
Decisions the owner made on 2026-10-21 are marked "Decided" in the sections they shape.

---

## 1. Where the code splits today

The backend already divides along this line. `label/` imports only shared layers, and the benchmark side
reaches into labelling in two places (the job dispatcher and the API router).

| Area | Today | After the split |
|---|---|---|
| `core/` settings, paths, db, models | one SQLite schema | **both**, each with only its own tables (§2.1) |
| `datasets/` audio, ingest, parsers | used by both | **both** |
| `registry/`, `platform/`, `adapters/` | used by both | **both** |
| `runs/cache.py` (output cache) | used by both | **both** |
| `runs/executor.py` `prepare_cuts`, `usable_files` | pipelines and pseudo-labelling | **both**; Label keeps only these two from the executor |
| `runs/jobs.py`, `runs/worker.py` | one `HANDLERS` dict naming every job type | **both**, each with only its own job types |
| `metrics/` | Bench scores; Label's live evaluation uses `wer`, `der`, `normalisers` | **Bench** all; **Label** `wer`, `der`, `normalisers` |
| `offline/` (kits) | one app | **both** (§7) |
| `runs/` executor, scoring; `analysis/`; `acceptance.py`; `api/benchmarks.py`, `api/errors.py`, `api/runs.py` | | **Bench** |
| `label/` pseudo, fusion, edit, console, live, sync; `api/label.py`, `api/sync.py` | | **Label** |
| `label/promote.py` | copies confirmed segments into a new dataset in the same database | **replaced** by the reference package (§4) |
| `concord_improve` (Phase 3) | placeholder router, in `concord-full` only | **Label**, still on hold, in no image (§3.2) |

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

## 2. Repositories and packaging

**Decided: two repositories that share no code.** Each app owns a copy of the code both use today, and
each copy keeps only what its app needs. The two apps agree on formats and HTTP endpoints, written down in
one contract document (§2.3), not on shared code. Both depend on the platform as they do today (`aicore`,
`tools/stack.py`); the platform is the only code they have in common.

```
dev/apps/
├── Concord/                    # becomes Concord Bench; keeps the git history
│   ├── CONTRACTS.md            # the formats and endpoints both apps follow (§2.3)
│   ├── contracts/samples/      # sample files for each contract, used by the tests
│   ├── backend/concord_bench/  # core, datasets, registry, platform, adapters, cache, runs, scoring,
│   │                           #   metrics, analysis, offline, reference-package import, API
│   ├── frontend/               # Bench's Vite app with its own components
│   ├── fixtures/
│   └── tests/
└── concord-label/              # new repository, cloned from Concord (keeps the history too)
    ├── CONTRACTS.md, contracts/samples/   # identical copies (§2.3)
    ├── backend/concord_label/  # core, datasets, registry, platform, adapters, cache, cuts, metrics
    │                           #   (wer, der, normalisers), offline, pseudo, fusion, edit, console,
    │                           #   live, sync, reference-package export, API
    ├── backend/concord_improve/  # Phase 3 placeholder, on hold
    ├── frontend/               # Label's Vite app with its own components
    ├── fixtures/
    └── tests/
```

- **How the Label repository starts.** Clone the current repository, so both keep the full history. Each
  then deletes what it does not need (§2.2). This is less work than extracting a shared package, and no
  code is rewritten.
- **Package names.** The Python package `concord` becomes `concord_bench` in one repository and
  `concord_label` in the other, so logs and tracebacks say which app they come from.
- **Size of the copies.** About 3,950 lines of Python (core, datasets, registry, platform, adapters, cache,
  jobs, metrics, offline) and 2,800 lines of TypeScript and CSS (components, lib, tokens) exist in both
  repositories after the split, less what each removes. A fix in that code is made in each repository that
  has it (§11).
- **import-linter**: in the Label repository, `concord_label` never imports `concord_improve`. Bench has no
  contract left to enforce.
- **Images**: `concord-bench` and `concord-label`. Each image runs its own API and worker. `concord-lite`,
  `concord-full` and `FEATURE_IMPROVE` go away from Bench.
- **Names and ports (decided)**: Concord Bench on 8600 (unchanged), Concord Label on 8620 (decided as 8610, moved because the Synthetics app already uses 8610 on this machine). Development:
  Vite 5173 (Bench) and 5174 (Label).
- **Data**: each app has its own data directory and database (`CONCORD_DATA` in each repo's `.env`). Nothing
  is shared on disk, so the two can run on different machines.
- **Directory name.** The Bench repository can be renamed (for example to `concord-bench`) at any time;
  nothing refers to it by path.

### 2.1 Database migrations

Each app has its own Alembic chain. Both start empty (§8), so each squashes today's two revisions into one
new initial revision holding only its own tables.

| App | Tables |
|---|---|
| Bench | `dataset`, `audio_file`, `segment`, `keyword_event`, `model`, `model_output`, `job`, `offline_export`, `pipeline`, `run` (asr/seg/pipeline/kws), `score`, `result`, new `reference_import` |
| Label | `dataset`, `audio_file`, `segment`, `keyword_event`, `model`, `model_output`, `job`, `offline_export`, `run` (label task), `sync_package` (snapshots stay files under the data directory), new `reference_export` |

The tables both apps have start identical and may then change separately. Only the contracts in §2.3
must stay compatible.

### 2.2 What each copy keeps

| Today | Bench keeps | Label keeps |
|---|---|---|
| `core/`, `datasets/`, `registry/`, `platform/`, `adapters/`, `runs/cache.py`, `runs/jobs.py`, `runs/worker.py`, `offline/` | all | all |
| `runs/executor.py`, `runs/references.py`, `runs/scoring.py` | all | `prepare_cuts` and `usable_files`, moved to `concord_label/cuts.py` |
| `metrics/` | all | `wer`, `der`, `normalisers` |
| `analysis/`, `acceptance.py`, `recording.py` | all | none (`recording.py` runs Bench's acceptance flow) |
| `label/` | none | all except `promote.py` |
| `concord_improve` | none | placeholder, not installed |
| Frontend `components/` | all | `AudioPlayer`, `Timeline`, `timelineMath`, `TagInput`, `icons`, `ui` |
| Frontend `lib/`, `index.css` tokens | all except `label.ts` | all except benchmark-only helpers |
| Frontend pages Models, Add model, Contract, Jobs, Upload, Offline export | own copy | own copy |
| `fixtures/` | all | `generated/` and `recorded/` as copies; Bench regenerates both (`just fixtures`, `just record`) and Label copies them again when they change |

### 2.3 Contracts between the apps

The only things the two apps must agree on. They are written in `CONTRACTS.md`, which carries a version
number and is kept identical in both repositories. Each contract has sample files in `contracts/samples/`.

| Contract | Written by | Read by | Section |
|---|---|---|---|
| Reference package, format version 1 | Label (and people, by hand) | Bench | §4.1 |
| Flags file, format version 1 | Bench | Label | §4.5 |
| `POST /api/fingerprints/missing`, chunked upload, staged reference import | Label calls | Bench serves | §4.2 |
| `GET /api/datasets/by-source/<uid>/scores` | Label calls | Bench serves | §5.3 |
| `/label/file/by-sha/<sha256>`, `POST /api/flags/import` | Bench links and calls | Label serves | §5.2, §4.5 |
| `GET /api/config`: app name, version, contract version | both | both | §5.1 |
| How to run each app in a kit: image, port, environment, mounts | either app | `install.sh` in the kit | §7 |

- **Tests in both repositories** check their own side against the samples: Label's writer produces files
  that match the samples, and Bench's reader imports every sample.
- **`just check-contracts`** compares `CONTRACTS.md` and `contracts/samples/` with the other repository when
  it sits next to this one, and fails on any difference.
- **Version changes.** A contract change bumps the version in `CONTRACTS.md` and lands in both repositories.
  Each app reads the format versions it knows and refuses others with a clear message. `/api/config`
  reports the contract version so a push can be refused before any upload starts.

---

## 3. The two apps

### 3.1 Concord Bench

For: benchmarking models on reference datasets and reading their errors.

| Nav | Pages | Change from today |
|---|---|---|
| Benchmarks | ASR, Diarisation, Pipelines tabs; Run cells; normaliser and collar selectors | unchanged |
| Errors | ASR, diarisation, pipeline error pages | "Open in annotator" becomes "Open in Label" (§5.2) |
| Datasets | list, upload (annotated only), detail, **Import reference package** | audio-only upload removed; import added (§4.3); a dataset shows where its references came from and its version |
| Models | registry, Add model, contract page | unchanged |
| Jobs | queue, logs | unchanged |
| Offline export | kit builder | can add Label's image to a kit (§7) |

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
| Sync | label packages between Label installs | unchanged (PLAN.md §6, Import / export) |
| Models, Jobs | | own copies of today's pages |
| Offline export | kit builder | can add Bench's image to a kit (§7) |

**Decided: Improve (Phase 3) moves to Label and stays on hold.** It trains from labels, so it belongs
with Label. The `concord_improve` placeholder moves to the Label repository with its import-linter
contract. It is not installed in `concord-label`, there is no Training nav, and the disabled "Use for
retraining" button is removed. When Phase 3 is scoped (PLAN.md Q5), Label gets a `concord-label-full`
image and the feature flag back.

---

## 4. Handoff: the reference package

### 4.1 Format

A reference package is a folder or zip that Bench's **existing** ingest can read, plus a manifest. No new
parser is needed in Bench. The format is defined in `CONTRACTS.md` (§2.3); Label's writer and Bench's
reader are each tested against the same sample packages.

```
<dataset>-refs-v<N>/
  manifest.json      format "concord-reference-package", version 1; source (Label install id, dataset uid,
                     name), export version N, created_at, scope, label types, counts (files, segments,
                     hours), review stats, member models of the label runs, normaliser hint
  audio/             originals at their dataset paths (optional when Bench already has every file, §4.2)
  files.csv          path, sha256, duration, sample_rate
  refs/segments.seglst.json   speaker-attributed transcripts (cpWER, WER, DER)
  refs/refs.rttm              diarisation only (scope `complete`)
  refs/segments.jsonl         timed text without speakers, for ASR-only datasets (scope `any`)
  refs/uids.jsonl             segment uid per (file, start, end), so flags can come back (§4.5)
  outputs/           optional: seg-model outputs on whole files (§4.4)
```

**Decided: packages from other sources are accepted.** Bench takes a package written by hand or by
another tool, not only one sent from Label. Only `audio/` (or files Bench already has) and one file under
`refs/` are required. A manifest without a `source` makes a dataset with no provenance, so it has no Label
links and no flags round trip. Without a manifest at all, Bench treats the folder like an annotated
upload. A template package is generated with the other templates (`fixtures/generated/templates`).

What Label puts in follows today's promotion rules (PLAN.md §6.6):
- only **confirmed** segments; auto-accepted segments never become references;
- scope `complete` (default): fully reviewed files only, so DER is meaningful;
- scope `any`: every file with a confirmed segment, ASR only;
- pseudo-label segments keep their unpadded turn times.

### 4.2 Sending (Label)

"Send to Bench" on the Live evaluation and dataset pages. The form has name, scope, tags, include audio
(auto / always / never), include model outputs, and a preview of counts. Two ways out:
- **Push**, when `BENCH_URL` is set and Bench answers `/api/config` with a contract version Label knows.
  Label first asks Bench which sha256s it already has (`POST /api/fingerprints/missing`) and leaves those
  files out. It then uploads the package with Bench's chunked upload API.
- **Download**, otherwise. Label writes the zip under its data directory and offers it for download. This is
  for another machine or the offline setup.

Every send is recorded (`reference_export`): version, scope, counts, destination, Bench's dataset id if pushed.

### 4.3 Receiving (Bench)

- Pushed packages and uploaded zips or folders both arrive in **Datasets → Import reference package** as
  staged imports with a preview: source, version, files, hours, label types, how many files are new, and
  differences from the previous version.
- **Accept** runs the normal ingest job on the package folder, then stores the provenance on the dataset.
- **Versions.** References that have been scored are never changed, so benchmark numbers stay reproducible.
  A package from the same source becomes a new dataset (`<name> v2`) linked to the earlier versions.
  Accepting offers "Run the models benchmarked on v1 against v2" (the existing benchmark-plan panel,
  pre-ticked).
- Audio files Bench already has (same sha256) are not stored again; derived files and the cache are reused.

### 4.4 Model outputs in the package (optional)

Each app has its own output cache. Seg-model outputs on whole files depend only on (model, audio sha256,
whole file, params, image tag), so they mean the same in both apps. The package carries each output with
its params and image tag written out in full, not as Label's cache hash, because the two apps' cache code
may change separately. Bench computes its own key from them and imports the output when the image tag
matches the one it would use. Bench then does not re-run diarisation it would get the same answer from.
ASR outputs are not carried: Label transcribed padded cuts, and Bench runs ASR on the reference spans
anyway (PLAN.md §6.6).

### 4.5 Flags back to Label

Bench's "Flag reference as wrong" is useful to the person who labelled the data. A dataset that came from
Label gets "Send flags to Label": a small JSON of (segment uid, file sha256, note), pushed to `LABEL_URL`
(`POST /api/flags/import`) or downloaded. Label imports it as flags on the matching segments, which then
show in the annotator's flags lane. The next reference package from that dataset is a new version (§4.3).

---

## 5. Links between the apps

Both links are optional. Each app works fully on its own.

### 5.1 Settings

| Setting | App | Meaning |
|---|---|---|
| `BENCH_URL` | Label | Where to push reference packages and read locked scores |
| `LABEL_URL` | Bench | Where "Open in Label" and "Send flags to Label" go |

`GET /api/config` reports the app, its version, the contract version, the other app's URL and whether it
answered, so the UI shows or hides each link.

### 5.2 Open in Label

Bench's error pages show "Open in Label" for a dataset whose references came from Label. The link is
`LABEL_URL/label/file/by-sha/<sha256>?dataset_uid=<uid>&t=<start>`, so it does not depend on Label's database ids. Label
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

The platform, which both apps already call, keeps track of who is using what. No code is shared between
the apps for this.

- **`stack.py up NAME... --holder <app>:<job>`** records a lease on the named services in the platform's
  own run directory, then starts them. A service leased by another holder is never stopped; if the request
  does not fit next to leased services, `up` refuses with the usual message and the job is `blocked`
  (existing behaviour), to be retried when the other job ends.
- **`stack.py lease --holder <app>:<job>`** renews the holder's leases. A lease expires two minutes after its
  last renewal, so a crashed worker cannot hold services forever. Each app's worker renews once a minute
  while a job runs.
- **`stack.py release --holder <app>:<job>`** ends the leases when the job finishes.
- Each app's `ensure_services` passes `--holder` and runs the renew loop. The platform changes are three
  small commands with tests; both apps' changes are a few lines each.

---

## 7. Offline export after the split

**Decided: both apps can write a kit.** Each app keeps its copy of the offline kit code
(OFFLINE_EXPORT_PLAN.md) and its export page. A full kit holds the app images that are ticked: this app,
the other app, or both, with one `concord/compose.yml` that starts what is in the kit and sets
`BENCH_URL` / `LABEL_URL` between them. How to run each app (image, port, environment, mounts) is a
contract (§2.3), so either app can write the compose entry for the other. Model kits are unchanged. The
air-gap test (`just test-offline`) stays in the Bench repository and gains a variant with both images.

---

## 8. Starting empty

**Decided: both apps start empty**, with new data directories and databases. Nothing is migrated from
today's `data/concord.db`.

- Today's data stays where it is, untouched. The last single-app commit is tagged (`concord-single-app`) so
  the old app can still be run against it.
- Datasets are uploaded again: annotated ones to Bench, audio for labelling to Label. Models are registered
  again in each app (no typed fields, PLAN.md §5 M3).
- Labelling work in progress can be carried over with today's label package export before the switch, then
  imported into Label (PLAN.md §6, Import / export). The package format is unchanged, so this needs no new code.

---

## 9. Milestones

Each one ends with `just test`, `just lint` and the e2e tests green in the repository it touches.

| # | Repo | Work | Exit | Size |
|---|---|---|---|---|
| S1 | Bench | Tag `concord-single-app`. Write `CONTRACTS.md` v1 and `contracts/samples/` (reference package, flags file, endpoints, kit entry) | Samples pass a small checker; existing tests still pass | 2 d |
| S2 | Label | Clone the repository; rename the package `concord_label`; remove benchmark code, pages and fixtures source (§2.2); move `prepare_cuts` and `usable_files` to `cuts.py`; own port, data directory and nav; one new initial migration; promote removed; Improve placeholder not installed | Label runs alone; label tests and label e2e pass | 3–4 d |
| S3 | Bench | Rename the package `concord_bench`; remove label code and pages, audio-only upload, `concord_improve`, the lite/full split; one new initial migration | Bench runs alone; Phase 1 acceptance test and smoke e2e pass | 3 d |
| S4 | both | Reference package: Label export (download, push), Bench import with preview, versions, optional outputs, hand-made packages and template | Contract tests and the round trip (§10) | 4–5 d |
| S5 | both | Links: `BENCH_URL` / `LABEL_URL`, locked score, Open in Label, flags back | Both links work with the other app up, and hide with it down | 2–3 d |
| S6 | platform + both | `stack.py up --holder`, `lease`, `release`; renew loop in each app | Lease tests (§10) | 2 d |
| S7 | both | Images, offline kits with either or both apps, READMEs, `just check-contracts` | Both images build; air-gap test with both images passes | 2–3 d |

About 3.5–4 weeks for one developer. S2 and S3 can run side by side once S1 is done.

---

## 10. Testing

- **Moved tests**: each repository keeps the tests for the code it keeps, unchanged in substance.
- **Contract tests** (in both repositories, on `contracts/samples/`):
  - Label: a package it writes matches the sample layout and validates; it imports the sample flags file.
  - Bench: every sample package imports with the expected segments, including a hand-made one (refs only,
    no manifest) and the template; an unknown format version is refused; the flags file it writes matches
    the sample.
  - `just check-contracts` finds no difference between the two repositories.
- **Round trip** (Label repository, `just e2e-handoff`, with the `concord-bench` image built from the Bench
  repository, both on one Docker network, recorded responses):
  - pseudo-label and confirm in Label;
  - send to Bench, both as a downloaded zip and as a push;
  - accept in Bench and benchmark one ASR model;
  - the WER must equal what today's Promote path gives on the same confirmed segments;
  - send a second version, and check that v1's scores are unchanged and v2 is a new dataset.
- **Missing-audio push**: Bench already has every file, so the package carries no audio and the import
  still succeeds.
- **Links**: live evaluation with Bench up shows the locked score, and with Bench down hides it. Flags
  sent from Bench show in Label's flags lane.
- **Leases** (platform tests, Docker mocked): a lease protects a service from eviction by another holder;
  an expired lease does not; `release` frees it.
- **e2e**: today's smoke spec in Bench and label spec in Label, plus the handoff spec above.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| A fix in the copied code (adapters, ingest, registry, cache, offline) is made in one repository and forgotten in the other | The READMEs list the copied modules (§2.2). A fix there is checked against the other repository in the same sitting. The copies are allowed to differ where the apps need different things. |
| The two apps stop agreeing on a format or endpoint | `CONTRACTS.md` with a version, sample files tested on both sides, `just check-contracts`, and the version in `/api/config` so a mismatch is refused with a message (§2.3). |
| Two workers evict each other's services | Leases in the platform (§6). |
| Benchmark numbers change when references are re-sent | A new package from the same source is a new dataset version; scored references are never edited (§4.3). |
| Losing cache reuse between benchmarking and pseudo-labelling costs GPU time | Seg-model outputs travel in the package (§4.4); Label's own cache still serves re-fusing. |
| Re-uploading datasets and re-registering models after the switch (§8) | Upload and Add model need no typed fields; labelling in progress moves with a label package. |
