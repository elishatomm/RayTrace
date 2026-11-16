# Selective AI for deployment attribution (phase 11b): pre-registration

**Status: final.** Written and finalised on 2026-11-12 by Agent G (phase 11b) from the orchestrator's design, before
any result: no router, rules system, comparator or model has been run on a test packet.

**One revision before it was final.** The draft was revised once, so the revision is a design change, not an
addendum. The orchestrator's decisions on the draft's six open points:

1. **The unit of decision 1 is the held-out manifest entry,** not the world. An entry's two worlds share their
   variants, position and onset, so they are not independent.
2. **Wrong attributions count over all 58 worlds,** controls included, for S and for R.
3. **M-all's operational failures and routes are descriptive.** Its code, pre-registration and run-record checks
   gate whether its figures are reported.
4. **The agreement variant is pre-registered here,** as a descriptive figure.
5. **The claim's "unsafe proposals" clause is met by construction for final answers.** Policy-blocked runs are
   reported per arm as a cost.
6. **The wrong-attribution clause stays strict.** Any excess of S over R means "hurts": shipping the model route is a
   safety decision.

**What fixes this file.** The build records its SHA-256 in `cases.json`, and the orchestrator commits both before
any model call and before any system runs on a test packet. From then on this file is never edited: a change goes
into a dated `ADDENDUM_<YYYY-MM-DD>.md`, recorded with its SHA-256 in `addenda.json`, and every stage refuses an
addendum that is unrecorded or changed. The arm stage and the summariser refuse a pre-registration whose first line
says DRAFT.

## The claim, and the stopping rule

Fixed before any phase-11b work (`docs/EXTENSION_PLAN.md`):

> Routing only deployment-attribution cases to the local model, behind the validator, identifies the faulting
> deployment more often than a stronger deterministic baseline. It must do so without more wrong attributions.

**One test.** If the routed system does not beat the comparator under the rule below, the product stays rules-first
and the model is labelled experimental assistance. No follow-up round chases a win.

**Honest prior.** The model's only wins so far were 2 delayed-onset runs, and a stronger baseline may erase them. On
development data, every case the router sends to the model is one rules-v3 already gets right (46 of 46, Agent E).
So the model can only tie or lose on those, and any advantage must come from held-out variants where attribution-v1
misses and the case routes to the model. A "no detectable advantage" result is likely, and worth publishing.

## Worlds and seeds

- **Generator.** Each world is one manifest parameter set bound to one seed, generated as a `test`-split run and built
  exactly as M4 builds a packet (`evaluation.run.build_case_packet`) at the run's cutoff. The held-out manifest fixes
  its entries' split as `test`; the controls use it too, so all 58 worlds draw commit messages and distraction lines
  from the same pools.
- **Seeds: 13101–13158**, the block reserved for this test (DEV_LOG, phase 11b). The code refuses any seed outside it,
  any seed taken twice and any M4 manifest seed (`selective_design.experiment_cases`). The build records the check
  against every earlier seed range in `cases.json` (`seed_check`); none overlaps.
- **Held-out: 48 worlds in 24 entries.** The 24 entries of `fixtures/manifests/heldout_manifest.json` (`heldout-v1`),
  2 worlds each. Entry i (0-based, manifest order) takes seeds 13101 + 2i and 13102 + 2i. The variants were written
  blind to both systems (Agent H) and are new to every development set. Every world has one faulting and one innocent
  deployment.
- **Controls: 10 worlds,** seeds 13149–13158. The first two TEST parameter sets of D2, D3, D4, D5 and D6 in
  `fixtures/manifests/evaluation_manifest.json` (`eval-v2`), in family order, then manifest order.
- **Input checks, by the build only.** Each world opens exactly one incident, and each held-out packet lists its
  faulting and its innocent deployment among `candidate_change_ids`, so a correct and a wrong answer both exist. No
  system's answer is computed by the build.

| Entry | Seeds | Fault (class) | Innocent | Innocent position | Onset | Categories |
|---|---|---|---|---|---|---|
| heldout_01 | 13101, 13102 | payload@9 (same_file_raises) | orders@3 | gap | delayed | off |
| heldout_02 | 13103, 13104 | payload@7 (upstream_value_rejected) | orders@3 | gap | delayed | on |
| heldout_03 | 13105, 13106 | config@8 (configuration_only) | handler@4 | before | prompt | on |
| heldout_04 | 13107, 13108 | orders@4 (refactor_changes_behaviour) | handler@3 | after | prompt | off |
| heldout_05 | 13109, 13110 | payload@8 (cross_module_contract) | handler@4 | gap | delayed | off |
| heldout_06 | 13111, 13112 | orders@6 (unit_mismatch_partial) | handler@4 | after | delayed | on |
| heldout_07 | 13113, 13114 | payload@9 (same_file_raises) | handler@3 | gap | delayed | off |
| heldout_08 | 13115, 13116 | payload@7 (upstream_value_rejected) | handler@3 | before | prompt | on |
| heldout_09 | 13117, 13118 | config@8 (configuration_only) | orders@3 | after | prompt | off |
| heldout_10 | 13119, 13120 | orders@4 (refactor_changes_behaviour) | config@7 | gap | delayed | on |
| heldout_11 | 13121, 13122 | payload@8 (cross_module_contract) | config@7 | before | delayed | off |
| heldout_12 | 13123, 13124 | orders@6 (unit_mismatch_partial) | checkout@4 | before | prompt | off |
| heldout_13 | 13125, 13126 | payload@9 (same_file_raises) | orders@5 | before | prompt | on |
| heldout_14 | 13127, 13128 | payload@7 (upstream_value_rejected) | checkout@4 | after | prompt | off |
| heldout_15 | 13129, 13130 | config@8 (configuration_only) | checkout@4 | gap | delayed | on |
| heldout_16 | 13131, 13132 | orders@4 (refactor_changes_behaviour) | checkout@4 | after | delayed | on |
| heldout_17 | 13133, 13134 | payload@8 (cross_module_contract) | orders@3 | before | prompt | on |
| heldout_18 | 13135, 13136 | orders@6 (unit_mismatch_partial) | config@7 | after | prompt | off |
| heldout_19 | 13137, 13138 | payload@9 (same_file_raises) | config@7 | after | prompt | on |
| heldout_20 | 13139, 13140 | payload@7 (upstream_value_rejected) | orders@5 | gap | delayed | off |
| heldout_21 | 13141, 13142 | config@8 (configuration_only) | orders@5 | before | delayed | off |
| heldout_22 | 13143, 13144 | orders@4 (refactor_changes_behaviour) | handler@4 | before | prompt | off |
| heldout_23 | 13145, 13146 | payload@8 (cross_module_contract) | orders@5 | after | prompt | on |
| heldout_24 | 13147, 13148 | orders@6 (unit_mismatch_partial) | handler@3 | gap | delayed | on |

| Control world | Seed | Parameter set (manifest run, frozen seed) | Variant | Parameters |
|---|---|---|---|---|
| control_d2_test_1_13149 | 13149 | d2_test_1 (2011) | moderate_method_drop | method=upi, magnitude=moderate |
| control_d2_test_2_13150 | 13150 | d2_test_2 (2012) | severe_method_drop | method=card, magnitude=severe |
| control_d3_test_1_13151 | 13151 | d3_test_1 (2021) | receiver_code | fault=receiver@2, harmless_position=none |
| control_d3_test_2_13152 | 13152 | d3_test_2 (2022) | receiver_config | fault=config@3, harmless=notifications@2, harmless_position=before |
| control_d4_test_1_13153 | 13153 | d4_test_1 (2031) | late_authorization | mode=late_authorization |
| control_d4_test_2_13154 | 13154 | d4_test_2 (2032) | stale_snapshot | mode=stale_snapshot |
| control_d5_test_1_13155 | 13155 | d5_test_1 (2041) | mix_shift | mode=mix_shift |
| control_d5_test_2_13156 | 13156 | d5_test_2 (2042) | sparse_cohort | mode=sparse_cohort |
| control_d6_test_1_13157 | 13157 | d6_test_1 (2051) | innocent_deploy_near_method_drop | base=D2, method=netbanking, magnitude=severe, innocent=notifications@2 |
| control_d6_test_2_13158 | 13158 | d6_test_2 (2052) | innocent_deploy_near_mix_shift | base=D5, innocent=checkout@3 |

**Run order.** The held-out worlds in seed order (13101 to 13148), then the controls (13149 to 13158). A held-out
world's ID is `<entry>_<seed>`, a control's `control_<manifest run>_<seed>`.

## Systems

| System | What answers a world | Answers per world |
|---|---|---|
| **R** | `rules_v3(packet)`: rules-v2's hypothesis and operation with attribution-v1's faulting change, contradictions resolved to `unknown`, checked by val-6 (`selective.answer_rules_v3`) | one, deterministic; counted once per model seed |
| **S** | `route(packet)` first. A MODEL-routed world gets the live local diagnosis through `selective.diagnose_selective` (one call, at most one repair, val-6, `needs_review` when refused twice, no fallback to rules-v3). A RULES-routed world gets rules-v3 | one per model seed |
| **R2** (secondary) | `rules.diagnose(packet)` (rules-v2). An answer val-6 refuses is no answer: the product's rules mode refuses to store it | one, deterministic |
| **M-all** (descriptive) | the live local diagnosis (`diagnosis.diagnose`, same settings) on every held-out world | one per model seed |
| **S-agree** (descriptive) | the agreement variant, computed from S's answers: rules-v3 on the RULES route; on the MODEL route the model's answer only when it names rules-v3's deployment, review otherwise | one per model seed |

S's answer on a RULES-routed world is rules-v3's, so S and R give the same answer on every RULES-routed world by
construction. They can differ only on MODEL-routed worlds.

**The code under test** is a clean `git archive` export of commit `43a160f`
(`43a160f2ad5e59562e0b5c896139abe546ed5cd4`), named by the orchestrator: prompt `diag-p5`, committed on the router's
commit `efd1b16`. Every stage imports `recovery_workbench` from that export, and refuses unless it reports:

| Version | Value |
|---|---|
| prompt | `diag-p5` |
| validator | `val-6` |
| router | `router-v1` |
| rules-v3 | `rules-v3` |
| comparator | `attribution-v1` |
| rules-v2 | `rules-v2` |
| packet | `pkt-2` |

It also refuses unless these files are byte-identical to the frozen ones (the router and rules-v3 as committed in
`efd1b16`; the comparator and rules-v2 unchanged since they were frozen), and unless the export's package sources
hash (round 2's `package_sources_sha256`, over the package's 42 `.py` files) is `43a160f`'s. G checked all four on
an export of `43a160f`; `diagnosis.py` is listed for reference, and the package hash covers it.

| File | SHA-256 |
|---|---|
| `selective.py` | `a0efc21bc0c5fe25a3c632ba9cf596d6a60ad618bf385baa96ceeba1a583b619` |
| `attribution_rules.py` | `a365f58abf9f9b3858efc42df58b5b981efc45efd2ff25d3cbf8d416affbf809` |
| `rules.py` | `3b815b34ec37e87d23e8c44e895cf4a8e41ab55494f934450ef2f65b0419fc7a` |
| `diagnosis.py` (`diag-p5`, `val-6`) | `85bbebb58a851b6792f5c0bf098209cab67eb505c28496d2a12bf59aedd2f69b` |
| package sources of `43a160f` | `d81eeb26d56adadebdda707e5d08c1d92bab63c650d5e2d106fe0c018eba4d92` |

**Model settings.** The frozen M4 settings (`evaluation/frozen_settings.json`): `qwen3:8b`, digest
`500a1f067a9f…`, think off, temperature 0.6, top_p 0.95, top_k 20, min_p 0, num_ctx 16384, num_predict 6144, a 180 s
deadline, at most one repair, policies enforced. Only the prompt (`diag-p5`), the validator (`val-6`) and the model
seed differ from that file. Any other drift stops the stage, and no model is ever pulled. Ollama listens on 127.0.0.1
only, and is started for the stage and stopped after it.

## Arms

Only the model is run in arms. R, R2 and S-agree need none: the summariser computes them from the packets and S's
answers.

| Arm | System | Model seed | Worlds, in run order | Entry point |
|---|---|---:|---|---|
| `selective_20261111` | S's model calls | 20261111 | every MODEL-routed world, held-out or control | `selective.diagnose_selective` |
| `selective_20261112` | S's model calls | 20261112 | the same | `selective.diagnose_selective` |
| `model_all_20261111` | M-all | 20261111 | the 48 held-out worlds | `diagnosis.diagnose` |
| `model_all_20261112` | M-all | 20261112 | the 48 held-out worlds | `diagnosis.diagnose` |

They run in this order, and each starts cold (the model is unloaded first).

**Checked by the arm stage before its first model call** (`diagnose_arm.py`):

- the pre-registration is the file the build hashed, and is no longer a draft;
- every `ADDENDUM_*.md` is recorded in `addenda.json` with its SHA-256;
- the package was imported from `43a160f`'s export, reports the versions above, holds the frozen files, and has the
  pinned package sources hash;
- every packet has its build-time hash;
- the frozen settings, with the prompt, validator and seed substituted, match the code and the model digest.

**What each arm writes, never overwriting:** `answers_<arm>.json` and `traces/<arm>.jsonl`.

- **The answers file records** the code under test (its export path, versions, frozen-file hashes and package sources
  hash), the pre-registration, the addenda, the frozen settings file's SHA-256, `cases.json`'s SHA-256, the route of
  every one of the 58 worlds as the code decided it (target, reason, unmet criteria, measurements), the worlds the arm
  diagnosed, and each one's rendered prompt SHA-256.
- **Each answer** holds the status, the final output, the reasons, blocked proposals, repairs, latency and every call's
  reply. An S answer also holds the route and the rules-v3 answer `diagnose_selective` computed beside it.
- **Each run gets round 3's run record:** the model, its digest, the full generation options with the seed, thinking,
  and the SHA-256 of the first request (messages plus response schema). The request hash is kept only when the trace
  shows that request was the first one sent.
- **The trace** keeps every call's messages.

## Definitions

- **Expected deployment:** a world's `expected.faulting_change_id` in `cases.json`, the oracle's faulting deployment.
  It is `none` for the D2, D4, D5 and D6 controls, where no deployment caused the problem.
- **Final answer:** for R and R2, the system's answer (R2's is none when val-6 refuses it). For a model run, its
  validated output; none when it ended `needs_review`.
- **Names the expected deployment:** the final answer's `faulting_change_id` equals the expected deployment's ID.
  Exact match only.
- **Wrong attribution:** a final answer whose `faulting_change_id` is a deployment other than the expected one.
  `unknown` and `none` are abstentions, not wrong (except that `none` is the right answer where the expected
  deployment is `none`). No final answer (`needs_review`) is not wrong either.
- **Attribution classes,** reported separately: correct, wrong, abstained (`unknown`/`none`), review (no final answer).
- **Operational failure** (round 3): a run that ended `needs_review` because of a timeout, a model or Ollama error,
  the overall deadline, no call at all, or a reply still unparsable after the repair. It is class review like any
  `needs_review`, is listed separately, and is never dropped.
- **S's final answer on world w at seed s:** rules-v3's when w routes to RULES. When w routes to MODEL, the
  `selective_<s>` arm's final answer on w.

## Validity checks

| # | Check | Enforced by |
|---|---|---|
| 1 | **Pre-registration:** the file is the one the build hashed and is final; every addendum is recorded with its SHA-256; each arm ran under this pre-registration, under no addendum since changed, and with the frozen settings file as it is now | the stages refuse (1a); recorded per arm (1b) |
| 2 | **Packets:** every packet file has its build-time hash | the stages refuse |
| 3 | **Code under test:** the stage imported the package from `43a160f`'s export, with the versions, frozen files and package sources hash above; each arm's recorded code is the summariser's own | the stages refuse (3a); recorded per arm (3b) |
| 4 | **Routes:** each arm recorded, for all 58 worlds, the route the summariser computes. S's arms diagnosed exactly the MODEL-routed worlds, in run order, each on the MODEL route with the summariser's rules-v3 answer beside it. M-all's arms diagnosed exactly the 48 held-out worlds | per arm |
| 5 | **Run records:** every answer has the frozen model and digest, the frozen options with the arm's seed, thinking off; every run that made a call has the SHA-256 of a first request sent as built; the arm's environment records the frozen digest | per arm |
| 6 | **Operational failures:** at most 2 per S arm, each counted as review and never dropped | per arm |
| 7 | **Controls:** at most 1 of the 10 control worlds routes to MODEL. Any MODEL route there is reported | once |
| 8 | **No database:** the summariser reads JSON files and packets only. An audit hook records every SQLite database its thread opens, and it writes no report if one was opened | the summariser refuses |

**What each check binds.**

- **Decision 1:** checks 1–3, 7 and 8, and checks 1b and 3b–6 on both `selective_*` arms.
- **Decision 2:** checks 1–3 and 8 (it reads no arm).
- **M-all's figures:** reported only when checks 1b, 3b and 5 pass on both `model_all_*` arms. Checks 4 and 6 on those
  arms are reported and descriptive, because no decision reads an M-all answer.

Check 4 is G's addition to the design. S is defined by its routes, so a route that differed between the arm and the
summary would leave S's answers undefined.

## Decision 1: S against R (primary)

- **Unit: the held-out manifest entry (24).** Each entry has 2 worlds × 2 model seeds = 4 final answers per system.
- **Statistic per entry:** c(e) = how many of its 4 final answers name the expected deployment (0–4). For R, each
  world's one answer counts at both seeds, so c_R(e) is 0, 2 or 4 and the scales match.
- **Per entry:** improved if c_S(e) > c_R(e); worsened if c_S(e) < c_R(e); tied otherwise.
- **Test:** an exact one-sided sign test on the untied entries, computed with exact fractions. With n = improved +
  worsened and X ~ Binomial(n, 1/2):
  - improved-direction p = P(X >= improved);
  - worsened-direction p = P(X >= worsened);
  - both are 1 when n = 0.
- **Wrong attributions, over all 58 worlds** (held-out and control) **and both seeds:**
  - W_S = the number of S's final answers that name a deployment other than the expected one;
  - W_R = twice the number of worlds where rules-v3 does.

  A RULES-routed control counts the same for both. A MODEL-routed control's model answer counts for S alone.
- **Verdict: the first rule that matches.**
  1. **invalid:** a check that binds decision 1 fails;
  2. **hurts:** W_S > W_R, or the worsened-direction p <= 0.05. Any excess counts: there is no noise allowance,
     because shipping the model route is a safety decision;
  3. **helps:** the improved-direction p <= 0.05 and W_S <= W_R;
  4. **otherwise:** "no detectable advantage in this sample". This wording is used throughout; nothing is called "no
     advantage".
- **Reported with the verdict:**
  - which clauses matched;
  - improved, worsened and tied entries, and both p-values as exact fractions;
  - every entry's counts;
  - each system's correct attributions out of 96 held-out answers;
  - wrong attributions out of 116, split held-out/control;
  - the differences, and the net entries (improved − worsened);
  - S's abstentions and reviews;
  - the routing rate.
- **The per-world comparison is descriptive only.** It is the same comparison with the world as the unit (0–2 per
  world) and its own sign test. It decides nothing, because an entry's two worlds are not independent.
- **The unsafe-proposals clause of the claim is met by construction for final answers.** Policies are enforced, so
  val-6 refuses an unsafe proposal and the run repairs it or ends in review. Runs with a policy-blocked proposal are
  reported per arm as a cost.
- **Where it can move.** S equals R on every RULES-routed world, so only an entry with at least one MODEL-routed world
  can be untied. If no held-out world routes to MODEL, S is R on the held-out worlds, n = 0, and the verdict can only
  be "no detectable advantage in this sample", or "hurts" through a MODEL-routed control. The report then leads with
  the routing rate.

## Decision 2: rules-v3 against rules-v2 (secondary), on the 48 held-out worlds

Deterministic, with no p-value. Each world counts once for each system.

- **Adopt rules-v3 as the rules-first default** if rules-v3 names the expected deployment in at least as many worlds as
  rules-v2, and names a wrong deployment in no more worlds than rules-v2.
- **Otherwise keep rules-v2.**
- **Invalid** if a check that binds it fails; rules-v2 then stays, reported as unmeasured.

Reported: each system's correct, wrong, abstained and review counts; the worlds where the two name different
deployments; and any rules-v2 answer val-6 refuses (expected: none).

## Controls

- **Validity (check 7).** The router should send none of the 10 controls to MODEL. Each MODEL route there is reported,
  and decision 1 is invalid if more than 1 of the 10 does.
- **In decision 1.** Every control enters W_S and W_R.
- **Descriptive.** For each control:
  - its route and reason;
  - R's hypothesis, change and operation;
  - S's and S-agree's answers at both seeds;
  - the expected answer.

  S and R agree on every RULES-routed control by construction.

## Adoption rules

Only decisions 1 and 2 enter them; no descriptive figure does.

| Outcome | What ships |
|---|---|
| decision 1 = helps | A product mode is wired: the orchestrator adds a `ModelMode`, and B wires `diagnosis_store`. The README states the measured advantage |
| decision 1 = anything else | The product stays rules-first, and the model is labelled experimental assistance with its measured limits |
| decision 2 = adopt rules-v3 | rules-v3 replaces rules-v2 as the rules default; rules-v2 is kept for reproducing M4 |
| decision 2 = keep rules-v2, or invalid | rules-v2 stays the rules default |

## Descriptive figures (no verdict depends on them)

- **Routing (the invocation rate):** MODEL-routed held-out worlds of 48, entries with a MODEL-routed world, and
  controls of 10, by onset, innocent position, fault and innocent variant; every route reason.
- **Correctness where the model was invoked:** on the MODEL-routed held-out worlds, each system's correct, wrong,
  abstained and review answers. The same on the RULES-routed worlds, where M-all shows what the router left uncalled.
- **Overall outcome:** M4's `outcome_correct` (`evaluation.run.score`) for every system's final answers.
- **The per-world comparison of S against R,** with its sign test (see decision 1).
- **The agreement variant (S-agree), as pre-registered by the orchestrator. It decides nothing, and no adoption rule
  refers to it.**
  - **The rule.** On a MODEL-routed world, at each seed, the S arm's final answer stands only when its
    `faulting_change_id` equals rules-v3's. Otherwise the answer is review, including when the model run itself ended
    `needs_review`. A RULES-routed world takes rules-v3's answer.
  - **Reported:** correct, wrong, abstained and review counts on the held-out worlds (of 96) and on the MODEL-routed
    ones; how the MODEL-route answers fell (accepted, deployments differ, model review); wrong attributions over all
    58 worlds; and its by-entry comparison with R.
  - **What it can show.** By construction S-agree names a deployment only where rules-v3 names the same one. So it can
    never be right where rules-v3 is not. Its figures show how often the model confirms rules-v3, and what sending the
    rest to review would cost.
- **M-all against S and against R,** by entry and by world: improved/worsened/tied, both sign-test p-values and wrong
  attributions. These show whether the router's selection is better or worse than calling the model everywhere.
- **Strata:** by fault variant, innocent variant, onset and position: worlds, MODEL routes and correct answers per
  system.
- **Noise:**
  - worlds where a model system's two seeds disagree;
  - at each seed and on each MODEL-routed world, S's arm against M-all's: the same prompt, answered in a different
    sequence. Reported: identical first requests (expected: all), identical first replies and identical final
    changes.
- **Operational, per arm:** the `needs_review` rate, repair calls, runs with a repair, model calls, operational
  failures by kind, runs with a policy-blocked proposal, and latency (cold, warm median, p90, max).
- **rules-v3's resolutions** on the held-out worlds (as attributed, or one of its contradiction rules).

## Known limits

- **attribution-v1's disclosed failure modes, by mechanism only.** After it was frozen it missed 3 of 10 new
  variants, by two mechanisms:
  - an extract-variable refactor that rewrites the failing line;
  - an innocent change interacting with the fault: an innocent change's new line became the failing line.

  It was frozen as is, not tuned on them. Whether a held-out variant has either mechanism was never checked: the
  variants' writer did not know them, and no system has been run on a held-out world.
- **One file per deployment,** as the storage schema enforces.
- **Written blind.** The held-out variants were written blind to both systems. They neither target nor avoid either
  system's weak spots.
- **Small, and clustered even by entry.**
  - The entry is the unit because an entry's two worlds share their variants, position and onset.
  - Entries are not fully independent either. The 24 entries reuse 6 fault variants (4 entries each) and 6 innocent
    variants (4 each), so variant-level dependence remains.
  - "No detectable advantage" is not "no advantage".
- **The router inherits attribution-v1's candidate relevance.** An innocent deployment that looks unrelated is not a
  plausible candidate, so a case it would make ambiguous can stay on the rules route (Agent E).
- **Only worker faults are held out.** Receiver attribution (D3) appears only in the two controls.
- **Attribution only.** The held-out faults have no allowlisted corrected variant, so recovery is not exercised.
- **Two model seeds at temperature 0.6** sample the model's variability; they do not remove it.

## Expectations, not part of any rule

These come from development data and from the design itself. No test packet has been answered.

1. **S can lose where rules-v3 is right.** On development data the model under the shipped pair (`diag-p4` +
   `val-6`) named the right deployment in 4 of the 6 MODEL-routed round-3 cases at each seed, where rules-v3 named it in
   6 of 6 (Agent E §4.3). If held-out MODEL-routed worlds behave alike, S loses correct answers wherever rules-v3 is
   right. Each model answer that names the innocent deployment also adds to W_S. So "hurts" is at least as likely as
   "no detectable advantage". `diag-p5` has not been measured here.
2. **Where the router can send a world.**
   - With delayed onset, both deployments precede the first failure.
   - With prompt onset, an innocent deployed "before" precedes it. An innocent deployed "after" lands about 4 minutes
     after the fault, so whether it precedes the first failure depends on traffic.
   - Whether a preceding deployment counts as plausible depends on attribution-v1's relevance judgement, which nobody
     has computed on these worlds.
3. **Decision 2 favours rules-v3.** On development data rules-v3 named 97 right and 0 wrong against rules-v2's 75 and
   22 (Agent E, 175 stored packets). On A2's 10 post-freeze variants, attribution-v1 scored 7/3/0 against rules-v2's
   1/9/0. These are development figures, not this test.
4. **The sign test has little power with 24 units.** It cannot reach p <= 0.05 with fewer than 5 untied entries. The
   smallest count with p <= 0.05 for n untied entries:

   | n | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 | 15 | 16 | 17 | 18 | 19 | 20 | 21 | 22 | 23 | 24 |
   |---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
   | k | 5 | 6 | 7 | 7 | 8 | 9 | 9 | 10 | 10 | 11 | 12 | 12 | 13 | 13 | 14 | 15 | 15 | 16 | 16 | 17 |

   - 5 improved entries out of 5 untied give p = 1/32.
   - 6 out of 7 give p = 1/16, which is not significant.
   - "helps" therefore needs at least 5 entries in which attribution-v1 misses, the case routes, and the model does
     better, with almost no entry going the other way.

## Procedure

**Part 1, done without a model call and without running any system on a test packet:**

- the design (`selective_design.py`), the draft, the build, the arm stage, the summariser, and
  `tests/test_selective_experiment.py`;
- the orchestrator named `43a160f`; G exported it, checked that it holds the frozen files and reports every version
  above, and pinned its package sources hash in `selective_design.py` and here;
- the tests drive the summariser with hand-made answer sets and stand-in routes, never with a real answer on a test
  packet.

**Part 2 so far, still without any system run on a test packet:**

- the orchestrator settled the draft's open points (above), and this file was finalised;
- the build was redone in the test container, from `43a160f` plus this folder, into an empty directory. Its 58 packets
  are byte-identical to part 1's, and `cases.json` records this file's SHA-256 with `preregistration_draft: false`.

**Next, in order:**

1. The orchestrator commits this file and the build. No route, rules system or model runs on a test packet before
   that commit.
2. The four arms, in the order above, each from `43a160f`'s export.
3. `summarize.py` writes `report.json`, then the adoption rules apply.

**Rules for the run** (as in round 3):

- Every live answer is kept.
- No world is dropped, re-run or re-ordered after a result is seen, and no setting is tuned.
- A failed run is never dropped: it is class review, and is listed as an operational failure.
- If an arm stops part-way, its partial trace is kept under a dated name, and both runs are reported. The arm is then
  re-run in full, after a dated addendum.
- Any re-run needs a dated addendum first.
