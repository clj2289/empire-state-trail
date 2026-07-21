#!/usr/bin/env bash
#
# Run the Empire State Trail nearby-POI fetcher inside a throwaway container so
# nothing gets installed on your machine and no build artifacts are left behind.
#
#   - Uses the stock `python:3.12-slim` image (pulled once, cached by Docker).
#   - The container is removed on exit (`--rm`); there is no image to build.
#   - Your repo is bind-mounted read/write, so the script reads est-core.js and
#     writes data/pois-nearby.json into the repo, owned by you.
#   - Linux images ship real CA certs, so verified TLS to Overpass just works —
#     no `--insecure` fallback needed like on macOS Python.
#
# NYC is skipped by default (the fetcher starts above the Bronx/Westchester line).
#
# Usage:
#   tools/fetch_nearby_pois.sh                  # default: skip NYC -> data/pois-nearby.json
#   tools/fetch_nearby_pois.sh --include-nyc    # the whole trail, Battery Park up
#   tools/fetch_nearby_pois.sh --limit 2        # quick test: only the first 2 chunks
#   CONTAINER_ENGINE=podman tools/fetch_nearby_pois.sh   # use podman instead
#
# Any arguments are passed straight through to fetch_nearby_pois.py.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="${CONTAINER_ENGINE:-docker}"
IMAGE="${FETCH_IMAGE:-python:3.12-slim}"

if ! command -v "$ENGINE" >/dev/null 2>&1; then
  echo "error: '$ENGINE' not found. Install Docker Desktop, or set CONTAINER_ENGINE=podman." >&2
  exit 1
fi

echo ">> $ENGINE run ($IMAGE) — output: $ROOT/data/pois-nearby.json" >&2

exec "$ENGINE" run --rm \
  --user "$(id -u):$(id -g)" \
  -e PYTHONUNBUFFERED=1 \
  -v "$ROOT":/work \
  -w /work \
  "$IMAGE" \
  python3 tools/fetch_nearby_pois.py "$@"
