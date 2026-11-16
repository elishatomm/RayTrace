# Security audit: runs 1, 2 and 3 (2026-11-13, 2026-11-14 and 2026-11-16)

One security audit was run before publication, then a short second pass over the web-facing areas the first deferred. This page records how each ran, what it found, and what was done about each finding.

**Result:** the audit confirmed three findings, all low severity. It found nothing critical, high or medium. A further 3 findings depend on facts the sandbox could not observe, and the audit's budget ran out before it could re-check 5 more.

- Every finding needs a program already running on the operator's machine, because the app listens on 127.0.0.1 only.
- Each fix below was reproduced first by a test that fails on the old code.
- The rest were decided on 2026-11-14: four more were fixed, and four are listed as limits in the README.

The audit covered only part of the code; see [Coverage](#coverage-partial).

**Run 2** found nothing in its six areas, and one of its notes led to one more fix; see [Run 2](#run-2-2026-11-14-the-web-facing-areas).

**Run 3** reviewed public mode and the model chain before any public link: one confirmed medium finding, two needing a deployment fact; see [Run 3](#run-3-2026-11-16-public-mode-and-the-model-chain).

## How it ran

- **Tool:** Cloudflare's open-source `security-audit` agent skill (MIT licence), in its "standard" profile, run once.
- **Target:** a read-only snapshot of commit `f168436`. Between that commit and the fixes below, only tests, comments and documents changed.
- **Method:**
  1. Reconnaissance and a coverage plan.
  2. 18 hunters in two waves, each checked by a coverage critic.
  3. One independent verifier per candidate.
  4. One independent re-check per surviving record.

  In all: 55 agent calls over about 5 hours.
- **Safety:** the project's code ran only in Docker, with:
  - no network and a read-only root filesystem;
  - writes confined to a scratch directory;
  - limits on CPU, memory, processes, file size and time.

  Only synthetic data was used, and nothing outside the machine was contacted.

## Results

| # | Finding | Audit's verdict | Severity | What was done |
|---|---|---|---|---|
| 1 | An approved reprocess could fulfil an order at an amount nothing checked, from a signed capture dated after its receipt | Needs validation: the reproduction was never kept as evidence | — | **Fixed** in `e7e5040`. The target's amount and currency must match the order's |
| 2 | Requests to an integration the run doesn't configure counted in the packet's delivery health | Rejected: an unsigned request to the configured integration has the same effect, by design | — | **Fixed** in `e7e5040`, because the code contradicted what is now RUNNING.md section 8 |
| 3 | One unsigned request can change a run's rules-mode answer | Rejected: by design (see RUNNING.md section 8), and the changed answer gates nothing | — | **Documented** in RUNNING.md's known issues |
| 4 | Request bodies are read and decoded, at any size, before the session or token check | Unresolved (see below) | low | **Fixed** in `265fa53`. Bodies are capped at 64 KiB before they are read, on every route but the webhook route |
| 5 | The deployed `receiver@2` expands unverified webhook bodies before the size, nesting and signature checks | Unresolved | low | **Fixed** in `265fa53`. The receiver hook runs only on bodies within the size and nesting limits |
| 6 | The failed-login window is shared by every caller, and refuses even the right token | Confirmed | low | **Fixed** in `5ee3f5c`. The token is compared first, so the right one always logs in; the window counts wrong tokens only |
| 7 | Idle connections can use up the server's file handles | Confirmed | low (medium impact) | **Listed** in RUNNING.md's known issues. Public mode, not deployed, closes such connections with deadlines ([DEPLOY.md](DEPLOY.md)) |
| 8 | The server accepts the `localhost` name but listens only on 127.0.0.1 | Needs validation: browser behaviour (the sandbox has no browser) | — | **Listed** in RUNNING.md's known issues, with the advice to open `http://127.0.0.1:8000/` |
| 9 | The session cookie also goes to other loopback ports the browser visits | Needs validation: browser behaviour | — | **Listed** in RUNNING.md's known issues |
| 10 | Each plan build re-parses every stored capture body while holding the process-wide lock | Confirmed | low | **Listed** in the README's known issues |
| 11 | Numbering an unsigned attempt counts every earlier one | Unresolved | low | **Fixed** in `0ca1d9a`. One index seek numbers the attempt |
| 12 | The order queue scans every payment once per unknown order | Unresolved | low | **Fixed** in `0ca1d9a`. Payments and held conflicts are grouped once per queue |
| 13 | Each order projection re-reads every held conflicting delivery, body included | Unresolved | low | **Fixed** in `0ca1d9a`. The projection matches its conflicts in SQL and reads IDs only |

Findings 10–13, and 4 and 5, are slowdowns caused by sustained sending. Most need the fixture signing key, which is published in source by design.

**What the audit's verdicts mean:**

- **Confirmed:** a finding gets a severity only once it is confirmed. The severities above are the audit's.
- **Needs validation:** the audit could not observe one specific fact.
- **Unresolved:** the audit's second check re-read every source claim and found it accurate, but it replaced the record's measured evidence with evidence the audit had kept. The audit's rules send such a replacement to a fresh verifier, and the run had none left in its budget. That is why the run is marked incomplete.

## Coverage: partial

The audit planned 41 areas:

- 8 were examined and found clean;
- 10 produced the findings above;
- 23 were deferred: 22 for budget, and 1 that its last coverage check added.

The deferred areas include:

- how the UI renders evidence that webhook senders control;
- the protection against requests from other websites;
- receipt and report output;
- the diagnosis prompt path and the model client;
- the CLI's file handling;
- the data directory;
- settings;
- the locked dependency sets.

So this is a first pass, not a clean bill of health, and a second pass followed.

The audit's raw output (its report, `findings.json`, the coverage ledger and the run metadata) is kept outside the repository, because it contains local paths.

## Run 2 (2026-11-14): the web-facing areas

A short second pass covered the areas run 1 deferred that face the browser.

- **Tool and target:** the same skill, in its "quick" profile, over a read-only snapshot of `53f6a18`. That snapshot predates the login-order and slowdown fixes above. Their code is covered by their own tests and the static re-scan, and in the six areas below it changes only the order of login's checks.
- **Scope:** six areas, each examined by one hunter:
  1. how the UI renders data that webhook senders control;
  2. the request guard and the body limit, including the webhook route's exemption;
  3. the receipt route, as JSON and Markdown;
  4. the run report route;
  5. `/health` and every other response without a session;
  6. the diagnosis route's packet and prompt path.
- **Method:** one hunter per area, one coverage critic, and a verifier for each candidate.
  - A single planning call reused run 1's architecture map instead of four new reconnaissance agents, so that all six hunters fit the budget. The audit's report records this.
  - In all: 10 agent calls of the 15 approved, in about 1 hour 20 minutes.
- **Safety:** as in run 1. No hunter needed to run the project's code.

**Result: no findings.** No hunter found a candidate, so nothing needed a verifier, and the coverage critic accepted the six areas as covered. Browser behaviour was judged from source only, because the sandbox has no browser.

**Its notes found one defect in an earlier fix** (note 11). A request that declared a body over the 64 KiB limit still ran a route that reads no body, such as logout, plan execution or any GET, and only then had its answer replaced with 413. The limit in `265fa53` relied on Starlette's limiter, which refuses only at a route's first read. **Fixed** in `cf9c588`: such a request is now refused before any route runs.

**Twelve more hardening notes** followed: notes 1–10 were built, and 12 and 13 are listed. None lets anyone cross a boundary today; each would stop a future change from creating one.

| Note | Area | Suggested change | Done |
|---|---|---|---|
| 1 | UI | Reject order IDs made only of dots, which the UI cannot open | **Built** in `30a5200` |
| 2 | UI | Escape non-ASCII characters in the report the UI saves, as the CLI does | **Built** in `82a2988` |
| 3 | Guard | Exempt GET, HEAD and OPTIONS from the origin checks instead of checking named methods, so a new method fails closed | **Built** in `30a5200` |
| 4 | Guard | A test that every GET route leaves the database unchanged | **Built** in `30a5200` |
| 5 | Server | Turn off uvicorn's proxy headers, so no loopback client can set the request's client address | **Built** in `30a5200` |
| 6 | Receipt | Escape terminal control and bidi characters in Markdown receipts | **Built** in `82a2988` |
| 7 | Receipt | Check the model ID's and digest's formats where they enter | **Built** in `82a2988` |
| 8 | Report | List the exported fields explicitly | **Built** in `82a2988` |
| 9 | Report | Build the report in one read transaction | **Built** in `82a2988` |
| 10 | Health | Log the model probe's error text instead of returning it | **Built** in `30a5200` |
| 12 | Prompt | Fence a payment ID the run does not hold: today a sender can put up to 128 identifier-shaped characters on the facts line | **Listed** in RESULTS.md's known gaps |
| 13 | Prompt | Escape chat-template strings such as `<\|im_start\|>` inside fenced text | **Listed** in RESULTS.md's known gaps |

Notes 12 and 13 change what the model sees, so building them means a new prompt version.

While notes 1–10 were built, one more place that printed model-endpoint text raw turned up: the CLI demo's rationale and reasons. It is escaped there too.

**Coverage: still partial.** The six areas are now covered, but the rest of run 1's deferred areas are still unexamined. Among them are the CLI's file handling, the data directory, settings, the model client and the locked dependency sets. Run 2 also re-checked none of run 1's findings; the fixes above were verified by their own tests.

Run 2's raw output is kept outside the repository with run 1's.

## Run 3 (2026-11-16): public mode and the model chain

The public-mode hardening required one more review before any public link, because every earlier finding assumed the app listens on 127.0.0.1 only.

- **Tool and target:** the same skill, in its "quick" profile, over a read-only export of `d0a834d`. That commit includes public mode and the model chain, but not the built-in demo runs, which came after it.
- **Scope:** 12 areas, two per hunter:
  1. the request guard's host and origin checks, and the absent webhook route;
  2. login, the `__Host-` session cookie, timeouts and logout;
  3. seeding quotas, and the published seeds;
  4. the per-session limit and the hosted model's daily call budget;
  5. the connection deadlines and the concurrency cap;
  6. forwarded headers and the per-address limit;
  7. the data-directory claim;
  8. migration 8, the table rebuild;
  9. the new settings and how secrets appear;
  10. what the hosted provider's replies become;
  11. the production image's build inputs;
  12. the production image's runtime configuration.
- **Method:** six hunters, one final coverage critic, and one fresh verifier for each candidate. Each verifier tried to refute its candidate and reproduced it independently. In all, 10 agent calls of the 15 budgeted. As in run 2, the coordinator reused the earlier maps instead of four reconnaissance agents.
- **Safety:** any code the review ran ran in Docker, with no network, a read-only root, low limits, and writes only to each agent's scratch folder. No real key or token was used, and nothing deployed was contacted.

**Result: one confirmed finding (medium), and two that need a deployment fact.**

| # | Finding | Status | Severity | What was done |
|---|---|---|---|---|
| 1 | A `Connection: close` request whose small answer the client stops reading keeps the connection open forever. The slow-connection deadline never arms on that path, so held connections use up the 64-connection cap and other viewers get 503 | Confirmed, reproduced twice | medium | Fixed (see below) |
| 2 | The hosted model's daily call budget lives in memory and starts over at every process start; free hosts restart after short idle periods | Needs validation: the host's restart pattern and the provider plan | — | Documented. On Cloudflare Workers Free, calls past the free allowance are expected to be refused rather than billed [Likely] |
| 3 | Public mode's start check accepts a person-chosen demo token, and the login window never stops a guess from being tested (login compares the token first) | Needs validation: whether the deployed token is random | — | Fixed as far as a start check can: a stricter token shape (see below). The login order stays |

**Coverage: partial, by design.** 9 areas were covered and 3 held the candidates above. The final critic named 4 availability areas that no unit had reviewed; they are deferred, because a quick run has no second wave:

- one token holder filling all 20 session slots;
- finished jobs kept in memory;
- the data limit gating only seeding;
- unlimited authenticated read and refresh routes.

Some of these are addressed by the fixes below. Run 1's login-lockout finding no longer applies, and its held-connection finding is superseded in public mode by finding 1.

**Hardening notes** (25 in all) went into the same fix round where they were cheap:

- an HSTS header in public mode;
- provider text that skipped the key scrubber;
- a crash on a deeply nested reply;
- the local-model client following proxy settings;
- the demo token in a config object's repr;
- remembered jobs, and session-slot eviction;
- a writable file and the ignore list in the image;
- the data directory's marker.

**Fixes (2026-11-16, after run 3).** One fix round closed finding 1, tightened finding 3's start check and took the cheap hardening notes and deferred areas. Each fix came with tests that fail on the code before it. Finding 1 was also reproduced independently, before and after the fix, in the production image through a real route: a 56,660-byte answer to a client that read nothing for 40 s was still delivered whole before the fix, and cut off at the 30 s deadline after it.

- **Finding 1:** when an answer completes on a connection uvicorn is closing, public mode arms the 30 s abort.
- **Finding 3:** the demo token must have the shape `secrets.token_urlsafe(32)` writes: 43 to 256 characters, at least 3 uppercase and 3 lowercase letters, a digit, 20 different characters, and no 8-character sequence that occurs twice. About 1 generated token in 1,600 is refused; draw again. A long chosen passphrase with capitals and a digit still passes, so [DEPLOY.md](DEPLOY.md) says to generate the token. The login order is unchanged.
- **Deferred areas:**
  - a login ends the session its browser presented, and with all 20 slots live it ends the session unused longest instead of refusing, logged as `session_evicted`;
  - the server keeps every live diagnosis job and its 64 newest finished ones, in public and loopback mode alike; an older job's ID answers 404, and its diagnosis stays in the run's list.
- **Hardening notes:**
  - `Strict-Transport-Security: max-age=31536000` on every public-mode answer;
  - the provider's `finish_reason`, `Content-Encoding` and `Content-Type` text goes through the key scrubber, and only documented finish reasons are kept;
  - a hosted reply nested too deeply to parse now needs review instead of failing the job;
  - the local-model client ignores proxy settings from the environment;
  - the server configuration's repr hides the token;
  - the image drops a world-writable lock file, its example run binds 127.0.0.1, and `.dockerignore` keeps local data, other `.env` files and key files out of the build;
  - the data directory's marker is created with `O_EXCL | O_NOFOLLOW`, and with public mode off the server and every CLI command refuse a directory public mode marked, because its next start would empty it.

**Left open:**

- a token holder can keep ending other viewers' sessions (each is logged, and they can log in again), and per-session quotas start over at each login;
- the local-model client's JSON reader can still fail on a deeply nested reply (that model listens on loopback only);
- the evaluation entry points do not check the marker (they need an explicit `--data-dir`);
- the key scrubber matches an echoed key only in its original case;
- uvicorn's own 503 and 400 answers bypass the app, so they carry no HSTS or other security headers, as before;
- finding 2 (the per-start budget) stays documented, not changed;
- two of the four deferred areas are unchanged: the data-size limit gates only seeding, and reads, approval and refresh have no per-session limit (public mode's per-address limit applies only behind proxies a setting names).

Run 3's raw output is kept outside the repository with runs 1 and 2.
