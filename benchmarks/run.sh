#!/usr/bin/env bash
set -Eeuo pipefail

script_directory="$(
    cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
    pwd -P
)"
repository_root="$(cd -- "${script_directory}/.." && pwd -P)"
output_directory="${PGLC_BENCH_OUTPUT_DIR:-${script_directory}/results}"
secret_directory="$(mktemp -d -t pg_local_cache_benchmark.XXXXXX)"
project_name="pg_local_cache_benchmark_${$}_${RANDOM}"
pglc_image="pg_local_cache:benchmark-${$}"
runner_image="pg_local_cache-benchmark-runner:${$}"
postgres_image="${PGLC_BENCH_POSTGRES_IMAGE:-postgres:16.14-bookworm}"
valkey_image="${PGLC_BENCH_VALKEY_IMAGE:-valkey/valkey:9.1.1-trixie}"
redis_image="${PGLC_BENCH_REDIS_IMAGE:-redis:8.8.1-trixie}"

cleanup() {
    local status="$?"
    trap - EXIT INT TERM
    docker compose \
        --project-name "$project_name" \
        --file "${script_directory}/compose.yaml" \
        down --volumes --remove-orphans >/dev/null 2>&1 || true
    docker image rm "$pglc_image" "$runner_image" >/dev/null 2>&1 || true
    rm -f -- \
        "${secret_directory}/postgres_password" \
        "${secret_directory}/pg_local_cache_auth_token"
    rmdir -- "$secret_directory" 2>/dev/null || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

command -v docker >/dev/null
docker compose version >/dev/null
command -v openssl >/dev/null

umask 077
mkdir -p -- "$output_directory"
output_directory="$(cd -- "$output_directory" && pwd -P)"
rm -f -- \
    "${output_directory}/comparison.json" \
    "${output_directory}/comparison.md" \
    "${output_directory}/failure.json" \
    "${output_directory}/failure.md" \
    "${output_directory}/process-failure.txt" \
    "${output_directory}/.comparison.json.tmp" \
    "${output_directory}/.comparison.md.tmp" \
    "${output_directory}/.failure.json.tmp" \
    "${output_directory}/.failure.md.tmp"

postgres_password="$(openssl rand -hex 32)"
auth_token="$(openssl rand -hex 32)"
printf '%s\n' "$postgres_password" \
    > "${secret_directory}/postgres_password"
printf '%s\n' "$auth_token" \
    > "${secret_directory}/pg_local_cache_auth_token"
chmod 0600 \
    "${secret_directory}/postgres_password" \
    "${secret_directory}/pg_local_cache_auth_token"

export PGLC_BENCH_AUTH_TOKEN="$auth_token"
export PGLC_BENCH_POSTGRES_PASSWORD="$postgres_password"
export PGLC_BENCH_SECRET_DIR="$secret_directory"
export PGLC_BENCH_OUTPUT_DIR="$output_directory"
export PGLC_BENCH_UID="$(id -u)"
export PGLC_BENCH_GID="$(id -g)"
export PGLC_BENCH_POSTGRES_IMAGE="$postgres_image"
export PGLC_BENCH_VALKEY_IMAGE="$valkey_image"
export PGLC_BENCH_REDIS_IMAGE="$redis_image"
export PGLC_BENCH_PGLC_IMAGE="$pglc_image"
export PGLC_BENCH_RUNNER_IMAGE="$runner_image"
export PGLC_BENCH_DOCKER_VERSION="$(
    docker version --format '{{.Server.Version}}'
)"
export PGLC_BENCH_COMPOSE_VERSION="$(docker compose version --short)"

compose=(
    docker compose
    --project-name "$project_name"
    --file "${script_directory}/compose.yaml"
)

docker pull "$postgres_image"
"${compose[@]}" pull valkey redis
"${compose[@]}" build pg-local-cache benchmark

image_identity() {
    docker image inspect --format \
        '{{if .RepoDigests}}{{index .RepoDigests 0}}{{else}}{{.Id}}{{end}}' \
        "$1"
}

export PGLC_BENCH_POSTGRES_IMAGE_IDENTITY="$(image_identity "$postgres_image")"
export PGLC_BENCH_VALKEY_IMAGE_IDENTITY="$(image_identity "$valkey_image")"
export PGLC_BENCH_REDIS_IMAGE_IDENTITY="$(image_identity "$redis_image")"
export PGLC_BENCH_PG_LOCAL_CACHE_IMAGE_IDENTITY="$(
    image_identity "$pglc_image"
)"
export PGLC_BENCH_RUNNER_IMAGE_IDENTITY="$(image_identity "$runner_image")"

read -r PGLC_BENCH_HARNESS_SHA256 _ < <(
    openssl dgst -sha256 -r "${script_directory}/compare.py"
)
export PGLC_BENCH_HARNESS_SHA256
PGLC_BENCH_SOURCE_REVISION="unknown"
if command -v git >/dev/null; then
    PGLC_BENCH_SOURCE_REVISION="$(
        git -C "$repository_root" rev-parse --verify HEAD 2>/dev/null ||
            printf 'unknown'
    )"
    if [[ -n "$(git -C "$repository_root" status --porcelain 2>/dev/null)" ]]; then
        PGLC_BENCH_SOURCE_REVISION="${PGLC_BENCH_SOURCE_REVISION}-dirty"
    fi
fi
export PGLC_BENCH_SOURCE_REVISION

"${compose[@]}" up --detach --wait \
    pg-local-cache postgres-plain valkey redis
benchmark_status=0
"${compose[@]}" run --rm benchmark || benchmark_status="$?"
if ((benchmark_status != 0)) && \
    [[ ! -f "${output_directory}/comparison.json" ]] && \
    [[ ! -f "${output_directory}/failure.json" ]]; then
    printf 'benchmark process exited with status %s before writing a report\n' \
        "$benchmark_status" > "${output_directory}/process-failure.txt"
fi
if ((benchmark_status != 0)); then
    exit "$benchmark_status"
fi

printf 'Benchmark reports: %s/comparison.json and %s/comparison.md\n' \
    "$output_directory" "$output_directory"
