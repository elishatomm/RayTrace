#!/usr/bin/env bash
# The recorded demo's recording step. scripts/demo/build-demo.sh runs it inside the UI walkthrough image
# (scripts/ui-walkthrough/Dockerfile.ui) with --network none and this folder mounted read-only at /opt/demo:
#   1. starts the real API in rules mode on 127.0.0.1:8000 over an empty data directory in /tmp;
#   2. runs record.py, which drives the production UI through the README's three flows in headless Chromium and
#      writes the fixtures and receipts to /out/site;
#   3. stops the API. Its log holds the operator token, so it stays in the container's /tmp and is never copied out.
set -euo pipefail

OUT=/out
DATA=/tmp/rw-demo
APP=/opt/recovery-workbench
export WORKBENCH_MODEL_MODE=rules

mkdir -p "$DATA"
cd "$APP"

"$APP/.venv/bin/python" -m recovery_workbench.api --data-dir "$DATA/api" --port 8000 > "$DATA/api.log" 2>&1 &
SERVER_PID=$!
stop_server() {
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
}
trap stop_server EXIT

for _ in $(seq 1 240); do
    grep -q 'Uvicorn running on' "$DATA/api.log" 2>/dev/null && break
    sleep 0.5
done
if ! grep -q 'Uvicorn running on' "$DATA/api.log"; then
    echo "[record] the API did not start; its log without the token line:" >&2
    sed -E 's/(\(shown once\): ).*/\1<redacted>/' "$DATA/api.log" >&2
    exit 1
fi
echo "[record] API on 127.0.0.1:8000 is up in rules mode"

/opt/walkthrough/venv/bin/python /opt/demo/record.py --server-log "$DATA/api.log" --out "$OUT"
