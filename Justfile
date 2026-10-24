# Concord Bench. Run `just` for the recipe list. See README.md.

set dotenv-load := true
set shell := ["bash", "-euo", "pipefail", "-c"]

py := "uv run --quiet python"
recorded := "fixtures/recorded"

# list recipes
default:
    @just --list --unsorted

# install backend (uv) and frontend (npm) dependencies
setup:
    uv sync
    cd frontend && npm install

# API (reload) + worker + Vite dev server; Ctrl-C stops all three
dev:
    #!/usr/bin/env bash
    set -euo pipefail
    trap 'kill 0' EXIT INT TERM
    {{py}} -m concord_bench.cli migrate
    uv run uvicorn concord_bench.api.app:app --reload --reload-dir backend --port ${CONCORD_PORT:-8600} &
    {{py}} -m concord_bench.runs.worker &
    (cd frontend && npm run dev -- --port ${CONCORD_WEB_PORT:-5173}) &
    wait

# API only (serves the built frontend from frontend/dist when present)
api:
    {{py}} -m concord_bench.cli migrate
    uv run uvicorn concord_bench.api.app:app --port ${CONCORD_PORT:-8600}

# job worker only
worker:
    {{py}} -m concord_bench.runs.worker

# bring the database to the latest schema
migrate:
    {{py}} -m concord_bench.cli migrate

# new Alembic revision from model changes: just revision "add foo"
revision message:
    {{py}} -m concord_bench.cli revision "{{message}}"

# backend tests (no GPU / platform needed) + frontend unit tests
test *args:
    uv run pytest {{args}}
    cd frontend && npm run test --silent

# adapter tests against the live model catalog and its services (the lease starts what they need)
test-live *args:
    uv run pytest -m live -o addopts="--import-mode=importlib" {{args}}

# M7 performance check at realistic size (synthetic database, no audio)
test-perf:
    uv run pytest -m perf -o addopts="--import-mode=importlib" -s tests/test_perf.py

# Playwright smoke tests (acceptance item 3) on replayed fixtures. The API runs in the concord-bench image
# (`just build`) with this checkout's backend, tests and frontend/dist mounted; browsers run in the
# Playwright image (no host libs).
e2e: build-web
    #!/usr/bin/env bash
    set -euo pipefail
    docker network create concord-e2e >/dev/null 2>&1 || true
    docker rm -f concord-e2e-api >/dev/null 2>&1 || true
    trap 'docker rm -f concord-e2e-api >/dev/null 2>&1' EXIT
    docker run -d --name concord-e2e-api --network concord-e2e -e PYTHONPATH=/srv/apps/concord/backend \
      -v "$PWD/backend":/srv/apps/concord/backend:ro -v "$PWD/fixtures":/srv/apps/concord/fixtures:ro \
      -v "$PWD/tests":/srv/apps/concord/tests:ro -v "$PWD/frontend/dist":/srv/apps/concord/frontend/dist:ro \
      concord-bench:0.2.0 python tests/e2e_server.py --host 0.0.0.0 --port 8611 >/dev/null
    until docker logs concord-e2e-api 2>&1 | grep -q "e2e data ready"; do
      docker ps -q -f name=concord-e2e-api | grep -q . || { docker logs concord-e2e-api; exit 1; }
      sleep 1
    done
    docker run --rm --network concord-e2e --ipc=host -v "$PWD/frontend":/work -w /work \
      -v "$PWD/contracts":/contracts:ro -e E2E_CONTRACTS=/contracts \
      -e E2E_BASE_URL=http://concord-e2e-api:8611 mcr.microsoft.com/playwright:v1.64.1-noble npx playwright test

# ruff + frontend typecheck
lint:
    uv run ruff check backend tests fixtures/synth
    cd frontend && npm run lint --silent

# check contracts/samples against CONTRACTS.md
check-contracts:
    {{py}} contracts/check.py

# regenerate fixtures/generated with kokoro-tts (start it first: `just up kokoro-tts` in the platform)
fixtures:
    {{py}} fixtures/synth/make.py

# re-record adapter responses through the live model catalog into fixtures/recorded (it starts what is needed)
record:
    {{py}} -m concord_bench.cli record --out {{recorded}}

# frontend production build into frontend/dist
build-web:
    cd frontend && npm run build

# concord-bench image; the platform's aicore client comes in as a named build context
build:
    docker build --build-context platform=${PLATFORM_DIR:-../../platform} --target bench -t concord-bench:0.2.0 .
