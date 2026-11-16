# Recovery receipt `receipt-2`

Receipt hash `eb9d6f30f04d9ebc6694a7f153cc60d4465459c60ba07b6f698b7a3304c6afa6`: SHA-256 over the canonical JSON of the sections `receipt_version`, `run_id`, `versions`, `evidence`, `proposal`, `approved_operation`, `preconditions`, `execution`, `postconditions`, `value`, `boundary`. Database-local numbers are taken out first. Each inbox row number in the hashed sections (an `inbox_id`, each number in `superseded_inbox_ids`, the N of an `obs_inbox_N` observation ID or of `inbox row N` in a check's details) becomes its rank among them in ascending order, from 1; each target's observations are then sorted by their canonical JSON; and `plan_hash`, `planned_evidence_version` and `evidence_version_at_execution`, computed from those numbers, are left out. Identity and wall-clock times are not hashed.

> Synthetic sandbox: no real money, customers or provider accounts. The workbench called no provider; reprocessing fulfils orders from captures it had already received and captures nothing.

## Identity (not hashed)

| Record | ID |
|---|---|
| run id | `run_53966a08d4` |
| incident id | `inc_0001` |
| diagnosis id | `diag_84ad7ba40d3b47b7` |
| fix validation id | `fixval_36a2945bf6d049d6` |
| plan id | `plan_54aa03ad51bf4e2e` |
| approval id | `appr_8647cec015494c2c` |
| action id | `act_e2da112cf9ea8a83a486` |
| operation key | `reprocess_inbox:run_53966a08d4:plan_54aa03ad51bf4e2e:9db6c8c60bee4995eaed5927d37f339485ca3d204b4ffcac9b135286f2acc12e` |
| recorded from | none |

## Versions

- application: `0.1.0`
- simulator generator: `world-v3`
- policy: `policy-v3`
- prompt: not used
- model: not used
- model digest: not used
- thinking: not used
- validator: `val-7`
- rules: `rules-v3`

## Evidence

- Diagnosis packet hash: `8b47f5c942e67560ab7d1fcec07e039cbe1bd1c25e0be9ac35a89fb65689c924`
- Evidence version the plan hash binds: `c828da99cdb23feaa71c2d3c0303dc9937a0933e16478c0d9fd2dbde4c0cc9f0`
- Evidence version at execution: `c828da99cdb23feaa71c2d3c0303dc9937a0933e16478c0d9fd2dbde4c0cc9f0` (unchanged)
- Provider state counts as fresh for 3600000 ms (60 min)
- Snapshot IDs used: 279; each, with its known-at time, is listed per target in the JSON receipt

| Order | Observations | Snapshots | Newest known at | Freshness at execution |
|---|---|---|---|---|
| `order_SYNTHETIC_547a954d_0049` | 7 | 5 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0050` | 7 | 5 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0051` | 7 | 5 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0052` | 7 | 5 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0053` | 7 | 5 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0054` | 7 | 5 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0055` | 7 | 5 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0056` | 7 | 5 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0057` | 7 | 5 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0058` | 7 | 5 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0059` | 7 | 5 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0060` | 10 | 7 | 2026-11-05 12:33:00Z (1793881980000 ms) | 180000 ms (3 min) |
| `order_SYNTHETIC_547a954d_0061` | 7 | 5 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0062` | 7 | 5 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0063` | 6 | 4 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0065` | 9 | 6 | 2026-11-05 12:33:00Z (1793881980000 ms) | 180000 ms (3 min) |
| `order_SYNTHETIC_547a954d_0066` | 6 | 4 | 2026-11-05 12:10:00Z (1793880600000 ms) | 1560000 ms (26 min) |
| `order_SYNTHETIC_547a954d_0067` | 6 | 4 | 2026-11-05 12:10:00Z (1793880600000 ms) | 1560000 ms (26 min) |
| `order_SYNTHETIC_547a954d_0068` | 6 | 4 | 2026-11-05 12:10:00Z (1793880600000 ms) | 1560000 ms (26 min) |
| `order_SYNTHETIC_547a954d_0069` | 6 | 4 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0071` | 6 | 4 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0072` | 6 | 4 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0073` | 6 | 4 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0074` | 6 | 4 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0075` | 6 | 4 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0076` | 6 | 4 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0077` | 6 | 4 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0079` | 6 | 4 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0080` | 6 | 4 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0081` | 9 | 6 | 2026-11-05 12:33:00Z (1793881980000 ms) | 180000 ms (3 min) |
| `order_SYNTHETIC_547a954d_0082` | 6 | 4 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0083` | 6 | 4 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0086` | 6 | 4 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0087` | 5 | 3 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0088` | 5 | 3 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0089` | 5 | 3 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0091` | 5 | 3 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0092` | 5 | 3 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0093` | 6 | 3 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0094` | 8 | 5 | 2026-11-05 12:33:00Z (1793881980000 ms) | 180000 ms (3 min) |
| `order_SYNTHETIC_547a954d_0095` | 8 | 5 | 2026-11-05 12:33:00Z (1793881980000 ms) | 180000 ms (3 min) |
| `order_SYNTHETIC_547a954d_0096` | 5 | 3 | 2026-11-05 12:10:00Z (1793880600000 ms) | 1560000 ms (26 min) |
| `order_SYNTHETIC_547a954d_0098` | 5 | 3 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0099` | 6 | 3 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0100` | 5 | 3 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0101` | 5 | 3 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0103` | 5 | 3 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0104` | 5 | 3 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0105` | 5 | 3 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0106` | 5 | 3 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0107` | 5 | 3 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0109` | 5 | 3 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0111` | 4 | 2 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0112` | 4 | 2 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0113` | 4 | 2 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0114` | 4 | 2 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0115` | 4 | 2 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0116` | 4 | 2 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0117` | 4 | 2 | 2026-11-05 12:10:00Z (1793880600000 ms) | 1560000 ms (26 min) |
| `order_SYNTHETIC_547a954d_0119` | 4 | 2 | 2026-11-05 12:10:00Z (1793880600000 ms) | 1560000 ms (26 min) |
| `order_SYNTHETIC_547a954d_0124` | 4 | 2 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0125` | 4 | 2 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0126` | 4 | 2 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0127` | 4 | 2 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0128` | 4 | 1 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0129` | 4 | 2 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0131` | 4 | 2 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0132` | 4 | 2 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0133` | 5 | 2 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0134` | 4 | 2 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0136` | 4 | 2 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0137` | 4 | 2 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0138` | 4 | 2 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0139` | 4 | 2 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0140` | 5 | 2 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0141` | 4 | 2 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0142` | 4 | 2 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0143` | 4 | 2 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0144` | 4 | 2 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0145` | 3 | 1 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0146` | 3 | 1 | 2026-11-05 12:05:00Z (1793880300000 ms) | 1860000 ms (31 min) |
| `order_SYNTHETIC_547a954d_0147` | 3 | 1 | 2026-11-05 12:10:00Z (1793880600000 ms) | 1560000 ms (26 min) |
| `order_SYNTHETIC_547a954d_0148` | 3 | 1 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0149` | 3 | 1 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0150` | 4 | 1 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0151` | 4 | 1 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0152` | 3 | 1 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0153` | 3 | 1 | 2026-11-05 12:15:00Z (1793880900000 ms) | 1260000 ms (21 min) |
| `order_SYNTHETIC_547a954d_0154` | 3 | 1 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0155` | 3 | 1 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0156` | 3 | 1 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0157` | 3 | 1 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0158` | 3 | 1 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0159` | 3 | 1 | 2026-11-05 12:20:00Z (1793881200000 ms) | 960000 ms (16 min) |
| `order_SYNTHETIC_547a954d_0160` | 3 | 1 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0161` | 3 | 1 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0162` | 3 | 1 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0163` | 3 | 1 | 2026-11-05 12:25:00Z (1793881500000 ms) | 660000 ms (11 min) |
| `order_SYNTHETIC_547a954d_0164` | 3 | 1 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0165` | 3 | 1 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0166` | 3 | 1 | 2026-11-05 12:30:00Z (1793881800000 ms) | 360000 ms (6 min) |
| `order_SYNTHETIC_547a954d_0167` | 3 | 0 | 2026-11-05 12:29:28Z (1793881768677 ms) | 391323 ms |
| `order_SYNTHETIC_547a954d_0168` | 2 | 0 | 2026-11-05 12:28:56Z (1793881736766 ms) | 423234 ms |

## Proposal

- Mode: rules; status: validated
- Cause: `merchant_worker_regression`; faulting change `deploy_9b85880366`; method `none`; evidence level `reproduced_in_fixture`
- Proposed operation: `reprocess_inbox`; next check `validate_corrected_handler`
- Supporting evidence: `ev_trigger`, `ev_worker_errors`, `ev_deploy_9b85880366`, `ev_fixture`, `ev_metric_processing`
- Contradicting evidence: none
- Unsafe proposals blocked: 0; validator reasons recorded: 0

## Approved operation

- Operation: `reprocess_inbox` on 103 orders worth INR 125,027.00 (12502700 paise)
- Plan hash: `9db6c8c60bee4995eaed5927d37f339485ca3d204b4ffcac9b135286f2acc12e` (policy `policy-v3`)
- Planned at 2026-11-05 12:34:00Z (1793882040000 ms); the plan expires at 2026-11-05 13:04:00Z (1793883840000 ms)
- Approved by `op_demo_operator` at 2026-11-05 12:35:00Z (1793882100000 ms); the approval expires at 2026-11-05 12:50:00Z (1793883000000 ms); it names this plan hash: yes
- Fix `payload@5` validated by `op_demo_operator` against `deploy_e1b540bacd`: reproduced yes, passed yes
- Target orders: 103, one per row under Execution, sorted

## Preconditions

### At planning

- 114 candidate orders: 103 targets, 11 excluded
- Dry runs of `deploy_e1b540bacd+payload@5`: 103, 103 passed

| Excluded order | Amount | Reasons |
|---|---|---|
| `order_SYNTHETIC_547a954d_0064` | INR 1,549.00 (154900 paise) | `cancelled` |
| `order_SYNTHETIC_547a954d_0078` | INR 2,762.00 (276200 paise) | `conflicting_evidence` |
| `order_SYNTHETIC_547a954d_0102` | INR 808.00 (80800 paise) | `cancelled` |
| `order_SYNTHETIC_547a954d_0108` | INR 1,099.00 (109900 paise) | `amount_mismatch` |
| `order_SYNTHETIC_547a954d_0110` | INR 2,450.00 (245000 paise) | `refunded` |
| `order_SYNTHETIC_547a954d_0118` | INR 1,022.00 (102200 paise) | `refunded` |
| `order_SYNTHETIC_547a954d_0120` | INR 1,235.00 (123500 paise) | `cancelled` |
| `order_SYNTHETIC_547a954d_0121` | INR 512.00 (51200 paise) | `multiple_captures` |
| `order_SYNTHETIC_547a954d_0122` | INR 821.00 (82100 paise) | `conflicting_evidence` |
| `order_SYNTHETIC_547a954d_0123` | INR 4,782.00 (478200 paise) | `multiple_captures` |
| `order_SYNTHETIC_547a954d_0130` | INR 1,151.00 (115100 paise) | `amount_mismatch` |

| Dry run that did not pass | Reason | Detail |
|---|---|---|

### At execution

- 15 passed, 0 failed, 0 not run

| Check | Result | Details |
|---|---|---|
| `run_scope` | passed |  |
| `plan_not_approved` | passed |  |
| `plan_expired` | passed |  |
| `plan_hash_mismatch` | passed |  |
| `policy_version_changed` | passed |  |
| `approval_missing` | passed |  |
| `approver_not_human` | passed |  |
| `approval_hash_mismatch` | passed |  |
| `approval_expired` | passed |  |
| `fix_not_validated` | passed |  |
| `deployed_version_changed` | passed |  |
| `target_ineligible` | passed |  |
| `evidence_changed` | passed |  |
| `prior_action_exists` | passed |  |
| `corrected_handler_rerun` | passed |  |

## Execution

- Status: `succeeded` at 2026-11-05 12:36:00Z (1793882160000 ms)
- Orders: completed 103, no\_op 0, refused 0, failed 0

| Order | Payment | Amount | Result | Reasons |
|---|---|---|---|---|
| `order_SYNTHETIC_547a954d_0049` | `pay_SYNTHETIC_547a954d_00050` | INR 621.00 (62100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0050` | `pay_SYNTHETIC_547a954d_00051` | INR 373.00 (37300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0051` | `pay_SYNTHETIC_547a954d_00052` | INR 4,176.00 (417600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0052` | `pay_SYNTHETIC_547a954d_00053` | INR 3,577.00 (357700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0053` | `pay_SYNTHETIC_547a954d_00054` | INR 722.00 (72200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0054` | `pay_SYNTHETIC_547a954d_00055` | INR 902.00 (90200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0055` | `pay_SYNTHETIC_547a954d_00056` | INR 1,053.00 (105300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0056` | `pay_SYNTHETIC_547a954d_00057` | INR 647.00 (64700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0057` | `pay_SYNTHETIC_547a954d_00058` | INR 322.00 (32200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0058` | `pay_SYNTHETIC_547a954d_00059` | INR 621.00 (62100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0059` | `pay_SYNTHETIC_547a954d_00060` | INR 712.00 (71200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0060` | `pay_SYNTHETIC_547a954d_00062` | INR 635.00 (63500 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0061` | `pay_SYNTHETIC_547a954d_00063` | INR 1,100.00 (110000 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0062` | `pay_SYNTHETIC_547a954d_00064` | INR 416.00 (41600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0063` | `pay_SYNTHETIC_547a954d_00065` | INR 2,296.00 (229600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0065` | `pay_SYNTHETIC_547a954d_00068` | INR 1,826.00 (182600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0066` | `pay_SYNTHETIC_547a954d_00069` | INR 578.00 (57800 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0067` | `pay_SYNTHETIC_547a954d_00070` | INR 655.00 (65500 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0068` | `pay_SYNTHETIC_547a954d_00071` | INR 1,324.00 (132400 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0069` | `pay_SYNTHETIC_547a954d_00072` | INR 831.00 (83100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0071` | `pay_SYNTHETIC_547a954d_00074` | INR 1,742.00 (174200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0072` | `pay_SYNTHETIC_547a954d_00075` | INR 246.00 (24600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0073` | `pay_SYNTHETIC_547a954d_00076` | INR 281.00 (28100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0074` | `pay_SYNTHETIC_547a954d_00077` | INR 1,950.00 (195000 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0075` | `pay_SYNTHETIC_547a954d_00078` | INR 801.00 (80100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0076` | `pay_SYNTHETIC_547a954d_00079` | INR 3,195.00 (319500 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0077` | `pay_SYNTHETIC_547a954d_00080` | INR 491.00 (49100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0079` | `pay_SYNTHETIC_547a954d_00082` | INR 1,467.00 (146700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0080` | `pay_SYNTHETIC_547a954d_00083` | INR 297.00 (29700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0081` | `pay_SYNTHETIC_547a954d_00085` | INR 1,180.00 (118000 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0082` | `pay_SYNTHETIC_547a954d_00086` | INR 2,152.00 (215200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0083` | `pay_SYNTHETIC_547a954d_00087` | INR 951.00 (95100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0086` | `pay_SYNTHETIC_547a954d_00090` | INR 618.00 (61800 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0087` | `pay_SYNTHETIC_547a954d_00091` | INR 1,302.00 (130200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0088` | `pay_SYNTHETIC_547a954d_00092` | INR 991.00 (99100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0089` | `pay_SYNTHETIC_547a954d_00093` | INR 319.00 (31900 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0091` | `pay_SYNTHETIC_547a954d_00095` | INR 1,159.00 (115900 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0092` | `pay_SYNTHETIC_547a954d_00096` | INR 572.00 (57200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0093` | `pay_SYNTHETIC_547a954d_00097` | INR 2,091.00 (209100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0094` | `pay_SYNTHETIC_547a954d_00099` | INR 1,022.00 (102200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0095` | `pay_SYNTHETIC_547a954d_00101` | INR 772.00 (77200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0096` | `pay_SYNTHETIC_547a954d_00102` | INR 1,337.00 (133700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0098` | `pay_SYNTHETIC_547a954d_00104` | INR 751.00 (75100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0099` | `pay_SYNTHETIC_547a954d_00105` | INR 6,368.00 (636800 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0100` | `pay_SYNTHETIC_547a954d_00106` | INR 2,715.00 (271500 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0101` | `pay_SYNTHETIC_547a954d_00107` | INR 1,677.00 (167700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0103` | `pay_SYNTHETIC_547a954d_00109` | INR 825.00 (82500 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0104` | `pay_SYNTHETIC_547a954d_00110` | INR 241.00 (24100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0105` | `pay_SYNTHETIC_547a954d_00111` | INR 794.00 (79400 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0106` | `pay_SYNTHETIC_547a954d_00112` | INR 336.00 (33600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0107` | `pay_SYNTHETIC_547a954d_00113` | INR 1,249.00 (124900 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0109` | `pay_SYNTHETIC_547a954d_00115` | INR 1,349.00 (134900 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0111` | `pay_SYNTHETIC_547a954d_00117` | INR 2,521.00 (252100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0112` | `pay_SYNTHETIC_547a954d_00118` | INR 423.00 (42300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0113` | `pay_SYNTHETIC_547a954d_00119` | INR 654.00 (65400 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0114` | `pay_SYNTHETIC_547a954d_00120` | INR 1,704.00 (170400 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0115` | `pay_SYNTHETIC_547a954d_00121` | INR 4,045.00 (404500 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0116` | `pay_SYNTHETIC_547a954d_00122` | INR 503.00 (50300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0117` | `pay_SYNTHETIC_547a954d_00123` | INR 188.00 (18800 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0119` | `pay_SYNTHETIC_547a954d_00125` | INR 3,156.00 (315600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0124` | `pay_SYNTHETIC_547a954d_00132` | INR 978.00 (97800 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0125` | `pay_SYNTHETIC_547a954d_00133` | INR 466.00 (46600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0126` | `pay_SYNTHETIC_547a954d_00134` | INR 1,284.00 (128400 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0127` | `pay_SYNTHETIC_547a954d_00135` | INR 733.00 (73300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0128` | `pay_SYNTHETIC_547a954d_00136` | INR 2,207.00 (220700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0129` | `pay_SYNTHETIC_547a954d_00137` | INR 352.00 (35200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0131` | `pay_SYNTHETIC_547a954d_00139` | INR 1,489.00 (148900 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0132` | `pay_SYNTHETIC_547a954d_00140` | INR 1,210.00 (121000 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0133` | `pay_SYNTHETIC_547a954d_00142` | INR 644.00 (64400 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0134` | `pay_SYNTHETIC_547a954d_00143` | INR 783.00 (78300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0136` | `pay_SYNTHETIC_547a954d_00145` | INR 858.00 (85800 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0137` | `pay_SYNTHETIC_547a954d_00146` | INR 786.00 (78600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0138` | `pay_SYNTHETIC_547a954d_00147` | INR 343.00 (34300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0139` | `pay_SYNTHETIC_547a954d_00148` | INR 317.00 (31700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0140` | `pay_SYNTHETIC_547a954d_00150` | INR 905.00 (90500 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0141` | `pay_SYNTHETIC_547a954d_00151` | INR 562.00 (56200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0142` | `pay_SYNTHETIC_547a954d_00152` | INR 1,985.00 (198500 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0143` | `pay_SYNTHETIC_547a954d_00153` | INR 790.00 (79000 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0144` | `pay_SYNTHETIC_547a954d_00154` | INR 991.00 (99100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0145` | `pay_SYNTHETIC_547a954d_00155` | INR 2,363.00 (236300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0146` | `pay_SYNTHETIC_547a954d_00156` | INR 893.00 (89300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0147` | `pay_SYNTHETIC_547a954d_00157` | INR 1,364.00 (136400 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0148` | `pay_SYNTHETIC_547a954d_00158` | INR 783.00 (78300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0149` | `pay_SYNTHETIC_547a954d_00159` | INR 826.00 (82600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0150` | `pay_SYNTHETIC_547a954d_00160` | INR 375.00 (37500 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0151` | `pay_SYNTHETIC_547a954d_00162` | INR 397.00 (39700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0152` | `pay_SYNTHETIC_547a954d_00163` | INR 246.00 (24600 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0153` | `pay_SYNTHETIC_547a954d_00164` | INR 522.00 (52200 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0154` | `pay_SYNTHETIC_547a954d_00165` | INR 670.00 (67000 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0155` | `pay_SYNTHETIC_547a954d_00166` | INR 328.00 (32800 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0156` | `pay_SYNTHETIC_547a954d_00167` | INR 1,209.00 (120900 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0157` | `pay_SYNTHETIC_547a954d_00168` | INR 967.00 (96700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0158` | `pay_SYNTHETIC_547a954d_00169` | INR 1,459.00 (145900 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0159` | `pay_SYNTHETIC_547a954d_00170` | INR 3,448.00 (344800 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0160` | `pay_SYNTHETIC_547a954d_00171` | INR 557.00 (55700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0161` | `pay_SYNTHETIC_547a954d_00172` | INR 369.00 (36900 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0162` | `pay_SYNTHETIC_547a954d_00173` | INR 4,331.00 (433100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0163` | `pay_SYNTHETIC_547a954d_00174` | INR 1,067.00 (106700 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0164` | `pay_SYNTHETIC_547a954d_00175` | INR 4,133.00 (413300 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0165` | `pay_SYNTHETIC_547a954d_00176` | INR 801.00 (80100 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0166` | `pay_SYNTHETIC_547a954d_00177` | INR 1,369.00 (136900 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0167` | `pay_SYNTHETIC_547a954d_00179` | INR 419.00 (41900 paise) | completed |  |
| `order_SYNTHETIC_547a954d_0168` | `pay_SYNTHETIC_547a954d_00180` | INR 626.00 (62600 paise) | completed |  |

## Postconditions

- Captured payments: 161 before, 161 after; captured value INR 194,445.00 (19444500 paise) before, INR 194,445.00 (19444500 paise) after
- New captures: none; capture unchanged on every target: yes
- Duplicate fulfilments: 0; payments behind more than one fulfilment: none
- Outstanding exceptions: 11

| Order | Held at | Reasons |
|---|---|---|
| `order_SYNTHETIC_547a954d_0064` | planning | `cancelled` |
| `order_SYNTHETIC_547a954d_0078` | planning | `conflicting_evidence` |
| `order_SYNTHETIC_547a954d_0102` | planning | `cancelled` |
| `order_SYNTHETIC_547a954d_0108` | planning | `amount_mismatch` |
| `order_SYNTHETIC_547a954d_0110` | planning | `refunded` |
| `order_SYNTHETIC_547a954d_0118` | planning | `refunded` |
| `order_SYNTHETIC_547a954d_0120` | planning | `cancelled` |
| `order_SYNTHETIC_547a954d_0121` | planning | `multiple_captures` |
| `order_SYNTHETIC_547a954d_0122` | planning | `conflicting_evidence` |
| `order_SYNTHETIC_547a954d_0123` | planning | `multiple_captures` |
| `order_SYNTHETIC_547a954d_0130` | planning | `amount_mismatch` |

## Value

| Figure | Amount | Definition |
|---|---|---|
| Restored processing value | INR 125,027.00 (12502700 paise) | Order value of the orders this action fulfilled whose capture was known before it ran: money already collected, now processed. Not new revenue. |
| Unresolved value | INR 18,291.00 (1829100 paise) | Order value of this plan's candidate orders still unfulfilled after this action, whether left out at planning or not fulfilled at execution; an order with no order record is counted, not summed. |
| Additional captured | INR 0.00 (0 paise) | New provider captures this action caused. Reprocessing fulfils captures already received, so it is 0. |

- Unresolved orders: 11; counted but not valued: none

## Boundary

- Environment: `synthetic`
- Provider calls made by this action: 0
- Original capture unchanged: yes
