#!/usr/bin/env bash
# Agent U, phase 11: everything happens inside this container, which has no network.
# Reused by Agent V (phase 11 follow-up) with one change: the files it writes to /out are named v_* instead of u_*.
# Reused by Agent X (phase 11 follow-up X1) the same way: the files it writes to /out are named x_*.
# Reused by Agent Q33 (owner decision Q33) the same way: the files it writes to /out are named q33_*.
# Kept in the repository since owner decision Q36, under scripts/ui-walkthrough/: the files it writes to /out are named ui_*.
#   1. seed worker_backlog seed 101 with the CLI demo, rules mode, no model (diagnosis, plan, approval, execution);
#   2. server A on 127.0.0.1:8000 over that data; server B on 127.0.0.1:8001 over an empty data directory,
#      where the UI itself recreates the same world for the Flow A regression pass;
#   3. drive headless Chromium against both, writing screenshots and results to /out.
# The operator tokens never leave the container: the logs copied to /out have them redacted.
set -euo pipefail

OUT=/out
DATA=/tmp/rw
APP=/opt/recovery-workbench
APP_PYTHON="$APP/.venv/bin/python"
WALK_PYTHON=/opt/walkthrough/venv/bin/python
export WORKBENCH_MODEL_MODE=rules

mkdir -p "$DATA"
cd "$APP"

redact() { sed -E 's/(\(shown once\): ).*/\1<redacted>/' "$1"; }

stop_servers() {
    for pid in "${SERVER_PIDS[@]:-}"; do
        kill "$pid" 2>/dev/null || true
    done
    wait 2>/dev/null || true
    for port in 8000 8001; do
        if [[ -f "$DATA/api_$port.log" ]]; then
            redact "$DATA/api_$port.log" > "$OUT/ui_api_$port.log"
        fi
    done
}
SERVER_PIDS=()
trap stop_servers EXIT

echo "[run] $(date -u +%FT%TZ) seeding: demo --scenario worker_backlog --seed 101 --model-mode rules"
"$APP_PYTHON" -m recovery_workbench.cli demo --scenario worker_backlog --seed 101 --model-mode rules \
    --data-dir "$DATA/demo" > "$OUT/ui_cli_demo.log" 2>&1
echo "[run] demo exited 0"

"$APP_PYTHON" -m recovery_workbench.api --data-dir "$DATA/demo" --port 8000 > "$DATA/api_8000.log" 2>&1 &
SERVER_PIDS+=($!)
"$APP_PYTHON" -m recovery_workbench.api --data-dir "$DATA/ui" --port 8001 > "$DATA/api_8001.log" 2>&1 &
SERVER_PIDS+=($!)

for port in 8000 8001; do
    log="$DATA/api_$port.log"
    for _ in $(seq 1 240); do
        grep -q 'Uvicorn running on' "$log" 2>/dev/null && break
        sleep 0.5
    done
    if ! grep -q 'Uvicorn running on' "$log"; then
        echo "[run] the API on port $port did not start; its log:" >&2
        redact "$log" >&2
        exit 1
    fi
    echo "[run] API on 127.0.0.1:$port is up"
done

set +e
"$WALK_PYTHON" /opt/walkthrough/walkthrough.py \
    --server-a-log "$DATA/api_8000.log" --server-b-log "$DATA/api_8001.log" \
    --cli-log "$OUT/ui_cli_demo.log" --out "$OUT"
status=$?
set -e
echo "[run] walkthrough exited $status"
exit "$status"
