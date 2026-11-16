#!/usr/bin/env bash
# The recorded demo: the one-page UI as a folder of static files that replays answers the real API gave in rules mode,
# for any static host (scripts/demo/README.md). It deploys, uploads and publishes nothing.
#
#   scripts/demo/build-demo.sh <output-dir>
#
# Needs docker, npm, node and frontend/node_modules: run `npm ci` in frontend/ once first (that step needs the network).
#   1. Copies the checkout to a temporary folder, without what .dockerignore leaves out, and builds the production UI
#      there, as scripts/ui-walkthrough/run-walkthrough.sh does. Nothing is written inside the repository.
#   2. Builds the UI walkthrough image from scripts/ui-walkthrough/Dockerfile.ui with that script's build context, so
#      Docker reuses its cached layers. DEMO_IMAGE overrides the tag (default rw-ui-walkthrough:local, the
#      walkthrough's own).
#   3. Records: runs the image with --network none and scripts/demo mounted read-only. record.sh starts the API in rules
#      mode; record.py drives the production UI through the README's three flows, checks the values the docs give,
#      writes the fixtures and receipts, and fails if the operator token or the session cookie is in anything it wrote.
#   4. Builds the demo UI (frontend/demo: type-check, then vite build) and adds the recorded data.
#   5. Checks the site (check-site.mjs): the API's security headers, no inline script or style, no other origin, sizes,
#      and no local path, user or host name.
#   6. Copies the site to <output-dir>, which must be outside the repository and empty or new.
# Then test it: scripts/demo/test-demo.sh <output-dir> <results-dir>.
set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <output-dir>" >&2
    exit 2
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE="${DEMO_IMAGE:-rw-ui-walkthrough:local}"

for tool in docker npm node tar; do
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
if [[ -n "$(ls -A "$OUT")" ]]; then
    echo "error: $OUT is not empty; give an empty or new folder, so no file of an earlier build is left in the site" >&2
    exit 1
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/rw-demo.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
CONTEXT="$WORK/context"
mkdir -p "$CONTEXT/repo" "$CONTEXT/walkthrough" "$WORK/frontend" "$WORK/recording" "$WORK/site"

echo "[demo] copying the checkout to $CONTEXT/repo"
tar -C "$REPO" -cf - \
    --exclude=.git --exclude=.venv --exclude=node_modules --exclude=frontend/dist --exclude=local_data \
    --exclude=__pycache__ --exclude='*.pyc' --exclude=.pytest_cache --exclude='*.sqlite' --exclude='*.sqlite-wal' \
    --exclude='*.sqlite-shm' --exclude='*.db' --exclude=.env --exclude=.DS_Store \
    . | tar -C "$CONTEXT/repo" -xf -
WALKTHROUGH="$REPO/scripts/ui-walkthrough"
cp "$WALKTHROUGH/run.sh" "$WALKTHROUGH/walkthrough.py" "$WALKTHROUGH/census.js" "$WALKTHROUGH/requirements-walkthrough.txt" \
    "$CONTEXT/walkthrough/"
cp "$WALKTHROUGH/Dockerfile.ui" "$CONTEXT/Dockerfile.ui"

echo "[demo] building the production UI, which the recording drives"
cp -R "$CONTEXT/repo/frontend/." "$WORK/frontend/"
cp -R "$REPO/frontend/node_modules" "$WORK/frontend/node_modules"
(cd "$WORK/frontend" && npm --no-update-notifier run build)
cp -R "$WORK/frontend/dist" "$CONTEXT/repo/frontend/dist"

echo "[demo] building the image $IMAGE from scripts/ui-walkthrough/Dockerfile.ui"
docker build --file "$CONTEXT/Dockerfile.ui" --tag "$IMAGE" "$CONTEXT"

echo "[demo] recording the three flows in $IMAGE with --network none"
chmod 0777 "$WORK/recording"  # the image runs as its own user (pwuser), which writes the recording here
docker run --rm --init --network none --tmpfs /tmp -v "$CONTEXT/repo/scripts/demo":/opt/demo:ro \
    -v "$WORK/recording":/out --entrypoint /opt/demo/record.sh "$IMAGE"

echo "[demo] building the demo UI"
(cd "$WORK/frontend" && ./node_modules/.bin/tsc -p demo/tsconfig.json --noEmit \
    && DEMO_OUT_DIR="$WORK/site" ./node_modules/.bin/vite build --config demo/vite.config.ts)
cp -R "$WORK/recording/site/demo-data" "$WORK/recording/site/receipts" "$WORK/site/"

echo "[demo] checking the site"
FORBID=(--forbid "$REPO" --forbid "$WORK" --forbid "$OUT" --forbid "$(hostname)")
for name in "${HOME:-}" "${USER:-}"; do
    if [[ -n "$name" ]]; then
        FORBID+=(--forbid "$name")
    fi
done
node "$HERE/check-site.mjs" --site "$WORK/site" --summary "$WORK/recording/recording-summary.json" "${FORBID[@]}"

cp -R "$WORK/site/." "$OUT/"
if [[ -n "${DEMO_RECORDING_SUMMARY:-}" ]]; then
    cp "$WORK/recording/recording-summary.json" "$DEMO_RECORDING_SUMMARY"
fi
echo "[demo] the static site is in $OUT ($(du -sh "$OUT" | cut -f1)); nothing was deployed"
echo "[demo] next: scripts/demo/test-demo.sh $OUT <results-dir>"
