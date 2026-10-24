# syntax=docker/dockerfile:1
# Concord Bench image:
#   just build   ->  concord-bench:0.2.0
#
# The platform's aicore client is a sibling repo, so it comes in through a named build context:
#   docker build --build-context platform=../../platform --target bench -t concord-bench:0.2.0 .
#
# Running: join ai-net, where the model catalog answers at http://catalog:8700 (it starts models and
# routes every call). The platform directory at /platform and the Docker socket are only needed to write
# offline kits (the platform's tools/bundle.py). See README.md.

ARG PYTHON=python:3.12.15-slim-bookworm
ARG NODE=node:22.23.3-bookworm-slim

# ---------------------------------------------------------------- frontend
FROM ${NODE} AS web-base
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./

FROM web-base AS web
RUN npm run build

# ---------------------------------------------------------------- tools copied into the images
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv
FROM docker:29-cli AS dockercli

# ---------------------------------------------------------------- Python environments (built with a compiler)
FROM ${PYTHON} AS pybuild
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PROJECT_ENVIRONMENT=/opt/venv
RUN apt-get update && apt-get install -y --no-install-recommends build-essential && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
# The platform tools' own environment: at run time bundle.py (offline kits) uses it with `uv run --no-sync`,
# so the mounted platform's .venv on the host is never rebuilt from inside the container.
COPY --from=platform pyproject.toml uv.lock /opt/platform-src/
COPY --from=platform clients/aicore /opt/platform-src/clients/aicore
COPY --from=platform custom/_bases/common /opt/platform-src/custom/_bases/common
RUN UV_PROJECT_ENVIRONMENT=/opt/platform-venv uv sync --project /opt/platform-src --frozen --no-dev --no-editable
# pyproject's path dependency is ../../platform/clients/aicore: from /srv/apps/concord that is
# /srv/platform/clients/aicore (installed non-editable; the platform itself is mounted at /platform).
COPY --from=platform clients/aicore /srv/platform/clients/aicore
WORKDIR /srv/apps/concord
COPY pyproject.toml uv.lock README.md ./
COPY backend/ backend/

FROM pybuild AS venv
RUN uv sync --frozen --no-dev --no-editable

# ---------------------------------------------------------------- runtime
FROM ${PYTHON} AS bench
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/opt/venv/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libsndfile1 && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
# docker CLI + compose plugin: offline kits save images from the host's daemon (socket mounted)
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose
COPY --from=pybuild /opt/platform-venv /opt/platform-venv
COPY --from=venv /opt/venv /opt/venv
WORKDIR /srv/apps/concord
COPY README.md ./
COPY fixtures/generated/templates fixtures/generated/templates
COPY --from=web /web/dist frontend/dist
# UV_OFFLINE: `uv run --no-sync` for the platform tools never reaches for the network (offline kits).
# CATALOG_URL: the model catalog on ai-net; PUBLIC_URL: where Concord Label reads Bench's outputs.
ENV CONCORD_HOME=/srv/apps/concord CONCORD_DATA=/data PLATFORM_DIR=/platform PLATFORM_VENV=/opt/platform-venv \
    AI_HOST={name} AICORE_ENDPOINTS=/platform/clients/aicore/src/aicore/endpoints.generated.yaml UV_OFFLINE=1 \
    CATALOG_URL=http://catalog:8700 PUBLIC_URL=http://concord-bench:8600
VOLUME /data
EXPOSE 8600
CMD ["sh", "-c", "python -m concord_bench.cli migrate && (python -m concord_bench.runs.worker &) && exec uvicorn concord_bench.api.app:app --host 0.0.0.0 --port 8600"]
