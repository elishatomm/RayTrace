# Frozen research results

These sections were moved here from the README, unchanged apart from their links and the words "in this evaluation" added to one claim. Each result is frozen: it was measured under the settings it names, and later changes to the product do not re-score it. The README's [Results](../README.md#results) summarises them, and the release's own checks are reported separately.

## Model

The diagnosis model was chosen by milestone M0 before the rest was built. M0 ran ten frozen evidence packets, built from the merchant fixture's real stack traces and diffs, with one frozen prompt. The gate was at least 8/10 hypotheses correct at about 30 s. The full record:

| Model | Thinking, sampling | Hypotheses | Unsafe proposals | Warm latency median | Gate |
|---|---|---|---|---|---|
| qwen3:4b | off, greedy | 7/10 | 3/10 | 4.6 s | failed |
| qwen3:4b | on, greedy | 5/10 | 0/10 | 90 s (deadline) | failed |
| qwen3:4b | on, recommended sampling | 8/10 | 1/10 | 83 s | failed at 30 s |
| **qwen3:8b** | **off, greedy** | **8/10** | 2/10 | **7.2 s** | **passed** |
| qwen3:8b | on, recommended sampling | 8/10 | 2/10 | 23.2 s | passed |

- **qwen3:4b failed** (7/10, with 3 unsafe proposals).
- **qwen3:8b passed with no margin**, exactly 8/10, at 7.2 s warm with thinking off. That is the default setting (`WORKBENCH_MODEL=qwen3:8b`, `WORKBENCH_MODEL_THINK=false`). Caveats:
  - the M0 packets were written by the same agent that wrote the prompt;
  - every model misread the traffic-mix packet as a degradation.
- **Temperature-0 output was not fully reproducible on this stack.** A live re-run of one packet flipped the proposed operation.
- **Recorded mode exists** so that a demo can replay a real, earlier model output for an identical packet, clearly labelled as a replay.

Runtime metadata, as recorded in the dev log and `experiments/m0/m0_model_report.json`: `qwen3:8b` in Q4_K_M quantisation (5.2 GB, digest `500a1f067a9f…`), served by Ollama 0.40.1 on a 24 GB laptop.

Since M0, the application adds server-side policies the model cannot bypass:

- an operation policy per hypothesis;
- change consistency;
- a fixture-reproduction check;
- val-4: a method degradation must name its method;
- val-5:
  - `no_incident` is refused when any detector other than `orders_not_completing` alerted for the incident (its trigger, or `fired` / `fired_since_open` in `ev_trigger`). The one exception is `unresolved_spike` when the packet shows an observation gap: more unknown than created payments, and coverage below 0.9 or no provider snapshot. That is the case the rules baseline itself calls no incident.
  - `merchant_worker_regression` is refused when the packet holds no `worker_error` item.
- val-6 (phase 9; adopted after round 3, whose pre-registered decision found that it helps: 16 benefits, 0 costs):
  - `unknown` is refused when it names a faulting deployment;
  - `receiver_rejecting_deliveries` is refused unless the packet's delivery health shows rejected deliveries (`distinct_events_rejected` above 0, read fail-closed).
- val-7 (adopted 2026-11-13, shipped under a pre-registered rule; see `experiments/val7_repair/`):
  - a rationale that names a deployment ID the packet does not list as a candidate is refused (a garbled or invented ID);
  - a named faulting change is refused unless an item cited as supporting mentions it: its deployment item, or another item that names it, such as the fixture check's deployed version or the worker errors' per-version counts.

A refused answer gets one repair call. If it is refused again, the diagnosis needs review.

- **Unsafe proposals:** a proposal blocked by the operation policy or by change consistency is recorded as unsafe, even when a repair replaces it.
- **Evidence checks:** the fixture, method, val-5, val-6 and val-7 rules check the answer against the evidence instead. Their refusals stay in the diagnosis's reasons and are not counted as unsafe proposals.

## Evaluation results (M4)

**Setup.** The frozen TEST split has 30 runs: six diagnosis families × 5. It was evaluated **once**, with settings written to [`evaluation/frozen_settings.json`](../evaluation/frozen_settings.json) *before* the test data existed:

- `qwen3:8b` with thinking off, chosen on the 18 development runs by a pre-registered rule;
- prompt `diag-p2`, validator `val-4`, packet `pkt-2`, rules `rules-v2`.

**M4's figures are `diag-p2` / `val-4` / `rules-v2` results and are not re-scored.** The application now runs validator `val-7` (adopted 2026-11-13, building on phase 9's `val-6` and phase 8's `val-5`), prompt `diag-p5` (phase 10) and, in rules mode, `rules-v3` (see [Phase 11b](#phase-11b-does-the-local-model-add-attribution-value-pre-registered-held-out-variants)). The evaluation harness still diagnoses with rules-v2, so M4 reproduces unchanged.

- **`diag-p5` = `diag-p4` + a field policy.** It is round 3's adopted `diag-p4`, which removes commit messages from the model's input (see [Round 3](#round-3-fence-validator-and-strip-on-fresh-seeds)), plus an explicit rule for every `facts` value (see [docs/ARCHITECTURE.md](ARCHITECTURE.md) §4).
- **Not separately measured.** `diag-p5` was not measured by a round of its own. On every stored packet its only change is that fixture failure details move into a fence.
- **A smoke check, not an improvement.** A one-off check re-ran the 18 development packets under `diag-p5` + `val-6` with M4's frozen model settings: 15 of 18 outcomes correct, against M4's 10.
  - Where the prompt was unchanged, the model's first reply was byte-identical to M4's. The gains there came from validator rules written with these answers in view.
  - On the 6 packets `diag-p5` itself changes, it scored 5 of 6, the same as before.

  See [evaluation/runs/dev_diag_p5_smoke/](../evaluation/runs/dev_diag_p5_smoke/).

- **What `val-5` would have refused, offline:** 5 of the 30 TEST final answers and 5 of the 18 development ones. All were scored wrong, and each was a worker regression in a run without worker errors.
- **Why this is not a new score:** each of those answers would have had one repair call, whose outcome is unknown.

Full tables, by family and variant, are in [evaluation/summary.md](../evaluation/summary.md); per-case records are in `evaluation/results.json`.

**Outcome correct** means the right cause and an acceptable action, or a correct abstention. Raw counts:

| Family | Rules | Model | Plainly |
|---|---|---|---|
| D1 worker regression after a deploy | 3/5 | 5/5 | The model wins, **only** on the 2 delayed-onset runs (rules 0/2, model 2/2). They tie 3/3 when failures start right after the deploy |
| D2 provider/method degradation | 5/5 | 5/5 | Tie |
| D3 receiver rejects valid deliveries | 5/5 | 2/5 | Rules win |
| D4 ambiguous or late-authorised state | 5/5 | 0/5 | Rules win |
| D5 no real incident | 5/5 | 0/5 | Rules win |
| D6 innocent deploy near D2/D5 | 5/5 | 3/5 | Rules win |
| **All** | **28/30** | **15/30** | **Rules win** |

One D6 model answer gave the right cause, method and action but said the faulting change was `unknown` rather than `none`. It counts as correct because it did not blame the innocent deploy. Under a strict reading the model scores **14/30**.

**What this shows**

- **The rules baseline (rules-v2) is better at triage.** It uses detector state, structured error categories, provider error fields, the latest change before the first failure, and top-frame blame. That makes it better at deciding *whether* there is an incident and *what kind*. The model over-diagnosed: on D4 and D5 it never abstained. Five of its final answers proposed reprocessing where the oracle does not accept it:

  - three on runs with no real incident (two D5, one D6);
  - two on receiver-rejection runs (D3), whose events never reached the inbox.

  All five ended blocked or with nothing to reprocess.
- **The model's one advantage is the case it was aimed at.** It named the faulting deploy when an innocent deploy landed between the faulty deploy and the first failure (delayed onset). In one of those runs the innocent deploy touched the very file in the stack trace.
- **Safety held for both systems in this evaluation.**
  - The policy layer refused 21 model proposals, counted even when a repair replaced them.
  - Every wrong blame pointed recovery at a module that no allowlisted fix changes, so no fix could be validated and recovery was blocked.
  - Across 15 recovery attempts there were **0 executed errors, 0 new capture and 0 duplicate fulfilments**, and every executed recovery fulfilled exactly the oracle's eligible set.
- **Latency:** the model's warm median was 11.2 s per diagnosis (11.3 s cold).

**Read these results narrowly**

- Five runs per family is a tiny sample. Treat the numbers as raw counts and make no claims beyond this simulator.
- Nothing here is blind. The same agent wrote the simulator, the manifest and the rules. The rules also got one tuning pass on development runs; the model got none.
- A second model run on TEST reproduced the first exactly. That shows determinism at these settings, not population variance.
- **The delayed-onset variant was added on purpose.** Without it, failures in this simulator begin within about 30 s of a faulty deploy, so "latest change before the first failure" alone decides attribution. It is labelled and reported separately. Without it, the rules tie or beat the model in every family.
- The EVALUATION §9 citation spot check (do the cited evidence items support the claim?) is prepared for a human in [`evaluation/citation_spot_check.md`](../evaluation/citation_spot_check.md). It has **not** been filled in yet.

## Planted instructions (stretch item 2)

Some evidence is written by people outside the workbench, such as the order note a customer types (which checkout logs) and a deploy's commit message. [`experiments/injection/`](../experiments/injection/README.md) plants an instruction in one of them. The texts are fixed and use only IDs from the world. There are three forms:

- **Suppress** says the incident is already resolved and asks for `no_incident` / `no_action`. It goes in an order note, in D1 and D2 runs.
- **Misattribute** blames a deploy that caused nothing. It goes in a commit message, in D1 and D6 runs.
- **False approval** claims an operator pre-approved reprocessing every order. It asks for `reprocess_inbox` with `reproduced_in_fixture`, and cites an evidence ID that no packet has. It goes in an order note, in D2 and D4 runs.

Twenty fresh worlds were diagnosed, each once clean and once per applicable note: 50 live diagnoses. They used seeds 7101–7155, which M4 never used, and the frozen M4 settings: `qwen3:8b`, think off, `diag-p2`, `val-4`.

**Inert in EVALUATION.md's sense: no authority or tool change.**

- **Nothing marks the note.** It is an ordinary log line or commit summary; nothing in the database labels it.
- **The rules baseline did not move:** it gave the same answer with and without the note in 30 of 30 pairs. That was rules-v2; `rules-v3`, which rules mode runs, also gives the same answer in all 30, and in all 46 pairs of rounds 2 and 3.
- **An obedient model gains nothing.** A fake model that obeys every note exactly:
  - cannot cite the invented ID, because of the per-call schema enum and the validator;
  - cannot propose reprocessing for a provider-side or ambiguous incident, because of the operation policy;
  - creates no plan, approval or action with its diagnosis.
  - Reprocessing still needs an operator-validated fix and an approval of the exact plan hash ([`tests/test_planted_instruction.py`](../tests/test_planted_instruction.py)).

**The live model was influenced.** Counts are over the 30 planted runs. The bracket gives the same count for their clean twins, which had no note.

| Note | The final answer did what the note asked |
|---|---|
| Suppress | 3/10 (0). All three are D2 runs where a provider degradation became `no_incident` / `no_action`, an answer that validates |
| Misattribute | 7/10 (2): the blamed deploy is named as the cause |
| False approval | 2/10 (2) proposed reprocessing. None took the demanded evidence level or cited the invented ID. Replies asked for reprocessing in 8/10 (3), and the operation policy refused the D2 ones |

- **Correctness fell:** correct outcomes went from 24 to 18 of 30 pairs.
- **Recovery would still stop at fix validation.** All 9 wrong reprocessing proposals named no change, or a module with no allowlisted fix.
- **Sample size:** five pairs per cell at temperature 0.6. A difference of one or two runs in a cell is not evidence on its own.

**What the defences do not catch:**

- **Since `val-5`, the two legal-but-wrong shapes this study found are refused**:
  - `no_incident` while another detector alerted;
  - `merchant_worker_regression` in a packet with no `worker_error` item.

  The rules were designed from these 50 runs, so they are measured again on fresh seeds ([`experiments/injection/round2/`](../experiments/injection/round2/)).
- **Still legal. The gate after these is the operator:**
  - `unknown` with `no_action` on an alerting incident;
  - `no_incident` where only the order-completion symptom alerted, which includes same-time contradictions in payment state. In every frozen M4 packet where `no_incident` is the right answer, the method-drop detectors had too few samples to judge, so this acceptance reads an absent alert, not a measured healthy method;
  - a wrong worker regression in a run that does have worker errors.
- **A deploy's commit message reached the model unfenced, up to prompt `diag-p2`.** It sat on the packet's `facts` line, which the system prompt calls workbench measurements; only its copy in the deploy pipeline's log line was fenced as untrusted text. Prompt `diag-p3` (phase 9) fenced it. Round 3 found no detectable effect from the fence. The adopted `diag-p4` removes it from the model's input altogether.

### Round 2: validator `val-5` on fresh seeds

`val-5` adds two rules to the validator:

- `no_incident` must agree with the alert history;
- `merchant_worker_regression` needs a worker-error item in the packet.

[Round 2](../experiments/injection/round2/PREREGISTRATION.md) measured `val-5` against `val-4` on 25 fresh worlds, seeds 8101–8155, which neither M4 nor round 1 used.

- **Cases.** Round 1's notes and channels, each with a clean twin, plus 5 clean D5 controls.
- **Runs.** 55 live diagnoses per arm, with the frozen M4 model settings.
- **Pre-registration.** The metrics and the decision rule were fixed and committed before the `val-5` arm ran. Two dated addenda record the later wording of rule 1.

**Results:**

- **The validator is the only difference.** All 55 first replies were identical in the two arms, so every difference starts at the validator's decision.
- **Correct outcomes rose from 26/55 to 30/55.** `needs_review` rose from 0 to 5, and runs with a repair from 22 to 40.
- **Notes followed in the final answer:**
  - suppress: 5/10 → 0/10;
  - false approval: 2/10 → 0/10;
  - misattribute: 8/10 → 8/10. In D6 the repairs moved to `unknown` and kept the blamed deploy; neither rule addresses a wrong attribution.
- **Paired outcomes.** 9 benefits (4 wrong → correct, 5 wrong → `needs_review`) and 0 costs. 7 of 15 followed notes were blocked, and none was induced.
- **Pre-registered verdict: helps.** One benefit depends on rule 1's final wording; under the earlier wording the verdict is the same.
- **Limit: the live runs did not test false refusals.** No correct `val-4` answer was of a kind either rule examines, so "0 false refusals" says nothing about how often the rules refuse a right answer. The structural bound comes from elsewhere: across all 153 packets generated so far (48 frozen M4, 50 round 1, 55 round 2), no expected answer meets either refusal condition.
- **Independently checked.** Every figure was recounted and re-ran the `val-5` arm.

### Round 3: fence, validator and strip on fresh seeds

[Round 3](../experiments/injection/round3/PREREGISTRATION.md) was committed before any model call.

- **Codes.** It ran four committed codes as a chain, each step changing one thing.
- **Arms.** Two model seeds each, eight live arms, on 52 cases from 36 fresh worlds (seeds 10101–10158): D1 and D6 with a misattribute note in a commit message plus clean twins, and D2–D5 clean.
- **Primary measure.** *Attack-induced blame*: the planted case names the blamed deployment and its clean twin, at the same seed, does not.
- **Statistics.** Exact sign tests over worlds, with Holm across the two "helps" tests.

| Step | Change | Verdict | Key figures |
|---|---|---|---|
| 1 | Fence the commit message (`diag-p3` vs `diag-p2`) | **No detectable effect in this sample** | Induced blame 21 → 16 (7 worlds better, 3 worse; p = 11/64). Blame prevalence 23 → 23. Clean twins blamed that deployment more often under the fence (2 → 7) |
| 2 | Validator `val-6` vs `val-5` (both fenced) | **Helps** | 104 of 104 pairs comparable. 16 benefits (1 wrong → correct, 15 wrong → `needs_review`), 0 costs, 0 false refusals of 48 correct answers. 10 of 23 followed notes blocked, 0 induced. Clause (a) was behind 14 of the 15 reviews |
| 3 | Remove the commit message (`diag-p4` vs `diag-p3`, both `val-6`) | **Helps** (Holm) | Induced blame 9 → 1 (6 worlds better, 0 worse; p = 1/64). Prevalence 13 → 4. Clean-case guard: 4 harmed, 4 bettered |

- **Adopted under the pre-registered rules:** `diag-p4` + `val-6`. The fence stays in the code path only as `diag-p4`'s predecessor.
- **Correct outcomes per 52 cases** (descriptive; each figure is seed 20261111, then seed 20261112):

  | Code | Correct outcomes |
  |---|---|
  | `diag-p2` + `val-5` | 23, 30 |
  | `diag-p3` + `val-5` | 24, 24 |
  | `diag-p3` + `val-6` | 25, 24 |
  | `diag-p4` + `val-6` | 30, 33 |

  `needs_review` rose from 1 per arm to 5–6, which is the price of sending wrong answers to a person.
- **What it does not show.** The model was not made to resist planted text. The fence had no detectable effect in this sample; removing the text removed the attack by construction; the validator turned wrong answers into reviews.
- **A determinism caveat.** Under `diag-p4`, a planted case's prompt is byte-identical to its clean twin's, yet 6 of 32 first replies differed. The server had just answered the identical prompt and reused its cached prefix. Determinism held across arms, where the case order is the same: every cross-arm check was identical.
- **Independently checked.** Every figure was recounted from the raw files with a script written before any result existed, and re-ran two arms live. All 52 first replies and all 52 final answers were identical in each.

### Phase 11b: does the local model add attribution value? (pre-registered, held-out variants)

[The test](../experiments/selective/PREREGISTRATION.md) was pre-registered before any model call.

- **New code.** 12 new merchant code variants (6 faulty, 6 innocent), written by an agent that never saw the rules or the prompt.
- **Worlds.** 48 held-out worlds (24 entries × 2 seeds) and 10 control worlds.
- **Systems compared.**
  - `rules_v3`: rules-v2's hypothesis with a stronger deterministic attribution, `attribution-v1`. It reads the same packet as the model.
  - A selective system, which sends multi-candidate or delayed-onset merchant failures to the local model (`diag-p5` + `val-6`, two model seeds).

| Decision | Verdict | Figures |
|---|---|---|
| 1. Selective model against `rules_v3` | **Hurts**: by the pre-registered rule, it named more wrong deployments | Correct attributions 77 against 76 of 96. Wrong ones 19 against 16, over all 116 answers. By entry: 3 better, 3 worse, 18 tied (p = 21/32 each way) |
| 2. `rules_v3` against rules-v2 | **Adopt `rules_v3`**, adopted: rules mode now answers it, and rules-v2 is kept for reproducing M4 | 38 correct and 8 wrong, against 32 correct and 16 wrong, of 48 |

- **What it means.** On fault variations neither system was built on, the local model added no attribution value over a strong deterministic comparator. The product stays rules-first, and the model is experimental assistance.
- **The model alone,** on every held-out world: 70 of 96 correct, 26 wrong.
- **An agreement design** accepts the model only when it confirms `rules_v3`. It would have named a wrong deployment 11 times instead of 16, at the cost of 16 reviews. That is descriptive only, and would need its own test.
- **Routing.** 34 of 48 held-out worlds went to the model; 0 of 10 controls did.
- **Independently checked.** A recount from the raw files, by a script written before the run, matches every figure. A live re-run of one arm reproduced all 34 first replies and final answers.
- **Limits.** The 24 entries reuse 6 fault variants. Everything is synthetic, and one model was tested.

## Known gaps in the defences

The README lists this under [Boundaries](../README.md#boundaries); here it is in full.

- **Some legal-but-wrong diagnoses still pass the validator, and some outside text still reaches the model unfenced.** The planted-instruction study found both.
  - `val-5` refuses the two shapes it found.
  - These still validate: `unknown` with `no_action`; `no_incident` where only the order-completion symptom alerted (as in a same-time contradiction); and a wrong attribution in a run that has worker errors.
  - Commit messages are removed from the model's input since prompt `diag-p4` (round 3). Since `diag-p5` (phase 10) every `facts` value has an explicit rule:
    - fixture exception messages render only fenced;
    - provider failure reasons and worker exception categories render only when allowlisted;
    - identifiers render only when they match a strict pattern;
    - any other value renders as a placeholder, with its raw text fenced.

    Item titles and the candidate lines still repeat some identifiers unchecked.
  - Two gaps the security audit's run 2 noted are accepted as known limits: a payment ID that a webhook sender chose, up to 128 identifier-shaped characters, reaches the facts line outside any fence; and chat-template strings such as `<|im_start|>` inside fenced text pass through literally. Closing either changes what the model sees, so it needs a new prompt version.
  - `val-6` and `val-7` cannot catch a wrong deployment blamed as a worker regression in a run that has worker errors (D1) when a cited item names that deployment; `val-7` refuses it only when nothing cited names it. Only removing the planted text helped there.
