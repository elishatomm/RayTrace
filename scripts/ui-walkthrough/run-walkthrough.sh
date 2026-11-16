#!/usr/bin/env bash
# The UI walkthrough: the workbench API and headless Chromium in one Docker container that has no network.
#
#   scripts/ui-walkthrough/run-walkthrough.sh <output-dir>
#
# Needs docker, npm and frontend/node_modules: run `npm ci` in frontend/ once first (that step needs the network).
#   1. Copies the checkout to a temporary folder, without what .dockerignore leaves out (.git, node_modules, .venv,
#      frontend/dist, local_data, caches, databases, .env). Nothing is written inside the repository.
#   2. Builds the frontend there with a copy of frontend/node_modules (npm run build: typecheck, then vite build).
#   3. Builds the image rw-ui-walkthrough:local (WALKTHROUGH_IMAGE overrides the tag) from Dockerfile.ui. Its base
#      images and Python packages are pinned: a first build downloads them, later builds reuse Docker's cache.
#   4. Runs it with --network none, then copies the screenshots, logs and ui_results.json to <output-dir>, which must
#      be outside the repository.
# The exit status is the walkthrough's: 0 when every step passes.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <output-dir>" >&2
    exit 2
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE="${WALKTHROUGH_IMAGE:-rw-ui-walkthrough:local}"

for tool in docker npm tar; do
    if ! command -v "$tool" > /dev/null; then
        echo "error: $tool is required and is not on PATH" >&2
        exit 1
    fi
done
if [[ ! -d "$REPO/frontend/node_modules" ]]; then
    echo "error: $REPO/frontend/node_modules is missing; run 'npm ci' in frontend/ first (it needs the network once)" >&2
    exit 1
fi
mkdir -p "$1"
OUT="$(cd "$1" && pwd)"
case "$OUT/" in
    "$REPO/"*)
        echo "error: the output folder must be outside the repository, not $OUT" >&2
        exit 1
        ;;
esac

WORK="$(mktemp -d "${TMPDIR:-/tmp}/rw-ui-walkthrough.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
CONTEXT="$WORK/context"
mkdir -p "$CONTEXT/repo" "$CONTEXT/walkthrough" "$WORK/frontend" "$WORK/out"

echo "[walkthrough] copying the checkout to $CONTEXT/repo"
tar -C "$REPO" -cf - \
    --exclude=.git --exclude=.venv --exclude=node_modules --exclude=frontend/dist --exclude=local_data \
    --exclude=__pycache__ --exclude='*.pyc' --exclude=.pytest_cache --exclude='*.sqlite' --exclude='*.sqlite-wal' \
    --exclude='*.sqlite-shm' --exclude='*.db' --exclude=.env --exclude=.DS_Store \
    . | tar -C "$CONTEXT/repo" -xf -
cp "$HERE/run.sh" "$HERE/walkthrough.py" "$HERE/census.js" "$HERE/requirements-walkthrough.txt" "$CONTEXT/walkthrough/"
cp "$HERE/Dockerfile.ui" "$CONTEXT/Dockerfile.ui"

echo "[walkthrough] building the frontend in $WORK/frontend"
cp -R "$CONTEXT/repo/frontend/." "$WORK/frontend/"
cp -R "$REPO/frontend/node_modules" "$WORK/frontend/node_modules"
(cd "$WORK/frontend" && npm --no-update-notifier run build)
cp -R "$WORK/frontend/dist" "$CONTEXT/repo/frontend/dist"

echo "[walkthrough] building the image $IMAGE"
docker build --file "$CONTEXT/Dockerfile.ui" --tag "$IMAGE" "$CONTEXT"

echo "[walkthrough] running $IMAGE with --network none"
chmod 0777 "$WORK/out"  # the image runs as its own user (pwuser), which writes the results here
set +e
docker run --rm --init --network none --tmpfs /tmp -v "$WORK/out":/out "$IMAGE"
status=$?
set -e
cp -R "$WORK/out/." "$OUT/"
echo "[walkthrough] exit status $status; results in $OUT"
exit "$status"
