#!/usr/bin/env bash
# Installs a Concord offline kit. Written by Concord's offline export; see README.md beside it.
#
#   ./install.sh [--internal] [--skip-verify]
#       Full kit: this folder becomes the install. Loads the images, writes platform/.env and
#       concord/.env, creates the platform's Docker network (--internal: no outside access) and
#       starts Concord. CONCORD_PORT (8600) and CONCORD_PROJECT (concord) can be set in the environment.
#   ./install.sh --add INSTALL [--replace] [--skip-verify]
#       Model kit: merges these models into INSTALL, the folder a full kit was installed from.
#       A service INSTALL already has with a different image is refused unless --replace is given.
set -euo pipefail

KIT_KIND="@KIND@"
KIT_NAME="@NAME@"
CONCORD_IMAGE="@CONCORD_IMAGE@"
GPU_SERVICES="@GPU_SERVICES@"

KIT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ADD="" REPLACE=0 INTERNAL=0 VERIFY=1
while [ $# -gt 0 ]; do
  case "$1" in
    --add) ADD="${2:?--add needs the install folder}"; shift 2 ;;
    --replace) REPLACE=1; shift ;;
    --internal) INTERNAL=1; shift ;;
    --skip-verify) VERIFY=0; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

say() { printf '== %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# A top-level `key: value` of a YAML file (config.yaml has no nesting).
config_value() { sed -n "s/^$2:[[:space:]]*\([^#[:space:]]*\).*/\1/p" "$1" | head -n 1; }

check_docker() {
  command -v docker >/dev/null || die "docker is not installed"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not reachable"
  docker compose version >/dev/null 2>&1 || die "the Docker compose plugin is missing"
}

# Warn when the GPU differs from the one the kit's VRAM shares were written for.
check_gpu() {
  [ -n "$GPU_SERVICES" ] || return 0
  if ! command -v nvidia-smi >/dev/null; then
    warn "nvidia-smi not found: $GPU_SERVICES need an NVIDIA GPU and the container toolkit"
    return 0
  fi
  local total_gb mib driver
  total_gb="$(config_value "$1" gpu_total_gb)"
  read -r mib driver < <(nvidia-smi --query-gpu=memory.total,driver_version --format=csv,noheader,nounits | head -n 1 | tr -d ',')
  if awk -v mib="$mib" -v gb="$total_gb" 'BEGIN { d = mib / 1024 - gb; exit !(d > 1.5 || d < -1.5) }'; then
    warn "the GPU has $((mib / 1024)) GB but $1 says gpu_total_gb: $total_gb. gpu.util and util_limit are fractions of it, so vLLM services may not start; edit gpu_total_gb (and util_limit) there."
  fi
  if [ "${driver%%.*}" -lt 580 ] 2>/dev/null; then
    warn "NVIDIA driver $driver: the CUDA 13 images need driver >= 580"
  fi
}

verify() {
  if [ "$VERIFY" = 0 ]; then warn "checksums not verified (--skip-verify)"; return 0; fi
  command -v sha256sum >/dev/null || die "sha256sum not found (coreutils); use --skip-verify to install unverified"
  say "verifying checksums of $KIT_NAME (SHA256SUMS)"
  (cd "$KIT" && sha256sum --quiet -c SHA256SUMS) || die "checksums do not match: the kit is damaged or incomplete; copy it again"
}

install_full() {
  [ "$KIT_KIND" = full ] || die "$KIT_NAME is a model kit: run ./install.sh --add /path/to/install"
  local net port project
  net="$(config_value "$KIT/platform/config.yaml" network)"
  port="${CONCORD_PORT:-8600}"
  project="${CONCORD_PROJECT:-concord}"
  if [ -f "$KIT/concord/.env" ]; then
    say "already installed here: skipping checksums and docker load"
  else
    verify
    say "loading images (this can take a while)"
    docker load -i "$KIT/images/concord.tar"
    docker load -i "$KIT/images/platform.tar"
  fi
  check_gpu "$KIT/platform/config.yaml"

  say "writing platform/.env and concord/.env"
  mkdir -p "$KIT/assets/hf-cache"  # engine services mount it; Concord sees assets/ read-only
  printf 'AI_ASSETS=%s\nHF_HUB_OFFLINE=1\n' "$KIT/assets" > "$KIT/platform/.env"
  printf 'KIT_DIR=%s\nCONCORD_IMAGE=%s\nCONCORD_PORT=%s\nCONCORD_PROJECT=%s\nAI_NETWORK=%s\n' \
    "$KIT" "$CONCORD_IMAGE" "$port" "$project" "$net" > "$KIT/concord/.env"

  if ! docker network inspect "$net" >/dev/null 2>&1; then
    if [ "$INTERNAL" = 1 ]; then
      say "creating Docker network $net (internal: no outside access)"
      docker network create --internal "$net" >/dev/null
    else
      say "creating Docker network $net"
      docker network create "$net" >/dev/null
    fi
  fi
  say "starting Concord"
  docker compose --env-file "$KIT/concord/.env" -f "$KIT/concord/compose.yml" up -d
  echo "Concord: http://127.0.0.1:$port. Register each model with Add model."
}

install_add() {
  [ "$KIT_KIND" = models ] || warn "$KIT_NAME is a full kit; merging only its models"
  [ -d "$ADD" ] || die "no folder $ADD"
  local inst image replace=()
  inst="$(cd "$ADD" && pwd)"
  [ -f "$inst/platform/config.yaml" ] && [ -f "$inst/concord/.env" ] ||
    die "$inst is not an offline install (no platform/config.yaml or concord/.env); install a full kit there first"
  image="$(sed -n 's/^CONCORD_IMAGE=//p' "$inst/concord/.env")"
  docker image inspect "$image" >/dev/null 2>&1 || die "the install's Concord image $image is not loaded"
  [ "$REPLACE" = 1 ] && replace=(--replace)

  # The merge rules live in Concord: run them in its image. It runs as root, because Concord's platform
  # renders files as root, and gives what it touches back to the owner of the install folder.
  merge() {
    docker run --rm --network none -e UV_OFFLINE=1 -v "$KIT:$KIT:ro" -v "$inst:$inst" "$image" \
      python -m concord_bench.cli add-kit "$KIT" "$inst" ${replace[@]+"${replace[@]}"} "$@"
  }
  verify
  check_gpu "$inst/platform/config.yaml"
  say "checking $KIT_NAME against $inst"
  merge --check
  say "loading images (this can take a while)"
  docker load -i "$KIT/images/platform.tar"
  say "merging into $inst"
  merge
  echo "Done. Register the new models in Concord with Add model."
}

check_docker
if [ -n "$ADD" ]; then install_add; else install_full; fi
