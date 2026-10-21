# Concord — Offline export plan

Planned 2026-10-18. O1–O3 built 2026-10-18; O4 (the live `whisper-large-v3` run) is still to do, see §11. Kept apart from [PLAN.md](PLAN.md), which covers Phases 1–3; references
to it are written as "PLAN.md §N". Section numbers without a file name are in this document.

Take Concord and chosen models to a separate machine with no internet access, where other datasets are
benchmarked and labelled. The kit carries the application, the models' Docker images, their weights and
the platform files needed to start them. Labels come back with label packages (PLAN.md §6), which already work
between installs.

---

## 1. What the offline machine needs

Concord does not run models (PLAN.md §1), so a kit must hold everything `stack.py` and the services need. None of
it can be downloaded there.

| Piece | Today | Consequence |
|---|---|---|
| Concord image | `concord-lite:0.1.0`, 1.8 GB | Already carries the platform tools' environment (`/opt/platform-venv`, run with `--no-sync`). Add `UV_OFFLINE=1` so uv never tries the network. |
| Platform directory | `../../platform` | `stack.py up` validates the whole tree, which needs `tools/`, `engines/`, `config.yaml` and, per custom service, `custom/<name>/` with its Dockerfile plus `custom/_bases/`. It re-renders `compose.yml` in place and **builds** any image it cannot find, which fails offline. |
| Model images | local Docker | `vllm/vllm-openai:v0.31.0` alone is 32 GB. Custom images share a 12 GB CUDA base; one `docker save` call keeps shared layers once. |
| Weights | `$AI_ASSETS` | Three layouts: custom services use `weights.path`; vLLM with `model: /models/X` uses `$AI_ASSETS/X`; vLLM with a Hugging Face id (`qwen3-asr-1-7b`, `whisper-large-v3-turbo`) uses `$AI_ASSETS/hf-cache`. |
| Offline engines | — | vLLM containers get no `HF_HUB_OFFLINE`, so they try the hub on start. Custom images already set it. |

The platform's `tools/bundle.py` (`just bundle NAME... --with-images`) writes manifests, a standalone
compose file and the endpoint map. It has no weights and no runnable platform tree, and it assumes Hugging
Face models download on first start. §4 extends it instead of writing a second exporter.

## 2. Choosing what to export

- **The user picks every model by hand.** The export form lists every service in the platform registry
  with its image and weight sizes. A Concord registration is not required: a newly released model that was
  just added to the platform (manifest, `just fetch-weights`, `just build`) can be exported straight away.
  Nothing is added automatically, including the other half of a pipeline. The preview only warns:
  image not present locally, weights missing, or the set does not fit under `util_limit` together
  (a warning only; `stack.py` starts the subset a job needs).
- **Two kinds of kit.**
  - *Full kit*: Concord image, platform tree and the chosen models. Used once, to set up the offline machine.
  - *Model kit*: the chosen models only. Adds models to an offline machine that already has a full kit,
    for example when a new model is released.
- **No Concord state travels.** No registrations, scores, model outputs or datasets. On the offline
  machine the user registers each model with Add model, so the compatibility check runs on the hardware
  and images that will be used there. The check clip is in the image, and Add model needs no typed fields.

## 3. Kit layout

A directory, not a tarball: at tens of GB, packing doubles disk use and time.

```
concord-offline-<name>-<date>/
  README.md                restore steps, service table, VRAM budget
  manifest.json            kind (full | models), versions, services, image refs + digests, sizes, sha256 of every file
  install.sh               full kit: load images, set up platform/ and assets/, write .env, start Concord
                           model kit: `install.sh --add <offline install>` merges into an existing install
  images/concord.tar       full kit only: docker save of the Concord image
  images/platform.tar      one docker save of every chosen model's image
  platform/                trimmed platform tree holding only the chosen services
  assets/                  weights in AI_ASSETS layout, so AI_ASSETS=<kit>/assets works as-is
  concord/compose.yml      full kit only: runs Concord with the mounts in §6
```

## 4. Platform changes (`tools/bundle.py`, `tools/render.py`)

1. **`--with-weights`** copies each service's weights into `assets/` at the same relative path.
   - Hugging Face ids: write a fresh cache entry with real files in `snapshots/<rev>/` and a `refs/main`.
     Here snapshot files are symlinks into `blobs/`, the blobs are hardlinks into a shared `hub/blobs`, and
     `qwen3-asr-1-7b` has no `refs/main`. A plain copy would not resolve offline.
   - Sizes use apparent file size: `du` reports 4.4 MB for the 4.7 GB Qwen3-ASR cache entry because of the
     hardlinks.
2. **`--platform-tree`** writes a runnable `platform/`: `tools/`, `engines/`, `config.yaml`, `pyproject.toml`,
   `uv.lock`, `clients/aicore`, `custom/_bases/`, and only the chosen `services/*.yaml` and `custom/<name>/`.
3. **`--dir`** writes a directory instead of a tarball. **`--dry-run --json`** reports services, image refs and
   sizes, weight paths and sizes, and the problems listed in §2. Concord's preview reads it.
4. **Missing images fail the export** (`docker pull` / `just build` first), so the offline side never needs to
   build.
5. **`render.py`**: engine services get `HF_HUB_OFFLINE: ${HF_HUB_OFFLINE:-0}`. The kit's `.env` sets it to 1;
   nothing changes on the source machine.
6. Platform unit tests for each flag, on a fake `AI_ASSETS` (including a symlinked HF cache without
   `refs/main`) and with `docker save` mocked.

## 5. Concord changes

**Backend** (`concord/offline/`)
- `export.py`: run `bundle.py --dir --with-images --with-weights --platform-tree` with the chosen names
  (the same way `ensure_services` runs `stack.py`), `docker save` the Concord image for a full kit, then
  write `compose.yml`, `install.sh` and `manifest.json` with checksums.
- Job type `export` in the existing worker. Progress by stage (images, weights, checksums) through
  `paths.job_progress`; `bundle.py` output streams into the job log.
- API:
  - `GET /api/offline/preview?services=…&kind=…`: sizes, free space at the destination, warnings.
  - `POST /api/offline/exports`: starts the job (services, kind, destination, Concord image).
  - `GET /api/offline/exports`: history, with path, size and status. Same history pattern as `SyncPackage`.
- `python -m concord.cli export-offline NAME... --kind full|models --out DIR`, and `just export-offline`.
- `python -m concord.cli verify-kit DIR` checks `manifest.json` checksums. `install.sh` runs it first.

**Frontend**
- New *Export for offline* page under Bring your own: a registry table with checkboxes (service, kind,
  task, image size, weight size, registered in Concord or not), kit kind, Concord image, destination
  folder, and a size total with warnings.
- Progress on the Jobs page. When done, the page shows the kit path and size. Nothing goes through the
  browser.

## 6. Installing on the offline machine

- **Full kit**: `install.sh` verifies checksums, runs `docker load` for both tars, writes `platform/.env`
  (`AI_ASSETS=<kit>/assets`, `HF_HUB_OFFLINE=1`), creates `ai-net` and starts Concord from
  `concord/compose.yml`. Then the user registers models with Add model.
- **Model kit**: `install.sh --add <install>` loads `platform.tar`, copies `assets/`, `services/*.yaml` and
  `custom/<name>/` into the install, and runs the platform's `just validate`. A service already present
  with a different image tag is refused unless `--replace` is given.
- **Same-path mounts.** `stack.py` runs `docker compose` inside the Concord container, but the host daemon
  resolves the `${AI_ASSETS}` bind mounts. So `platform/` and `assets/` are mounted into the Concord container
  at the same absolute path they have on the host, and `PLATFORM_DIR` points there. `compose.yml` does this.
- **Exporting from a containerised Concord** additionally needs `$AI_ASSETS` mounted read-only and an output
  folder mounted. In development (Concord on the host) none of this applies.

## 7. Milestones

| # | Work | Exit |
|---|---|---|
| O1 | Platform flags (§4) and their tests | `just bundle silero-vad --dir --with-images --with-weights --platform-tree` writes a kit that starts with `stack.py up silero-vad` from inside it |
| O2 | Concord backend: export job, API, CLI, `verify-kit` | `just export-offline silero-vad --kind full` writes a full kit |
| O3 | `install.sh` (full and `--add`), `compose.yml`, `UV_OFFLINE`, frontend page | The air-gap test in §8 passes |
| O4 | Live run with `whisper-large-v3`, README section | Benchmark on the fixture set matches the source machine |

## 8. Testing

Real exports cover two models only. Everything else is unit-tested without copying real images or
weights.

- **Unit**: kit layout and `manifest.json`; checksum verification; `install.sh --add` merge and refusal
  rules on a fake install; preview warnings; HF cache copy on a fake symlinked cache (§4).
- **Air-gap test, `silero-vad`** (`just test-offline`, marker `offline`, not in `just test`). CPU only, 434 MB
  image and 2.3 MB of weights, so no GPU is needed. Export a full kit with `silero-vad`, install it on a
  Docker network created with `--internal` (no outside access), register the model through the API, run a
  VAD benchmark on the fixture set and compare the score with the source machine's.
- **Live, `whisper-large-v3`** (manual, once, on the 5090). Checks the vLLM path: the 32 GB engine image,
  `/models` weights and `HF_HUB_OFFLINE=1`. Export a model kit, `install.sh --add` it into the install from the
  air-gap test, register, run an ASR benchmark on the fixture set and compare WER.

## 9. Risks

| Risk | Mitigation |
|---|---|
| A kit is very large (vLLM image 32 GB, weights several GB per model) | The preview shows sizes and free space before anything is written; one `docker save` keeps shared layers once. |
| A service still reaches for the network offline | `HF_HUB_OFFLINE=1` for engines, already set in custom images; `UV_OFFLINE=1` in Concord; the air-gap test runs on an `--internal` network. |
| The offline GPU differs (Q1) | `install.sh` compares `nvidia-smi` with `gpu_total_gb` and warns; `config.yaml` in the kit can be edited. |
| A newer driver is needed on the offline machine (CUDA 13 images need driver >= 580) | Checked by `install.sh` and listed in the kit README. |
| A partly downloaded HF cache entry is exported | `bundle.py` checks every file named in `model.safetensors.index.json` exists before copying. |

---

## 10. Still open

| # | Question | Default meanwhile |
|---|---|---|
| Q1 | Does the offline machine have the same 32 GB GPU? `gpu.util` and `util_limit` are fractions of `gpu_total_gb`, so vLLM services may not start on a smaller card. | Same GPU. `install.sh` warns when `nvidia-smi` reports a different size. |
| Q2 | Extend the platform's `tools/bundle.py` (§4), or keep all export code in Concord? | Extend the platform; Concord already drives the platform's own tools. |
| Q3 | Which Concord image goes into a full kit? | `concord-lite`; `concord-full` when chosen in the export form. |
| Q4 | How is the kit carried (one directory, or archives split for a size-limited drive)? | A plain directory with checksums and a `verify` step. |

## 11. As built (2026-10-18)

| # | Status |
|---|---|
| O1 | Done. `bundle.py` gained `--dir [PATH]`, `--with-weights`, `--platform-tree`, `--dry-run --json` and `--all`; 11 platform tests. The exit command wrote a kit whose `stack.py up silero-vad` started healthy from inside it. |
| O2 | Done. `concord/offline/` (`export`, `kit`, `merge`), job type `export`, `/api/offline/{services,preview,exports}`, `export-offline`, `verify-kit`, `add-kit`. `just export-offline silero-vad --kind full` wrote a 0.56 GB kit in 10 s. |
| O3 | Done. `install.sh` (full and `--add`), `concord/compose.yml`, `UV_OFFLINE=1` in the image, the *Export for offline* page. `just test-offline` passes: VAD error 0.069221 on the `--internal` network, same as the source machine. |
| O4 | Not run: the GPU was 30.7 of 32 GB in use by running platform services. The README section is written. |

Differences from the plan:
- `install.sh` checks `SHA256SUMS` with `sha256sum` (coreutils), since a full kit's Concord image is
  not loaded yet when it runs. `verify-kit` checks the same checksums in `manifest.json`.
- The `--add` merge rules run as `python -m concord.cli add-kit` inside the install's Concord image, as
  root, then give what they touched back to the owner of the install folder (Concord's platform
  renders files as root). An engine whose image differs between kit and install is refused too, unless
  `--replace`; port and alias clashes are always refused. A failed validation puts the install back.
- The platform's `config.yaml` gained `compose_project` (default `aiplatform`), so a kit can run next to
  the source platform on one Docker daemon (the air-gap test uses it). `render.COMPOSE_PROJECT` is gone.
- The platform tree also holds `contracts/` and `ui/` (needed by `load_platform` and `render.py`).
- Image sizes come from `docker image inspect` and are upper bounds: with the containerd image store
  `docker save` writes compressed layers (silero-vad: 434 MB image, 104 MB tar). Too little free space is
  therefore a warning, not a refusal.
- `GET /api/offline/services` (the form's table, a dry run over every service) was added beside the
  preview, which covers only the chosen services.
- `config.yaml` is covered by the checksums, so the kit README says to edit it after `./install.sh`.

