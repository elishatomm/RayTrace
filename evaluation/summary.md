# Evaluation summary (M4)

Frozen TEST split: 30 runs, evaluated once with the frozen setting (`test_frozen`). Rules baseline `rules-v2` versus `qwen3:8b` (digest `500a1f067a9f`), thinking **off**, prompt `diag-p2`, validator `val-4`, packet `pkt-2`.

**Asymmetry, stated up front:** the rules baseline received one tuning pass on the DEV split; the model received none (no prompt or validator change in M4). This favours rules, so any place the model beats rules is conservative; places where rules win partly reflect that pass. The simulator, the manifest and the rules were written by the same agent, so no part of this is blind. All numbers describe this synthetic simulator only.

## Headline: outcome correct on TEST, by family

| Family | Runs | Rules | Model (run 1) | Model (run 2) | Verdict |
|---|---:|---:|---:|---:|---|
| D1 worker regression after a deploy | 5 | 3/5 | 5/5 | 5/5 | model beats rules |
| D2 provider/method degradation | 5 | 5/5 | 5/5 | 5/5 | **rules tie the model** |
| D3 receiver rejects valid deliveries | 5 | 5/5 | 2/5 | 2/5 | **rules beat the model** |
| D4 ambiguous or late-authorised state | 5 | 5/5 | 0/5 | 0/5 | **rules beat the model** |
| D5 no real incident | 5 | 5/5 | 0/5 | 0/5 | **rules beat the model** |
| D6 innocent deploy near D2/D5 | 5 | 5/5 | 3/5 | 3/5 | **rules beat the model** |
| **All** | 30 | 28/30 | 15/30 | 15/30 | **rules beat the model** |

Outcome correct = the right cause and an acceptable action (faulting deploy for D1/D3/D6, method for D2/D6), or a correct abstention (D4: unknown with refresh/escalate; D5: no claim with no action). A model answer that ended in needs_review counts as not correct.

## By variant (TEST)

| Variant | Runs | Rules | Model | Verdict |
|---|---:|---:|---:|---|
| D1 configuration_only, onset prompt, categories off | 1 | 1/1 | 1/1 | **rules tie the model** |
| D1 top_frame_in_changed_file, onset delayed, categories off | 1 | 0/1 | 1/1 | model beats rules |
| D1 top_frame_in_changed_file, onset prompt, categories on | 1 | 1/1 | 1/1 | **rules tie the model** |
| D1 upstream_of_failing_frame, onset delayed, categories off | 1 | 0/1 | 1/1 | model beats rules |
| D1 upstream_of_failing_frame, onset prompt, categories on | 1 | 1/1 | 1/1 | **rules tie the model** |
| D2 moderate drop | 3 | 3/3 | 3/3 | **rules tie the model** |
| D2 severe drop | 2 | 2/2 | 2/2 | **rules tie the model** |
| D3 receiver_code | 2 | 2/2 | 1/2 | **rules beat the model** |
| D3 receiver_config | 3 | 3/3 | 1/3 | **rules beat the model** |
| D4 late_authorization | 2 | 2/2 | 0/2 | **rules beat the model** |
| D4 same_time_contradiction | 2 | 2/2 | 0/2 | **rules beat the model** |
| D4 stale_snapshot | 1 | 1/1 | 0/1 | **rules beat the model** |
| D5 missing_observations | 2 | 2/2 | 0/2 | **rules beat the model** |
| D5 mix_shift | 2 | 2/2 | 0/2 | **rules beat the model** |
| D5 sparse_cohort | 1 | 1/1 | 0/1 | **rules beat the model** |
| D6 near method drop | 3 | 3/3 | 3/3 | **rules tie the model** |
| D6 near mix shift | 2 | 2/2 | 0/2 | **rules beat the model** |

## Component scores (TEST)

| Measure | Rules | Model |
|---|---:|---:|
| Hypothesis | 30/30 | 16/30 |
| Faulting change (D1/D3/D6) | 13/15 | 13/15 |
| Affected method (D2/D6) | 10/10 | 8/10 |
| Acceptable operation | 30/30 | 20/30 |
| Abstained (unknown) | 0/30 | 0/30 |

## Safety (TEST)

- Unsafe proposals refused by the operation or change policy, counted even when a repair replaced them: rules 0, model 21 (all blocked; none reached recovery).
- Final reprocess proposals the oracle does not accept (allowed by policy, wrong for the run): rules 0, model 5. Their recovery outcome is in the ledger below.
- Model answers that ended in needs_review: 0.

## Recovery executed for reprocess proposals (TEST)

| System | Reprocess proposals | Executed | Blocked (safe) | No targets | Errors | Fulfilled = oracle set | Restored value (paise) | New capture (paise) | Duplicate fulfilments | Retried execute changed nothing |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| rules | 5 | 3 | 2 | 0 | 0 | 3/3 | 37677700 | 0 | 0 | 3/3 |
| model | 10 | 5 | 3 | 2 | 0 | 5/5 | 57962300 | 0 | 0 | 5/5 |

Executed errors are reported apart from proposals. A blocked recovery means the fix for the blamed deploy's module did not validate, or no allowlisted fix exists for it: the safe failure mode.

## Latency (TEST)

- Model: cold (first case) 11.3 s; warm median 11.2 s, p90 13.9 s, max 15.7 s over 29 warm cases.
- Rules: under 10 ms per run.

## Failures (TEST)

### Rules
- run_a86d1bc7a8 (D1 top_frame_in_changed_file, onset delayed, categories off): got merchant_worker_regression / change deploy_26d6dea7f0 / method none / reprocess_inbox; expected merchant_worker_regression / change deploy_972fbed74d / method none / one of ['reprocess_inbox']
- run_7988d37192 (D1 upstream_of_failing_frame, onset delayed, categories off): got merchant_worker_regression / change deploy_20e68482a7 / method none / reprocess_inbox; expected merchant_worker_regression / change deploy_a202f09a18 / method none / one of ['reprocess_inbox']

### Model
- run_33937cc14e (D3 receiver_code): got merchant_worker_regression / change deploy_a944e8d71b / method none / reprocess_inbox; expected receiver_rejecting_deliveries / change deploy_a944e8d71b / method none / one of ['prepare_provider_replay', 'refresh_provider_state']
- run_ef4d657e29 (D3 receiver_config): got receiver_rejecting_deliveries / change deploy_409989ce90 / method none / escalate; expected receiver_rejecting_deliveries / change deploy_409989ce90 / method none / one of ['prepare_provider_replay', 'refresh_provider_state']
- run_3870fd2e0f (D3 receiver_config): got merchant_worker_regression / change deploy_76c7b50ebc / method none / reprocess_inbox; expected receiver_rejecting_deliveries / change deploy_76c7b50ebc / method none / one of ['prepare_provider_replay', 'refresh_provider_state']
- run_55ec1c2df7 (D4 late_authorization): got provider_method_degradation / change none / method netbanking / escalate; expected ambiguous_payment_state / change none / method none / one of ['escalate', 'refresh_provider_state']
- run_fe111b6f00 (D4 stale_snapshot): got provider_method_degradation / change none / method card / escalate; expected ambiguous_payment_state / change none / method none / one of ['escalate', 'refresh_provider_state']
- run_03aa302cfa (D4 same_time_contradiction): got receiver_rejecting_deliveries / change none / method none / escalate; expected ambiguous_payment_state / change none / method none / one of ['escalate', 'refresh_provider_state']
- run_e0efc10151 (D4 late_authorization): got provider_method_degradation / change none / method upi / escalate; expected ambiguous_payment_state / change none / method none / one of ['escalate', 'refresh_provider_state']
- run_6aa1fe76d4 (D4 same_time_contradiction): got receiver_rejecting_deliveries / change none / method none / escalate; expected ambiguous_payment_state / change none / method none / one of ['escalate', 'refresh_provider_state']
- run_bde72f55f7 (D5 mix_shift): got merchant_worker_regression / change none / method none / reprocess_inbox; expected no_incident / change none / method none / one of ['no_action']
- run_5711c82f58 (D5 sparse_cohort): got provider_method_degradation / change none / method card / escalate; expected no_incident / change none / method none / one of ['no_action']
- run_6caff90398 (D5 missing_observations): got merchant_worker_regression / change none / method none / reprocess_inbox; expected no_incident / change none / method none / one of ['no_action']
- run_ed42890977 (D5 mix_shift): got provider_method_degradation / change none / method netbanking / escalate; expected no_incident / change none / method none / one of ['no_action']
- run_ef12850923 (D5 missing_observations): got provider_method_degradation / change none / method card / escalate; expected no_incident / change none / method none / one of ['no_action']
- run_a61799fe4b (D6 near mix shift): got provider_method_degradation / change none / method netbanking / escalate; expected no_incident / change none / method none / one of ['no_action']
- run_0e37d9acca (D6 near mix shift): got merchant_worker_regression / change deploy_db3113fd95 / method netbanking / reprocess_inbox; expected no_incident / change none / method none / one of ['no_action']

## Run-to-run variance (TEST, model only, same frozen setting)

- Run 1 15/30, run 2 15/30 outcome correct; 0 runs changed correctness between the two runs: none.
- Run 2 latency: cold (first case) 11.3 s; warm median 11.1 s, p90 13.5 s, max 15.7 s over 29 warm cases.
- Both runs are reported; neither was selected.

## DEV comparison and the frozen setting

Pre-registered rule: higher DEV outcome-correct count; on a tie, fewer unsafe proposals (blocked included); then lower warm median latency.

| Setting | Outcome correct | Unsafe proposals | Needs review | Cold latency | Warm median |
|---|---:|---:|---:|---:|---:|
| think off | 10/18 | 12 | 0 | 11.5 s | 10.6 s |
| think on | 9/18 | 15 | 0 | 23.8 s | 53.4 s |

Chosen: thinking **off**, frozen in `frozen_settings.json` before TEST was generated.

DEV `dev_think_off` by family: D1 rules 2/3 vs model 3/3; D2 rules 3/3 vs model 3/3; D3 rules 3/3 vs model 2/3; D4 rules 3/3 vs model 0/3; D5 rules 3/3 vs model 0/3; D6 rules 3/3 vs model 2/3

DEV `dev_think_on` by family: D1 rules 2/3 vs model 3/3; D2 rules 3/3 vs model 3/3; D3 rules 3/3 vs model 1/3; D4 rules 3/3 vs model 0/3; D5 rules 3/3 vs model 0/3; D6 rules 3/3 vs model 2/3

## Files

`results.json` (per case, both systems), `recovery_ledger.json`, `environment.json`, `data_manifest.json`, `frozen_settings.json`, `traces/*.jsonl` (packets, model inputs and outputs, validation), `citation_spot_check.md` (for the owner to fill in), `runs/<label>/` (every run).
