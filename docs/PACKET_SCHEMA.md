# Evidence packet contract (`packet_version = "pkt-2"`)

This is the interface between `packet.py` (which builds packets from the database) and every packet consumer: `rules.py`, `attribution_rules.py` and `selective.py` (the rules baseline), `diagnosis.py`, the evaluation and the UI.

- **Builders** must emit every core key listed for a kind.
- **Consumers** may depend only on core keys.
- **Extra keys** are allowed but optional, and no consumer may require them.

The packet shape comes from the M0 packets (`experiments/m0/packets/`), normalised to the real detector names in `metrics.py`.

## General rules

1. **Cutoff and scope.** Every item has `known_at_ms <= incident.evidence_cutoff_ms`, which the contract enforces, and comes from the incident's run only. Anything derived from records known after the cutoff is excluded. That includes counts: computing them as of the cutoff is the builder's job.
2. **Deterministic output.** The same database, run, incident and cutoff give a byte-identical packet and therefore the same `packet_hash()`. Wall-clock values (acknowledgement latency, `*_wall_ms`) never enter a packet.
3. **No answer leakage.** Packets contain no oracle content, private simulator tables (`sim_*`), variant IDs (`payload@3`), family or variant labels, or words that give the answer away. Fixture-check results are present in **every** packet.
4. **Neutral, stable evidence IDs.** IDs match `^ev_[a-z0-9_]{1,60}$` and are listed per kind below.
5. **Untrusted text.** `text` holds untrusted free text: stack traces, diffs and log lines. A deployment's `facts.summary`, its commit message, is untrusted free text too; since prompt `diag-p3`, the prompt fences it like `text`. Consumers treat both as data, never as instructions.
6. **Facts reach the model only through `diagnosis.FIELD_POLICY`** (since `diag-p5`). A new fact needs a rule there. Until it has one, it is not rendered, and `tests/test_prompt_p5.py` fails for any stored or freshly built packet that carries it.
7. **Size budget.** Aim for 4k tokens or fewer in total:
   - diffs: at most 60 lines or 2,500 characters each;
   - stack trace: one, at most 4,000 characters;
   - logs: at most 8 items;
   - order journeys: at most 3.
8. **Candidates.**
   - `candidate_change_ids`: every deployment item's `deploy_id`, in `deployed_at_ms` order.
   - `candidate_methods`: the payment methods seen in the run's attempts, sorted.

## Incident card (`contracts.IncidentCard`)

| Field | Meaning |
|---|---|
| `symptom` | Neutral sentence, e.g. "Fewer orders are completing than usual" |
| `trigger` | Detector name that opened the incident, method-suffixed for method drops, e.g. `method_authorization_drop[card]` |
| `cohort` | `{"merchant_id", "integration_id", "methods": [...]}` |
| `window_start_ms`, `window_end_ms` | Incident window |
| `evidence_cutoff_ms` | Replay time T of the diagnosis |
| `summary_counts` | `{"orders_with_attempts", "orders_completed", "payment_attempts"}` |

**Detector names** (from `metrics.py`):

- `orders_not_completing`
- `merchant_processing_gap`
- `delivery_rejection_spike`
- `unresolved_spike`
- `method_authorization_drop[<method>]`, one entry per method, e.g. `method_authorization_drop[upi]`
- `signature_rejections_while_accepting`, one entry per integration, reported only for an integration whose deliveries show that pattern

## Items by kind (core keys)

### `incident_trigger` — exactly one, `ev_trigger`

| Key | Type | Meaning |
|---|---|---|
| `fired` | list[str] | Detector names that fired at the cutoff, sorted |
| `not_fired` | list[str] | Detector names evaluated but not firing, sorted |
| `fired_since_open` | list[str] | **pkt-2.** Every detector that fired while the incident was open, from its alert history known by the cutoff, sorted. Includes the trigger even if it has stopped firing |
| `first_breach_ms` | int \| null | When the incident's trigger first fired |

### `metric` — `ev_metric_<method>` per method, `ev_metric_all`, and `ev_metric_processing`

Provider cohort items (`ev_metric_<method>` and `ev_metric_all`, where `method = "all"`):

| Key | Type |
|---|---|
| `method` | str |
| `mature_cohort_start_ms`, `mature_cohort_end_ms` | int |
| `attempts`, `authorised`, `captured`, `unresolved` | int |
| `authorisation_ratio` | float \| null (null when `attempts = 0`) |
| `authorisation_ratio_95ci` | [float, float] \| null (Wilson) |
| `baseline_attempts` | int |
| `baseline_authorisation_ratio` | float \| null |
| `baseline_95ci` | [float, float] \| null |
| `share_of_attempts`, `baseline_share_of_attempts` | float \| null |

Merchant processing item (`ev_metric_processing`):

| Key | Type |
|---|---|
| `captured_orders`, `processed`, `pending_captured`, `held_for_review` | int |
| `pending_captured_value_paise` | int |
| `processing_completion_ratio`, `baseline_processing_completion_ratio` | float \| null |
| `grace_ms` | int (captures newer than this are not yet counted as pending) |

### `provider_state` — exactly one, `ev_provider_state`

| Key | Type | Meaning |
|---|---|---|
| `snapshot_age_ms` | int \| null | Cutoff minus the latest snapshot's `known_at_ms` |
| `observation_coverage` | float \| null | Attempts with sufficiently recent provider state ÷ attempts |
| `payments_by_status` | dict[str, int] | Projected status counts over the lookback |
| `failed_by_reason` | dict[str, int] | Provider `error_reason` counts |
| `failed_by_method_and_reason` | dict[str, dict[str, int]] | |
| `failed_by_error_source` | dict[str, int] | Provider `error_source` counts |
| `failed_then_authorised_later` | int | Payments with late authorisation |
| `authorised_not_captured` | int | |
| `captured_without_received_event` | int | Provider-observed captures with no accepted inbox event (receiver signal) |
| `payments_needing_reconciliation` | int | |
| `reconciliation_reasons` | dict[str, int] | `ConflictReason` counts |
| `stale_payments` | int | |

### `delivery_health` — exactly one, `ev_delivery`

`window_start_ms`, `delivery_attempts`, `accepted`, `duplicates`, `conflicting_ids`, `rejected_signature`, `rejected_schema`, `rejected_size`, `distinct_events_rejected`, `first_rejection_ms` (int \| null) and `accepted_since_first_rejection`.

### `worker_health` — exactly one, `ev_worker`

`inbox_pending`, `inbox_failed`, `inbox_manual_review`, `oldest_unprocessed_age_ms` (int \| null), `processed_last_30m`, `failed_attempts_last_60m`, `last_event_received_ms` (int \| null) and `retry_policy` (str).

### `worker_error` — `ev_worker_errors`, present only when worker failures exist

| Key | Type | Meaning |
|---|---|---|
| `exception_category` | str \| null | Most common category; **null when the merchant emits free text only** |
| `failed_events` | int | Distinct inbox rows that failed |
| `failed_by_event_type` | dict[str, int] | |
| `first_failure_ms`, `last_failure_ms` | int | |
| `worker_versions` | dict[str, int] | Failing attempts per worker version |
| `distinct_trace_signatures` | int | |

`text` holds the most common failure's stack trace, verbatim.

### `fixture_check` — exactly one, `ev_fixture`, in every packet

`deployed_version` (str), `tests_run`, `tests_failed`, and `results`, a list of `{"test_id", "passed", "detail" (str \| null), "top_frame" (str \| null)}`. `text` holds the failing trace, if any. The checks run against the **currently deployed** handler only. Corrected handlers never appear before a diagnosis.

### `deployment` — `ev_deploy_<deploy-id-suffix>`, one per deployment known by the cutoff within the lookback

`deploy_id`, `service`, `kind` (`code`/`config`), `deployed_at_ms`, `files_changed` (list[str]) and `summary`. `title` is "Deployment <id> to <service>" and `text` holds the bounded diff.

### `log` — `ev_log_<nn>`, at most 8

`source`, `level` and `occurred_at_ms`. `text` holds the message.

### `order_journey` — `ev_journey_<nn>`, at most 3, optional sample orders

`order_id`, `order_amount_paise`, `attempts` (a list of `{"payment_id", "method", "observations": [{"status", "occurred_at_ms", "known_at_ms", "source"}]}`) and `projection` (`{"status", "exclusions", "late_authorization", "conflicts"}`).

## What the rules baseline may use

EVALUATION §9 lists what the rules baseline may use. Under this contract that means:

- which detectors fired (`ev_trigger`);
- the structured exception category (`ev_worker_errors.exception_category`);
- provider error fields (`ev_provider_state.failed_by_*`);
- the latest deployment before the first error, comparing `deployed_at_ms` against `first_failure_ms` / `first_rejection_ms`;
- the top frame of the stack trace (parsed from `ev_worker_errors.text`, or `ev_fixture.results[].top_frame`);
- the fixture results.

The model receives exactly the same packet.

These are the inputs of rules-v2, the baseline M4 scored, which the evaluation still runs. The product's rules mode runs `rules-v3`: rules-v2's answer with the faulting deployment from `attribution-v1` (`attribution_rules.py`). That reads more of the same packet: every frame of the stack traces (`ev_worker_errors.text`, `ev_fixture.text`), the deployments' diffs (`text`) with their `service`, `kind` and `files_changed`, the delivery health, and the logs' `source`, `level`, `occurred_at_ms` and `occurrences` (an extra key, read when present). It never reads a deployment's `summary` or a log line's `text`.

## Version history

- **pkt-1** (phase 3): the initial contract.
- **pkt-2** (phase 3 verification): adds `ev_trigger.fired_since_open`. A moderate method drop opened its incident, then stopped firing before the diagnosis cutoff, so `fired` was empty and the rules baseline saw no signal (2 of 18 dev runs). An on-call engineer would see the alert history, so the packet now carries it. Consumers should use `fired_since_open` together with `fired` and `incident.trigger`.
