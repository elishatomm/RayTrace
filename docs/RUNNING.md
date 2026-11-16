# Running RayTrace

The README's [How to run](../README.md#how-to-run) gives each step's commands. This file gives their full output, under the same numbers, and everything beyond a first run: the evaluation commands, sending a webhook over HTTP, the production image (public mode), the settings, and the known issues of running it. These sections were written for the README and moved here with their links and section numbers updated; the public-mode settings also record the security fixes made since. In them, "this README" means the README the commands were run for.

Unless a command is marked otherwise, it was run for this README. Each ran in a copy of the repository with no `.venv`, `node_modules` or build output, against fresh temporary data folders. The output shown is pasted from those runs; `$RW` stands for the temporary folder and `…` marks omitted lines. Every command exits 0 unless its step says otherwise; a refused request, such as recorded mode with no matching recording, exits 2.

Requirements: Python 3.12 managed by uv, and Node 20.19+ or 22.12+ for the UI build. Ollama is needed only for the live local model.

## 1. Install

```console
$ uv sync --frozen
Using CPython 3.12.13
Creating virtual environment at: .venv
…
Installed 23 packages in 24ms
```

## 2. Seed one synthetic run and inspect it

```console
$ export RW=$(mktemp -d)
$ uv run python -m recovery_workbench.cli seed --scenario worker_backlog --seed 101 --data-dir "$RW/seed"
{
  "counts": {
    …
    "orders": 168,
    …
    "webhook_inbox": 343,
    "worker_attempts": 824,
    "worker_failed": 729,
    …
  },
  "cutoff_ms": 1793881800000,
  …
  "incident_ids": [
    "inc_0001"
  ],
  …
  "run_id": "run_53966a08d4",
  "scenario": "worker_backlog",
  "seed": 101,
  "split": "demo",
  …
}
$ uv run python -m recovery_workbench.cli status --run-id run_53966a08d4 --data-dir "$RW/seed"
{
  "backlog": {
    "failed": 234,
    "manual_review": 0,
    "pending": 14,
    "processed": 95
  },
  …
    "completion": {
      "denominator": 143,
      "numerator": 45,
      "value": 0.3147
    },
  …
```

Run IDs are neutral hashes. Use the `run_id` that `seed` prints. The same scenario and seed always give the same run.

## 3. Run the three flows end to end with the rules baseline (no model needed)

```console
$ uv run python -m recovery_workbench.cli demo --scenario worker_backlog --seed 101 --model-mode rules --data-dir "$RW/flow-a"
$ uv run python -m recovery_workbench.cli demo --scenario method_degradation --seed 101 --model-mode rules --data-dir "$RW/flow-b"
$ uv run python -m recovery_workbench.cli demo --scenario late_authorization --seed 101 --model-mode rules --data-dir "$RW/flow-c"
```

All three exit 0. Each prints numbered steps and writes a JSON report to `<data-dir>/reports/<run_id>.json`. Excerpts are in [The three flows](../README.md#the-three-flows).

Rules mode runs `rules-v3`: rules-v2's hypothesis and operation, with the faulting deployment taken from the deterministic attribution `attribution-v1` ([Phase 11b](RESULTS.md#phase-11b-does-the-local-model-add-attribution-value-pre-registered-held-out-variants)). On these three worlds its answers are exactly rules-v2's. Compared with rules-v2, the only printed line that changes is the diagnosis's mode, now `rules baseline rules-v3 (no model)`; record IDs are random per database either way.

The same steps exist as separate commands:

- `diagnose`, `fixes`, `validate-fix`, `plan`, `approve`, `execute`, `escalate`, `refresh`, `report`, `receipt`.
- **Every execution, applied or rejected, has a receipt.** `uv run python -m recovery_workbench.cli receipt --run-id RUN --action-id ACT --output-dir DIR` writes it as JSON and Markdown. Regenerated from the database it gives the same bytes, and the same recovery of the same world gives the same receipt hash, whatever other runs its database holds. Since receipt-2 the hash replaces the database's inbox row numbers with their ranks and leaves out the plan hash and the two evidence versions built from them; the receipt still shows all of them. CLI commands apply pending schema migrations when they open a data directory, as the API does at start.
- See `uv run python -m recovery_workbench.cli --help`.
- Exit codes: 0 success, 2 refused or invalid input, 3 stale or rejected plan.

## 4. Tests

The suite runs in a throwaway Linux container, so every database a test creates lives and dies inside it:

```console
$ scripts/test-in-docker.sh
…
2314 passed, 2 deselected, 1 warning in 455.41s (0:07:35)
```

- **What the container is.** `Dockerfile.test` uses Python 3.12 and uv, pinned by digest, with dependencies from `uv.lock`. The image holds its own copy of the checkout and runs pytest as a non-root user with no network, a read-only filesystem and an in-memory `/tmp`; nothing on the host is mounted or written.
- **Passing pytest arguments.** Extra arguments go to pytest, for example `scripts/test-in-docker.sh tests/test_plans.py -x`.
- **Running without Docker.** `uv run pytest -q -m "not ollama"` runs the same suite on the host with the same counts.
- The 2 deselected tests call a live local model; run them on the host with `-m ollama` when Ollama is up.
- The warning is Starlette's notice that its test client will move from `httpx` to `httpx2`.

## 5. The one-page UI

```console
$ npm --prefix frontend ci
…
found 0 vulnerabilities
$ npm --prefix frontend run build
…
dist/index.html                   0.46 kB │ gzip:  0.30 kB
dist/assets/index-Dss8CM_k.css   22.58 kB │ gzip:  5.51 kB
dist/assets/index-B0-AIFGN.js   289.81 kB │ gzip: 89.39 kB
✓ built in 371ms
$ WORKBENCH_MODEL_MODE=rules uv run python -m recovery_workbench.api --data-dir "$RW/api" --port 8000
RayTrace (synthetic sandbox) on http://127.0.0.1:8000/ ; execution mode rules
Model source: rules baseline (no model); set by WORKBENCH_MODEL_MODE=rules
Operator token for this server session (shown once): <printed here>
INFO:     Started server process […]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

Open http://127.0.0.1:8000/, paste the operator token the server printed, and choose an operator ID such as `op_demo_operator`.

- The server listens on 127.0.0.1 only, unless public mode is on (see [Settings](#settings)).
- `npm run build` type-checks (`tsc --noEmit`) before bundling.
- Use `WORKBENCH_MODEL_MODE=local` for the live model, `hosted` for a hosted one, or `auto` for the first that can serve (see [Settings](#settings)).
- Recorded replays are labelled `RECORDED` in the page, and hosted answers `HOSTED`, outside the M4 evaluation. The top bar names the model source the server chose at start. When a diagnosis needs review because the model was unavailable or failed, the page offers **Diagnose with the rules instead**.
- The run view opens with a run header. Its money strip, **Money at stake**, shows the queue's whole-queue totals (captured with processing incomplete; still awaiting an action or a hold), then the restored processing value and new capture, which read "Not yet known" until a plan for the latest diagnosis is executed on this page. Its recovery rail, **Recovery steps**, shows six steps (Diagnose, Validate fix, Plan, Approve, Execute, Receipt), each with its state and proof for the latest diagnosis in this session; steps its path does not use read "Not applicable".
- Below the header, the order recovery queue shows one case per order with its state, age, evidence freshness, next step and blockers, with filters (captured and processing incomplete, payment state unresolved, manual review, complete, or any one state) and sorting by age or amount. Click an order for its timeline (provider → delivery/inbox → worker → order, by replay time), its blockers, its next step and, after a recovery, its receipt; at 1280 px and wider the case opens beside the queue. The top bar's banner reads "Synthetic demonstration".
- For this README the page and `/health` were fetched from the running server; the full click-through was done earlier in development.
- **A recorded demo for static hosting.** `scripts/demo/build-demo.sh <output-dir>` records [the three flows](../README.md#the-three-flows) against the real API in rules mode, in Docker with no network, and writes this page as static files that replay those answers; `scripts/demo/test-demo.sh <site-dir> <results-dir>` tests it. A banner on every view says it is a recorded demo: nothing runs, no provider is called and no money moves. It is hosted at [raytrace-demo.elishatom04.workers.dev](https://raytrace-demo.elishatom04.workers.dev); [DEPLOY.md](DEPLOY.md) explains the hosting, and [scripts/demo/README.md](../scripts/demo/README.md) says what it covers.

## 6. Live local model, and recorded replay (optional)

The live mode needs [Ollama](https://ollama.com) on the same machine with `qwen3:8b`, listening on loopback only. **They were not run while writing this README**, because the model was busy with the frozen evaluation:

```bash
ollama pull qwen3:8b
ollama list
# keep the model listener local; skip if the desktop app already serves this port (configure it instead)
OLLAMA_NO_CLOUD=1 OLLAMA_HOST=127.0.0.1:11434 ollama serve
```

Then:

```bash
uv run python -m recovery_workbench.cli demo --scenario worker_backlog --seed 101 --model-mode local \
    --data-dir "$RW/live" --recordings-dir "$RW/recordings"
uv run python -m recovery_workbench.cli demo --scenario worker_backlog --seed 101 --model-mode recorded \
    --data-dir "$RW/replay" --recordings-dir "$RW/recordings"
```

**Verification status:**

- **Live path:** re-run on 2026-11-12 under policy-v3 (late-authorised captures are planned), using exactly the two commands above in fresh folders. Both exited 0:

```console
    mode: live local model qwen3:8b
    hypothesis: merchant_worker_regression; faulting change: deploy_9b85880366; affected method: none
    replayed: True; same action act_2e4c128974ac63f5a231; fulfilments still 148 (were 148); no new effects
    restored processing value: INR 125,027.00 (money already captured before the intervention, not new revenue)
    new capture: INR 0.00
---- recorded run ----
    mode: RECORDED replay of qwen3:8b output from recording:8b47f5c9….json (not live inference)
    hypothesis: merchant_worker_regression; faulting change: deploy_9b85880366; affected method: none
```

  The live demo took 16 s wall time end to end, including seeding the world. The model named the faulting deployment, not the more recent innocent one. The recording ID is the same as in the v1.0 run, because the evidence packet did not change; only the recovery plan did: 103 orders after the refresh instead of 99, the difference being the 4 late-authorised orders.
- **Live path again on 2026-11-13, under prompt `diag-p5` and validator `val-6`, the current pair then** (the recording file names both): the same two commands, from a clean checkout, in fresh folders. Both exited 0, with the same lines as above except the action ID, which is random per database. The answer was validated, named `deploy_9b85880366`, and gave the same 148 fulfilments, INR 125,027.00 restored and INR 0.00 new capture, in 15 s.
- **The two guard paths below were re-run for this README.** The first shows that an unavailable model gives a visible `needs_review` and a conservative escalation, never an invented answer:

```console
$ WORKBENCH_OLLAMA_URL=http://127.0.0.1:9 uv run python -m recovery_workbench.cli demo --scenario worker_backlog \
    --seed 101 --model-mode local --data-dir "$RW/local" --recordings-dir "$RW/recordings"
…
[3] diagnose
    mode: live local model qwen3:8b
    status: needs_review; packet 8b47f5c942e67560
    needs review because: the model is unavailable: Ollama GET /api/tags failed: ConnectError('[Errno 61] Connection refused')
…
[5] escalate
    …
    no reprocessing: failed inbox rows 234, fulfilments unchanged
```

The second shows that recorded mode replays only an identical model input (same packet hash and prompt version), and otherwise refuses with exit code 2:

```console
$ uv run python -m recovery_workbench.cli demo --scenario worker_backlog --seed 101 --model-mode recorded \
    --data-dir "$RW/replay" --recordings-dir "$RW/recordings"
{"error": "ContractError", "detail": "no recorded local output exists for packet 8b47f5c942e67560; recorded mode replays only an identical packet, so run local mode on this packet first"}
```

## 7. Evaluation commands

The evaluation code is in `src/recovery_workbench/evaluation/`. Generation and a rules-only run were executed for this README on the development split, into temporary folders:

```console
$ uv run python -m recovery_workbench.evaluation.generate --split dev --data-dir "$RW/eval-dev"
…
  "runs_without_incident": [],
  "split": "dev"
}
$ uv run python -m recovery_workbench.evaluation.run --split dev --data-dir "$RW/eval-dev" --systems rules --output-dir "$RW/eval-out"
…
$ ls "$RW/eval-out/runs/dev_rules" "$RW/eval-out/traces"
$RW/eval-out/runs/dev_rules:
data_manifest.json
environment.json
recovery_ledger.json
results.json

$RW/eval-out/traces:
dev_rules.jsonl
```

The model run uses the same runner:

```bash
uv run python -m recovery_workbench.evaluation.run --split dev --data-dir "$RW/eval-dev" \
    --systems rules,model --model qwen3:8b --think off --output-dir "$RW/eval-out"
uv run python -m recovery_workbench.evaluation.freeze --output-dir evaluation        # before generating TEST
uv run python -m recovery_workbench.evaluation.generate --split test --data-dir "$RW/eval-test"
uv run python -m recovery_workbench.evaluation.run --split test --data-dir "$RW/eval-test" \
    --systems rules,model --model qwen3:8b --think off --output-dir evaluation --label test_frozen
uv run python -m recovery_workbench.evaluation.summarize --output-dir evaluation --frozen test_frozen --rerun test_rerun
```

- These are the commands that produced the published results in [`evaluation/`](../evaluation/) during M4.
- The order was enforced and is visible in the file timestamps: development runs, then `freeze`, then the test split generated, then one frozen run.
- With thinking off, one diagnosis took a warm median of 10.6 s on the development split and 11.2 s on the test split, on the laptop described under [Model](RESULTS.md#model).

## 8. Sending a webhook over HTTP

With the API running (section 5), a run's receiver also takes signed deliveries over HTTP: `POST /api/runs/{run_id}/webhooks/{integration_id}`.

- **Path.** SPEC §11 names `/api/webhooks/{integration_id}`. The run is in the path because every run uses the same integration ID (`int_SYNTHETIC_MAIN`).
- **Authentication.** The HMAC over the raw body authenticates the sender. The route needs no session and no UI header; the Host, Origin and Sec-Fetch-Site checks still apply. The fixture secrets are published in `simulator/signing.py`, so any process on this machine can sign as the provider. The route trusts the local machine: it listens on 127.0.0.1 only, and browsers on other sites are refused.
- **Answers.** The body is `{"outcome", "event_id"}` with the receiver's status: 200 for accepted, duplicate (the same bytes again, under this event ID or another, including bytes held as a conflicting delivery) and conflicting_id; otherwise 400, 401, 404 (unknown integration) or 413. If the merchant receiver deployed at receipt raises on a body before it is verified, the answer is 500 `ReceiverFailedError`, without the receiver's own message. A body over the 1 MiB ceiling is read only to one byte past it; the answer is 413, and the server then closes the connection. A body nesting arrays and objects more than 32 deep is refused before any parser reads it (400 `rejected_schema`). Valid events nest 4 deep. A run that does not exist on this server answers 404 `{"error", "detail"}`, and nothing is recorded. When the route cannot take a delivery now, it answers 429 (more than 600 deliveries a minute) or 503 (any storage failure), with `Retry-After`.
- **What it records.** Every request that reaches an existing run leaves a `delivery_attempts` row, rejected or not, whoever sent it. The one exception is the 500 above: a body the deployed receiver fails on before verification is recorded nowhere, though the replay clock has already stepped. This is by design: rejected deliveries are D3's evidence, and a receiver cannot tell a mis-signed provider delivery from noise. Delivery health is computed per integration, so rejections for an unknown integration never count toward a run's evidence.
- **What it does not do.** The route runs no worker, so an accepted event waits as `pending`.

The `webhook-request` helper signs an event for an existing payment as the simulated provider signs, with the provider's secret, and prints a ready-to-run `curl` command. It only reads the database and sends nothing itself. If the run's deployed receiver verifies with another secret (a D3 configuration change), the helper's request answers 401, just as the provider's own deliveries do. Values in `<…>` come from your run:

```console
$ uv run python -m recovery_workbench.cli webhook-request --data-dir "$RW/api" --run-id <run_id> \
    --payment-id <payment_id> --status captured --event-id evt_demo_0001 --output "$RW/evt_demo_0001.json"
# captured event for payment <payment_id> of order <order_id>: <amount> paise INR; body <path> (<n> bytes)
X-Webhook-Event-Id: evt_demo_0001
X-Webhook-Signature: <64 hex digits>
curl --silent --show-error --data-binary @<path> --header 'Content-Type: application/json' --header 'X-Webhook-Event-Id: evt_demo_0001' --header 'X-Webhook-Signature: <…>' --write-out '\nHTTP %{http_code}\n' http://127.0.0.1:8000/api/runs/<run_id>/webhooks/int_SYNTHETIC_MAIN
```

- **Accepted:** run the printed command, and you get `{"outcome":"accepted","event_id":"evt_demo_0001"}` and `HTTP 200`.
- **Duplicate:** run it again, and you get `duplicate` and `HTTP 200`; the inbox still holds one row for the event.
- **Signature:** change one byte of the body file and send it, and you get `{"outcome":"rejected_signature"}` and `HTTP 401`.
- **Conflict:** run `webhook-request` again with the same `--event-id`, `--status failed` and a new `--output`, then send that file. You get `conflicting_id` and `HTTP 200`; the delivery is held in `conflicting_deliveries` and never processed.
- **Not sure which payment to use?** Pass any ID; the error names three of the run's payments.

These outcomes are what `tests/test_api_webhooks.py` asserts over HTTP. The transcript above is a template, not a recorded session.

## 9. The production image (public mode)

```console
$ docker build -t <tag> .
$ docker run --rm -e WORKBENCH_PUBLIC_ORIGIN=https://<name>.<host> -e WORKBENCH_DEMO_TOKEN <tag>
```

- `Dockerfile` builds the UI in a Node stage, installs the locked Python packages and runs the API as UID 1000, which can write only to `/data`. Building needs the network once; the image holds no secret.
- The container starts only in public mode (see [Settings](#settings)). It listens on `0.0.0.0`, which nothing else allows, and public mode refuses to start without its origin and a strong demo token. Without the origin it exits with status 2; a missing or weak token stops it with an error that names the problem (exit status 1).
- It answers only requests addressed to its public origin. On a host, the platform routes its HTTPS address to the container's port.
- Its model mode is `auto`. No local model runs in the image, so it uses the hosted model when the three hosted settings are set (see [Settings](#settings)), and the rules baseline otherwise.
- It holds the demo runs. Its build seeds worker_backlog, method_degradation and late_authorization with each seed in the build argument `WORKBENCH_PUBLIC_SEEDS` (default `101`, which also becomes the image's published list) into `/opt/recovery-workbench/demo-runs`, which root owns and the server can only read. At every start public mode copies them into its emptied `/data`, so a viewer opens each at once (HTTP 200) and no restart seeds them. Under `docker run --cpus 0.1 --memory 512m`, opening one took at most 0.1 s instead of 8.4–49.7 s of seeding, and the copy added 0.1–0.2 s to the start. The image grows by 10.5 MiB unpacked and 1.4 MB compressed.
- Free hosts run linux/amd64 images; `docker buildx build --platform linux/amd64 -t <tag> .` builds one (not run for this README).
- [DEPLOY.md](DEPLOY.md) explains running it on a host. The live app is not deployed.
- For this README the image was built and started through its entry point with no network: it printed its start lines, one naming the demo runs it copied, and never the token.

## Settings

The application reads `WORKBENCH_*` environment variables; it does not load `.env` files itself. [.env.example](../.env.example) lists every variable with its default. The main ones:

- `WORKBENCH_MODEL_MODE` (`local`, `recorded`, `rules`, `hosted` or `auto`);
- `WORKBENCH_MODEL` (`qwen3:8b`);
- `WORKBENCH_MODEL_THINK`;
- `WORKBENCH_OLLAMA_URL` (must be a loopback address);
- `WORKBENCH_DATA_DIR` (default `./local_data`, used when `--data-dir` is omitted);
- `WORKBENCH_STALE_AFTER_SECONDS` (3600).

**The hosted model** (Cloudflare Workers AI was the chosen provider) is any OpenAI-compatible chat-completions API:

- `WORKBENCH_HOSTED_URL`: the API's base URL, such as `https://api.cloudflare.com/client/v4/accounts/<account-id>/ai/v1`. It must be https, or plain http to a loopback host only.
- `WORKBENCH_HOSTED_MODEL`: the model ID, such as `@cf/openai/gpt-oss-20b`.
- `WORKBENCH_HOSTED_API_KEY`: the key, from the host's secret store. It goes only in the Authorization header to that URL's host, and is never printed, logged, stored or sent to the browser.
- `WORKBENCH_HOSTED_MAX_CALLS_PER_DAY` (50): every call counts, a repair included. Past the budget, a diagnosis needs review and nothing is sent.
- `WORKBENCH_HOSTED_RESPONSE_FORMAT` (`json_schema`, or `json_schema_strict`, `json_object` or `none`), `WORKBENCH_HOSTED_TEMPERATURE` (0, or `none` to send none) and `WORKBENCH_HOSTED_REASONING_EFFORT` (unset, or `none`, `low`, `medium` or `high`): providers differ on all three.

`auto` takes the first source that can serve, once, at start: the local model if Ollama answers on loopback within 2 s; the hosted model if its URL, model and key are all set; otherwise the rules baseline. It prints the source it chose and why, and never switches afterwards. With only some hosted settings set, it takes the rules, and its start line names what is missing.

A hosted answer is stored and labelled `hosted`, with the provider's host and the token counts but no digest. Recorded mode never replays it. A failed call leaves the diagnosis needing review; the provider's text is cut short, escaped and stripped of the key. Its accuracy is unmeasured: the M4 evaluation measured qwen3:8b on the development laptop.

`WORKBENCH_ENV` must stay `synthetic`, and `WORKBENCH_ALLOW_EXTERNAL_ACTIONS` must stay `false`; any other value is refused at start.

**Public mode** is off unless `WORKBENCH_PUBLIC_ORIGIN` is set, for example to `https://<name>.<host>` ([DEPLOY.md](DEPLOY.md)). Then the server:

- answers only that Host and that Origin, listens on 127.0.0.1 unless started with `--host 0.0.0.0` (which only public mode allows), and takes its port from `$PORT` when the host sets one;
- takes the operator token from `WORKBENCH_DEMO_TOKEN` (43 to 256 characters in the shape `secrets.token_urlsafe(32)` writes, with at least 3 uppercase and 3 lowercase letters, a digit and 20 different characters, and no 8-character sequence that occurs twice, from the host's secret store; generate it, never choose it) and never prints or logs it;
- keeps the session in a `__Host-` cookie that is `Secure`, `HttpOnly` and `SameSite=Strict`, ends sessions after 30 idle minutes or 8 hours, and allows 20 at once: a login ends the session its browser presented, and with all 20 live it ends the one unused longest instead of refusing;
- seeds only the seeds in `WORKBENCH_PUBLIC_SEEDS` (default `101`): at most `WORKBENCH_PUBLIC_RUNS_PER_SESSION` (3) new runs a session and `WORKBENCH_PUBLIC_RUNS_PER_PROCESS` (30) a process, and none once the data directory holds `WORKBENCH_PUBLIC_DATA_LIMIT_MB` (200). Seeding a scenario and seed that already have a run opens that run, because viewers share runs;
- takes 10 seeding, diagnosis, fix-validation, planning and execution requests a minute per session, and closes connections whose request arrives too slowly;
- installs no webhook route, sends `Strict-Transport-Security: max-age=31536000` with every answer, answers `/health` with `{"status": "ok"}` only, and logs one line per login result, run creation, quota refusal, session ended at the cap and 429;
- empties its data directory at every start, and refuses a non-empty one that public mode did not mark. Give it a directory used for nothing else. When `WORKBENCH_PUBLIC_TEMPLATE_DIR` names the demo runs an image was built with (the production image sets it), it then copies them in. It refuses to start if that template holds anything but regular files and directories, holds a run of a seed `WORKBENCH_PUBLIC_SEEDS` leaves out, or cannot be copied. Copied runs take no run quota, count toward the data limit and record the server's model mode; a published seed the template lacks is seeded when a viewer asks. With public mode off, the server and every CLI command refuse a directory public mode marked, because its next start would empty it.

`WORKBENCH_PUBLIC_TRUSTED_PROXIES` names the host's proxies whose `X-Forwarded-For` the server believes, never `*`, and turns on a limit of 300 requests a minute per client address. An unsafe value stops the server at start.

**Differences from the original target CLI:**

- The evaluation runs through `python -m recovery_workbench.evaluation.run`, not `cli evaluate`.
- The API starts with `python -m recovery_workbench.api`, not `uvicorn recovery_workbench.api:app`, because each server start creates and prints a fresh operator token; in public mode it takes `WORKBENCH_DEMO_TOKEN` instead and prints none.

## Known issues when running it

The README lists these under [Boundaries](../README.md#boundaries); here they are in full.

- **A hostile program running under another account on this computer can still do three things** ([security audit](SECURITY_AUDIT.md), findings 7–9; accepted as known limits). A program under the operator's own account needs none of them: it could already read the data directory or stop the server.
  - **Hold idle connections.** The server has no deadline for a request that never finishes arriving. About 250 held connections, at macOS's default open-file limit, use up its file handles, and the UI and every webhook sender are refused until they are released. Public mode, which a public link needs, closes such connections: a request must arrive whole within 15 s of the connection opening or of the previous answer, and an answer its client stops reading is aborted after 30 s.
  - **Pose as the app at `localhost`.** The server accepts the `localhost` name but listens only on 127.0.0.1. A browser that tries `[::1]` first for `localhost` would reach a program listening there on the same port, which could show a fake login page and collect the operator token. Opening `http://127.0.0.1:8000/`, as [section 5](#5-the-one-page-ui) says, avoids it. Unconfirmed: it depends on browser behaviour the audit could not observe.
  - **Replay the session cookie.** Browsers send a cookie to every port of the same host. A program serving another 127.0.0.1 port that the browser visits receives the session cookie, and could use it until logout or restart. Unconfirmed, for the same reason.
- **The webhook route is a sandbox receiver.**
  - It runs no worker.
  - Its rate limit (600 a minute) is per process and held in memory.
  - Each delivery moves the run's replay clock one millisecond and is received at that new time. This happens under the same lock that guards every evidence time the API fixes, so a delivery never lands at or before a cutoff already used.
  - A CLI process working on the same data directory while the API receives webhooks is outside that lock.
  - Its fixture secrets are published, so any local process can sign as the provider.
  - The rules baseline reads a single rejected delivery, with nothing accepted after it, as the receiver rejecting deliveries. So any local process can change a run's rules-mode answer with one unsigned request. The changed answer gates nothing: fix validation and the reprocess plan do not depend on it ([security audit](SECURITY_AUDIT.md), finding 3).
  - A sender that keeps sending past the 1 MiB ceiling may see the connection reset instead of the 413, because the server closes the connection rather than read the rest.
  - Other early answers leave the connection open, and uvicorn reads and discards whatever body the sender still sends. These are the guard's 403 and the 429, and a 404 or 503 that follows a body cut short at the ceiling.
