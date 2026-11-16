#!/usr/bin/env bash
# Tests a built recorded demo in Docker with no network (scripts/demo/README.md):
#
#   scripts/demo/test-demo.sh <site-dir> <results-dir>
#
# static_server.py serves <site-dir> on the container's own 127.0.0.1 with the headers of its _headers file, and
# test_demo.py drives headless Chromium through the three flows at 1440 and 390 px. The results (demo_results.json)
# and screenshots go to <results-dir>, which must be outside the repository. It uses the image build-demo.sh built
# (DEMO_IMAGE overrides the tag, rw-ui-walkthrough:local by default) and builds nothing.
# The exit status is the test's: 0 when every check passes.
set -euo pipefail

if [[ $# -ne 2 ]]; then
    echo "usage: $0 <site-dir> <results-dir>" >&2
    exit 2
fi
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE="${DEMO_IMAGE:-rw-ui-walkthrough:local}"

if ! docker image inspect "$IMAGE" > /dev/null 2>&1; then
    echo "error: the image $IMAGE is missing; scripts/demo/build-demo.sh builds it" >&2
    exit 1
fi
SITE="$(cd "$1" && pwd)"
if [[ ! -f "$SITE/index.html" || ! -f "$SITE/demo-data/manifest.json" ]]; then
    echo "error: $SITE is not a built recorded demo (no index.html or demo-data/manifest.json)" >&2
    exit 1
fi
mkdir -p "$2"
RESULTS="$(cd "$2" && pwd)"
case "$RESULTS/" in
    "$REPO/"*)
        echo "error: the results folder must be outside the repository, not $RESULTS" >&2
        exit 1
        ;;
esac
chmod 0777 "$RESULTS"  # the image runs as its own user (pwuser), which writes the results here

echo "[demo-test] testing $SITE in $IMAGE with --network none"
docker run --rm --init --network none --tmpfs /tmp -v "$SITE":/site:ro -v "$HERE":/opt/demo:ro -v "$RESULTS":/out \
    --entrypoint /opt/walkthrough/venv/bin/python "$IMAGE" /opt/demo/test_demo.py --site /site --out /out
