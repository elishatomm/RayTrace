# The recorded demo

A copy of the one-page UI as plain static files (HTML, JS, CSS, JSON and Markdown) that any static host can serve. It runs
the production UI from `frontend/src`, unchanged, with no server behind it: every answer the page shows is a response the
real API gave in rules mode, recorded while headless Chromium took the README's three flows. Nothing runs, no provider is
called and no money moves. All data is synthetic.

Nothing here deploys, uploads or publishes anything.

## Build it

```bash
scripts/demo/build-demo.sh <output-dir>
```

- **Needs** Docker, npm, Node and `frontend/node_modules` (`npm ci` in `frontend/` once; that is the only step that needs
  the network). `<output-dir>` must be outside the repository, and empty or new.
- **What it does.**
  1. Copies the checkout to a temporary folder and builds the production UI there, as `scripts/ui-walkthrough/run-walkthrough.sh`
     does. Nothing is written inside the repository.
  2. Builds the UI walkthrough image from `scripts/ui-walkthrough/Dockerfile.ui`, unchanged, with that script's build
     context. Every layer that downloads (the base images, the walkthrough's Python packages, the project's locked
     dependencies) comes from Docker's cache once the walkthrough image exists. A checkout no cached build has seen
     rebuilds only the layers from its copy on: the copy, the project's editable install (uv builds it directly and
     downloads nothing) and two small file steps. `DEMO_IMAGE` overrides the tag (`rw-ui-walkthrough:local`, the
     walkthrough's own).
  3. **Records.** Runs that image with `--network none` and this folder mounted read-only. `record.sh` starts the API in
     rules mode on an empty data folder; `record.py` drives the production UI through the three flows (below), checks 30
     values the docs give (see "Values" below), and writes the fixtures and receipts.
  4. Builds the demo UI: `tsc` on `frontend/demo`, then `vite build --config frontend/demo/vite.config.ts`.
  5. Checks the site (`check-site.mjs`): the security headers, the meta policy, no inline script or style, no other
     origin, the sizes, and no local path, user name or host name.
  6. Copies the site to `<output-dir>`.
- **How long.** About five minutes; the recording takes about four.
- `DEMO_RECORDING_SUMMARY=<file>` also keeps the recording's summary: run IDs, stage clocks, the values checked against
  the docs, and the headers the API sent.

## Test it

```bash
scripts/demo/test-demo.sh <site-dir> <results-dir>
```

In the same image, with `--network none`: `static_server.py`, a plain standard-library server, serves the site on the
container's own loopback with the headers of the site's `_headers` file, and `test_demo.py` drives headless Chromium. It
writes `demo_results.json` and screenshots at 1440 and 390 px to `<results-dir>`, and exits 0 only if every check passes.
Every value it expects is read from the site's own recording, so a check says the page shows what the API answered; the
documented values are checked too. It checks:

- the recorded-demo banner at the top of every view, and the "Synthetic demonstration" badge;
- all three flows, step by step: the money strip, the queue counts, the diagnosis, the plan, the rail's receipt hash,
  the escalations, against the recorded answers;
- every Show filter at every step of every flow, and the case view of the first order each sort lists;
- the receipts open from the site's static files, as the API served them;
- the controls the recording cannot answer give an honest message, and logging out asks for no token;
- that no request leaves the origin, with no Content-Security-Policy violation, page error or failed request.

## What it covers

The README's three flows, with the demo walkthrough's steps, so the values match the docs:

- **Flow A**, `worker_backlog` seed 101 (`run_53966a08d4`): diagnose; validate `payload@5`; build the plan (98 targets,
  INR 119,592.00); refresh the 5 stale-only orders and re-plan (103 targets, INR 125,027.00, hash `9db6c8c60bee4995…`);
  approve; execute (restored INR 125,027.00, new capture INR 0.00); execute again (the replay). Then `Complete (148)`,
  sorted by Amount, opens `order_SYNTHETIC_547a954d_0099`, its case and its receipt (`eb9d6f30f04d9ebc…`).
- **Flow B**, `method_degradation` seed 101 (`run_a59cd5d713`): diagnose (`provider_method_degradation`, `upi`); escalate
  (evidence needed: `compare_method_cohorts`).
- **Flow C**, `late_authorization` seed 101: diagnose (`ambiguous_payment_state`); refresh provider state (read-only);
  escalate.

The visitor may take the flows in any order and switch runs: each run keeps its own place in its flow. At every step each
Show filter works, and so does the case view of the orders each filter lists first (in order-ID order, and by Amount and
by Age), and of the orders the docs name. The report export works at every step. The demo starts logged in as
`op_demo_operator`, the operator of the recording.

## What it does not cover

Anything the recording did not do gets an honest message in the page's own error banner, repeated in the demo banner,
for example "Not in this recorded demo: validating the corrected handler config@5 on run_53966a08d4. The recording
validated payload@5, as the README's Flow A does: choose it under Corrected handler. The demo covers the README's three
flows." Nothing is invented. That covers:

- creating a run with another scenario or seed (a recorded scenario and seed opens its recorded run, as public mode does);
- advancing the replay clock;
- the Local model and Recorded run modes (nothing here calls a model);
- other corrected handlers, other step orders (escalating Flow C before its refresh, approving the first plan), a second
  diagnosis, a third execution;
- the case of an order no filter lists first.

Known limits:

- **Switching runs half-way through a flow.** Opening a run clears the page's diagnosis view, as it does with the real
  server, and the recording diagnosed each run once. A flow left half-way cannot be finished; **Restart the demo** starts
  every flow again. A finished flow can be revisited at any time.
- **The receipt tabs** show the receipt files exactly as the API served them, so they carry the receipt's own boundary
  statement ("Synthetic sandbox: no real money, …") but not the demo banner.
- **The top bar's wall clock** is the visitor's clock; every other time on the page is the recording's replay time.

## How it is labelled

- A banner sticks to the top of every view, at every width: "Recorded demo. Every answer on this page is a recorded
  response from the real RayTrace API in rules mode. Nothing runs here: no provider is called and no money
  moves. All data is synthetic." Beside it, **Restart the demo**.
- Under it, **What this recorded demo covers** lists each flow's run and steps, and what the recording does not cover.
- The top bar keeps its "Synthetic demonstration" badge; the page title is "RayTrace (synthetic sandbox):
  recorded demo".
- No token is asked for. After **Log out**, the login card says no token is needed and offers **Continue as
  op_demo_operator**.

## How it works

- **Where `fetch` is replaced.** `frontend/demo/index.html` loads `frontend/demo/src/main.tsx`, which calls
  `installReplay(window)` before it renders the production `App` under the banner. Every request the page sends goes
  through `call()` in `src/api.ts`, and so through `window.fetch`: requests for `/api/…` and `/health` are answered by the
  replay; requests for the site's own files go out unchanged; a request to another origin is refused before it is made.
- **Two module swaps, demo build only.** `frontend/demo/vite.config.ts` gives every importer of `src/api.ts` the stand-in
  `demoApi.ts`, which re-exports it with one change: `receiptLink` points at the static receipt files. It also swaps
  `src/components/LoginPanel.tsx` for `DemoLoginPanel.tsx`, which asks for no token. The build fails if either swap does
  not happen. No production source file changes, and the production build is unchanged.
- **Recording.** A script added to every page wraps `fetch` and keeps each request (method, path and query, the JSON body
  of a state-changing request) and each answer (status, content type and text). It keeps no header, so no cookie or
  `Set-Cookie`, and never the body of the login request. Each answer is filed under its run and the **stage** of that run's
  flow: the server state between two state-changing requests. At each stage the recorder chooses every Show option, reads
  the first rows under each sort, opens their cases and exports the report.
- **Replay.** Per run, the replay keeps the current stage. A read gets the answer recorded for the same method, path and
  query at that stage (answers recorded as a sequence, like a diagnosis job that was queued and then done, are served in
  order). A state-changing request moves the run to the next stage only if it is the request the recording made next
  there, with the same JSON body; it then gets the recorded answer. Anything else is refused as above. The state lives in
  the page, so reloading the page, or **Restart the demo**, starts again.
- **Fixtures.** `demo-data/manifest.json` (the session answers, the runs, their stages and the recorded state-changing
  requests with their answers) and one file per stage of each run, named by its content hash; an answer unchanged since an
  earlier stage is stored once. They are about 10.0 MB in 17 files (the largest 1.0 MB), so they are generated at build
  time and not committed. With the receipts (0.4 MB) and the page (0.3 MB), the site is about 10.7 MB in 23 files.
- **Receipts.** The build writes the one recovery action's receipt as `receipts/<run_id>/<action_id>.json` and `.md`:
  the bytes the API served for the case's receipt links (341,391 and 29,985 bytes).
  The case view's links and the rail's link point at these files; the rail's own read of the receipt is answered from the
  same file.
- **Values.** `record.py` checks 30 recorded values against the README and the demo walkthrough, among them Flow
  A's money strip, both plans, the execution, the receipt hash `eb9d6f30f04d9ebc…` and the queue after the recovery; each
  flow's late-authorisation facts; Flow B's diagnosis and escalation; and Flow C's refresh (76 payments). A difference
  fails the recording, and the build stops before anything reaches the output folder.

## Security and privacy

- The recorder never keeps a header or the login body. After writing, it scans every file it wrote for the operator token
  and the session cookie's value; if either is found it deletes the recording and the build fails. The token and the API
  log stay inside the container.
- `check-site.mjs` fails the build on a local path (`/Users/`, `/home/`, `/var/folders/`, …), the checkout's or the
  output's path, or the user or host name in any file of the site.
- The demo makes no request outside its own origin: no CDN, no external fonts, no analytics. The test counts every request
  the browser made.

## Content-Security-Policy on static hosts

- **`_headers`** (Cloudflare Pages and Netlify syntax) sends, for every path, the same `Content-Security-Policy`,
  `X-Content-Type-Options`, `X-Frame-Options` and `Referrer-Policy` the API sends (`SECURITY_HEADERS` in
  `src/recovery_workbench/api.py`). The build fails if they differ from what the API sent while recording. Other hosts
  need the same four headers set in their own configuration.
- **The meta policy.** `index.html` also carries the policy in `<meta http-equiv="Content-Security-Policy">`, and the
  referrer policy in `<meta name="referrer">`, for hosts that cannot send headers. A meta policy cannot enforce everything:
  - `frame-ancestors` is ignored in a meta policy, so it is left out, and `X-Frame-Options` exists only as a header:
    without real headers, another site can frame the demo (clickjacking);
  - `X-Content-Type-Options: nosniff` exists only as a header;
  - a meta policy applies only from where it appears in the document, and cannot report violations (`report-to`) or
    sandbox the page;
  - it does not reach the receipt files, which are served as JSON and Markdown, not HTML.

## Files

| File | What it is |
|---|---|
| `build-demo.sh` | Records in Docker, builds the demo, checks it and writes the site |
| `record.sh`, `record.py` | The recording, inside the walkthrough image |
| `check-site.mjs` | The site checks the build runs |
| `test-demo.sh`, `test_demo.py`, `static_server.py` | The test, inside the walkthrough image |
| `../../frontend/demo/` | The demo entry (`index.html`), Vite config, `_headers`, the replay, the banner and the two stand-ins |
