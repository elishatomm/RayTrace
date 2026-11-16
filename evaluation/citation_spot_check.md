# Citation spot check (owner to fill in)

EVALUATION.md §9 asks a person to check 10 diagnoses: does the evidence each one cites support what it claims? §9 forbids grading this with another model, so every verdict cell below is empty, for you.

## The sample

The original sample, kept: 10 of the 30 `test_frozen` model diagnoses (qwen3:8b, thinking off, prompt diag-p2, validator val-4; all 30 validated), drawn by `random.Random(20261112).sample(...)` over the traces in file order and sorted by run ID (`SPOT_CHECK_SEED` in `src/recovery_workbench/evaluation/summarize.py`). The rule was committed with the first sheet in the root commit `833264a`, before anyone judged a diagnosis. It was not written down before the frozen test ran: that commit holds both, so git cannot show which came first. The mechanical check re-draws the sample on its own and gets the same 10 runs in the same order.

## How to judge (rubric)

For each diagnosis, read the claim, then each cited item exactly as the model received it. The incident card the model also received is shown too, because claims refer to it, but it is not an item and cannot be cited. (The instructions the model received are not repeated.)

For each item **cited as supporting**, write one word:

- **yes**: the item states something a sentence of the claim asserts, or something the hypothesis needs.
- **partly**: the item bears on the claim but backs only part of it, or only through a calculation or inference the claim does not show.
- **no**: the item does not bear on anything the claim asserts.
- **against**: the item contradicts what it is cited for.

For each item **cited as contradicting** (the model says it argues against its own hypothesis or for another one), write yes, partly or no: does it?

Then name any sentence of the claim that no cited item supports, or write none.

Judge support, not correctness: a wrong diagnosis can cite good support, and a right one can cite irrelevant items. Judge only from the text shown, which is what the model had. The answer key is at the end; read it after judging.

**Pointers** under each claim come from string matching. For each number, quoted term, clock time or ID in the claim they list where it occurs, best match by name first, and say whether that item is cited. They are not verdicts: a match can be a coincidence (0, 1 and 2 occur almost everywhere), a loose match is weaker still, and a derived figure may match nothing.

## Tally (fill in last)

| # | Run | Supporting cited | yes | partly | no | against | Contradicting cited | of them argue against | Unsupported sentences |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
| 1 | run_6aa1fe76d4 | 4 |  |  |  |  | 4 |  |  |
| 2 | run_6c9ea0253e | 5 |  |  |  |  | 0 |  |  |
| 3 | run_7988d37192 | 4 |  |  |  |  | 0 |  |  |
| 4 | run_8dbc8185b6 | 3 |  |  |  |  | 0 |  |  |
| 5 | run_b33647f1c5 | 3 |  |  |  |  | 0 |  |  |
| 6 | run_d40e7cae7e | 3 |  |  |  |  | 0 |  |  |
| 7 | run_ed42890977 | 4 |  |  |  |  | 0 |  |  |
| 8 | run_ef12850923 | 5 |  |  |  |  | 0 |  |  |
| 9 | run_fc1654e2ef | 5 |  |  |  |  | 0 |  |  |
| 10 | run_fe111b6f00 | 4 |  |  |  |  | 4 |  |  |

---

## 1. run_6aa1fe76d4

**The answer:** hypothesis `receiver_rejecting_deliveries`; faulting change `none`; method `none`; operation `escalate`; evidence level `supported`. It cites 4 supporting and 4 contradicting items of the packet's 14.

**The claim** (the rationale, verbatim, split at sentence ends):

1. The incident card shows fewer orders completing than usual, but the provider's authorisation and capture ratios are stable or improved.
2. The merchant's processing of captured payments is complete, but 56 orders are held for review, indicating conflicting evidence.
3. The order journeys show payments failed then captured, or captured without received events, suggesting the merchant's system is rejecting valid deliveries before they are durably stored.

**Pointers** (string matching only, not a verdict):

- sentence 2, number `56`: in a cited item: ev_metric_processing: facts.held_for_review = 56 (cited); ev_provider_state: facts.payments_needing_reconciliation = 56 (not cited); ev_worker: facts.inbox_manual_review = 56 (not cited); and 3 more

**The incident card**, not a citable item, exactly as the model received it:

```text
Evidence packet pkt-2
Incident card
Symptom: Fewer orders are completing than usual
Trigger: orders_not_completing
Cohort: {"merchant_id": "merchant_SYNTHETIC_A", "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]}
Window: 2026-11-07 10:10:00Z to 2026-11-07 11:10:00Z; evidence cutoff 2026-11-07 12:00:00Z
Summary counts: {"orders_with_attempts": 360, "orders_completed": 288, "payment_attempts": 374}
Candidate deployment IDs: none in this packet
Candidate payment methods: card, netbanking, upi
```

**ev_metric_processing**, cited as supporting, exactly as the model received it:

```text
[ev_metric_processing] metric, known 12:00:00Z: Merchant processing of captured payments
facts: {"captured_orders": 70, "processed": 70, "pending_captured": 0, "held_for_review": 56, "pending_captured_value_paise": 0, "processing_completion_ratio": 1.0, "baseline_processing_completion_ratio": 1.0, "grace": "5m00s", "lookback": "1h00m"}
```

**ev_journey_01**, cited as supporting, exactly as the model received it:

```text
[ev_journey_01] order_journey, known 12:00:00Z: Order journey for order_SYNTHETIC_6f7e892e_0345
facts: {"order_id": "order_SYNTHETIC_6f7e892e_0345", "order_amount_paise": 26100, "attempts": [{"payment_id": "pay_SYNTHETIC_6f7e892e_00359", "method": "upi", "observations": [{"status": "authorized", "occurred_at": "11:48:27Z", "known_at": "11:48:28Z", "source": "webhook"}, {"status": "failed", "occurred_at": "11:49:13Z", "known_at": "11:49:14Z", "source": "webhook"}, {"status": "captured", "occurred_at": "11:49:13Z", "known_at": "11:50:12Z", "source": "webhook"}, {"status": "captured", "occurred_at": "11:55:00Z", "known_at": "11:55:00Z", "source": "snapshot"}]}], "projection": {"status": "captured", "exclusions": ["conflicting_evidence"], "late_authorization": true, "conflicts": ["failure_after_success", "same_time_contradiction"]}}
```

**ev_journey_02**, cited as supporting, exactly as the model received it:

```text
[ev_journey_02] order_journey, known 12:00:00Z: Order journey for order_SYNTHETIC_6f7e892e_0360
facts: {"order_id": "order_SYNTHETIC_6f7e892e_0360", "order_amount_paise": 52000, "attempts": [{"payment_id": "pay_SYNTHETIC_6f7e892e_00374", "method": "netbanking", "observations": [{"status": "authorized", "occurred_at": "11:58:33Z", "known_at": "11:58:36Z", "source": "webhook"}]}], "projection": {"status": "authorized", "exclusions": ["unresolved_attempt", "not_captured"], "late_authorization": false, "conflicts": []}}
```

**ev_log_02**, cited as supporting, exactly as the model received it:

```text
[ev_log_02] log, known 11:50:30Z: warning log from order_worker
facts: {"source": "order_worker", "level": "warning", "occurred_at": "11:50:30Z", "occurrences": 56}
--- untrusted text ev_log_02 ---
event evt_SYNTHETIC_6f7e892e_000749 held for review (conflicting_evidence)
--- end of untrusted text ev_log_02 ---
```

**ev_metric_card**, cited as contradicting, exactly as the model received it:

```text
[ev_metric_card] metric, known 12:00:00Z: Provider authorisation and capture for card, mature cohort
facts: {"method": "card", "mature_cohort_start": "11:20:00Z", "mature_cohort_end": "11:50:00Z", "attempts": 20, "authorised": 20, "captured": 20, "unresolved": 0, "authorisation_ratio": 1.0, "authorisation_ratio_95ci": [0.839, 1.0], "baseline_attempts": 67, "baseline_authorisation_ratio": 0.925, "baseline_95ci": [0.837, 0.968], "share_of_attempts": 0.299, "baseline_share_of_attempts": 0.29}
```

**ev_metric_netbanking**, cited as contradicting, exactly as the model received it:

```text
[ev_metric_netbanking] metric, known 12:00:00Z: Provider authorisation and capture for netbanking, mature cohort
facts: {"method": "netbanking", "mature_cohort_start": "11:20:00Z", "mature_cohort_end": "11:50:00Z", "attempts": 13, "authorised": 13, "captured": 13, "unresolved": 0, "authorisation_ratio": 1.0, "authorisation_ratio_95ci": [0.772, 1.0], "baseline_attempts": 45, "baseline_authorisation_ratio": 0.911, "baseline_95ci": [0.793, 0.965], "share_of_attempts": 0.194, "baseline_share_of_attempts": 0.195}
```

**ev_metric_upi**, cited as contradicting, exactly as the model received it:

```text
[ev_metric_upi] metric, known 12:00:00Z: Provider authorisation and capture for upi, mature cohort
facts: {"method": "upi", "mature_cohort_start": "11:20:00Z", "mature_cohort_end": "11:50:00Z", "attempts": 34, "authorised": 30, "captured": 30, "unresolved": 0, "authorisation_ratio": 0.882, "authorisation_ratio_95ci": [0.734, 0.953], "baseline_attempts": 119, "baseline_authorisation_ratio": 0.933, "baseline_95ci": [0.873, 0.966], "share_of_attempts": 0.507, "baseline_share_of_attempts": 0.515}
```

**ev_metric_all**, cited as contradicting, exactly as the model received it:

```text
[ev_metric_all] metric, known 12:00:00Z: Provider authorisation and capture for all, mature cohort
facts: {"method": "all", "mature_cohort_start": "11:20:00Z", "mature_cohort_end": "11:50:00Z", "attempts": 67, "authorised": 63, "captured": 63, "unresolved": 0, "authorisation_ratio": 0.94, "authorisation_ratio_95ci": [0.856, 0.977], "baseline_attempts": 231, "baseline_authorisation_ratio": 0.926, "baseline_95ci": [0.885, 0.954], "share_of_attempts": 1.0, "baseline_share_of_attempts": 1.0}
```

**Verdicts for run_6aa1fe76d4:**

| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |
|---|---|---|---|---|
| ev_metric_processing | metric |  |  |  |
| ev_journey_01 | order_journey |  |  |  |
| ev_journey_02 | order_journey |  |  |  |
| ev_log_02 | log |  |  |  |

| Cited as contradicting | Kind | Argues against the hypothesis, or for another? (yes / partly / no) | Note |
|---|---|---|---|
| ev_metric_card | metric |  |  |
| ev_metric_netbanking | metric |  |  |
| ev_metric_upi | metric |  |  |
| ev_metric_all | metric |  |  |

Sentences of the claim that no cited item supports (numbers, or none): 

---

## 2. run_6c9ea0253e

**The answer:** hypothesis `provider_method_degradation`; faulting change `none`; method `card`; operation `escalate`; evidence level `supported`. It cites 5 supporting and 0 contradicting items of the packet's 13.

**The claim** (the rationale, verbatim, split at sentence ends):

1. The incident card indicates a drop in card method authorizations.
2. The card's authorisation ratio (32.4%) is significantly below its baseline (81.4%), and the metric shows a degradation in two successive windows.
3. The merchant processing metrics are unaffected, indicating the issue is on the provider side.
4. The order journey shows payments failing at the provider but later being authorized and captured, indicating a provider-side degradation.

**Pointers** (string matching only, not a verdict):

- sentence 2, percent `32.4%`: in a cited item: ev_metric_card: facts.authorisation_ratio = 0.324 (cited)
- sentence 2, percent `81.4%`: in a cited item: ev_metric_card: facts.baseline_authorisation_ratio = 0.814 (cited)

**The incident card**, not a citable item, exactly as the model received it:

```text
Evidence packet pkt-2
Incident card
Symptom: Fewer orders are completing than usual
Trigger: method_authorization_drop[card]
Cohort: {"merchant_id": "merchant_SYNTHETIC_A", "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]}
Window: 2026-11-05 11:50:00Z to 2026-11-05 12:50:00Z; evidence cutoff 2026-11-05 14:00:00Z
Summary counts: {"orders_with_attempts": 1300, "orders_completed": 1158, "payment_attempts": 1401}
Candidate deployment IDs: none in this packet
Candidate payment methods: card, netbanking, upi
```

**ev_trigger**, cited as supporting, exactly as the model received it:

```text
[ev_trigger] incident_trigger, known 14:00:00Z: Detector state at the evidence cutoff
facts: {"fired": ["method_authorization_drop[card]", "orders_not_completing"], "not_fired": ["delivery_rejection_spike", "merchant_processing_gap", "method_authorization_drop[netbanking]", "method_authorization_drop[upi]", "unresolved_spike"], "fired_since_open": ["method_authorization_drop[card]", "orders_not_completing"], "first_breach": "13:00:00Z", "reasons": {"delivery_rejection_spike": "too_few_rejections", "merchant_processing_gap": "too_few_pending", "method_authorization_drop[card]": "degraded_in_two_successive_windows", "method_authorization_drop[netbanking]": "insufficient_samples", "method_authorization_drop[upi]": "insufficient_samples", "orders_not_completing": "degraded_in_two_successive_windows", "unresolved_spike": "within_baseline"}}
```

**ev_metric_card**, cited as supporting, exactly as the model received it:

```text
[ev_metric_card] metric, known 14:00:00Z: Provider authorisation and capture for card, mature cohort
facts: {"method": "card", "mature_cohort_start": "13:20:00Z", "mature_cohort_end": "13:50:00Z", "attempts": 71, "authorised": 23, "captured": 23, "unresolved": 0, "authorisation_ratio": 0.324, "authorisation_ratio_95ci": [0.227, 0.439], "baseline_attempts": 365, "baseline_authorisation_ratio": 0.814, "baseline_95ci": [0.771, 0.85], "share_of_attempts": 0.477, "baseline_share_of_attempts": 0.438}
```

**ev_metric_all**, cited as supporting, exactly as the model received it:

```text
[ev_metric_all] metric, known 14:00:00Z: Provider authorisation and capture for all, mature cohort
facts: {"method": "all", "mature_cohort_start": "13:20:00Z", "mature_cohort_end": "13:50:00Z", "attempts": 149, "authorised": 96, "captured": 96, "unresolved": 0, "authorisation_ratio": 0.644, "authorisation_ratio_95ci": [0.565, 0.717], "baseline_attempts": 834, "baseline_authorisation_ratio": 0.876, "baseline_95ci": [0.852, 0.897], "share_of_attempts": 1.0, "baseline_share_of_attempts": 1.0}
```

**ev_provider_state**, cited as supporting, exactly as the model received it:

```text
[ev_provider_state] provider_state, known 14:00:00Z: Provider payment states and snapshot freshness
facts: {"snapshot_age": "0ms", "observation_coverage": 1.0, "payments_by_status": {"captured": 1158, "failed": 243}, "failed_by_reason": {"authentication_failed": 41, "bank_technical_error": 99, "insufficient_balance": 41, "issuer_unavailable": 62}, "failed_by_method_and_reason": {"card": {"authentication_failed": 19, "bank_technical_error": 71, "insufficient_balance": 16, "issuer_unavailable": 62}, "netbanking": {"authentication_failed": 11, "bank_technical_error": 18, "insufficient_balance": 18}, "upi": {"authentication_failed": 11, "bank_technical_error": 10, "insufficient_balance": 7}}, "failed_by_error_source": {"bank": 161, "customer": 82}, "failed_then_authorised_later": 13, "authorised_not_captured": 0, "captured_without_received_event": 0, "payments_needing_reconciliation": 0, "reconciliation_reasons": {}, "stale_payments": 0, "lookback_start": "08:00:00Z"}
```

**ev_journey_01**, cited as supporting, exactly as the model received it:

```text
[ev_journey_01] order_journey, known 14:00:00Z: Order journey for order_SYNTHETIC_642886f2_1232
facts: {"order_id": "order_SYNTHETIC_642886f2_1232", "order_amount_paise": 78100, "attempts": [{"payment_id": "pay_SYNTHETIC_642886f2_01326", "method": "card", "observations": [{"status": "failed", "occurred_at": "13:43:34Z", "known_at": "13:43:37Z", "source": "webhook"}, {"status": "authorized", "occurred_at": "13:54:34Z", "known_at": "13:54:34Z", "source": "webhook"}, {"status": "captured", "occurred_at": "13:55:34Z", "known_at": "13:55:35Z", "source": "webhook"}]}], "projection": {"status": "captured", "exclusions": ["already_fulfilled"], "late_authorization": true, "conflicts": []}}
```

**Verdicts for run_6c9ea0253e:**

| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |
|---|---|---|---|---|
| ev_trigger | incident_trigger |  |  |  |
| ev_metric_card | metric |  |  |  |
| ev_metric_all | metric |  |  |  |
| ev_provider_state | provider_state |  |  |  |
| ev_journey_01 | order_journey |  |  |  |

Sentences of the claim that no cited item supports (numbers, or none): 

---

## 3. run_7988d37192

**The answer:** hypothesis `merchant_worker_regression`; faulting change `deploy_a202f09a18`; method `none`; operation `reprocess_inbox`; evidence level `supported`. It cites 4 supporting and 0 contradicting items of the packet's 20.

**The claim** (the rationale, verbatim, split at sentence ends):

1. The merchant's worker failed to process captured payments due to a regression in the code that converts payment amounts to rupees.
2. The error 'InvalidOrderAmount' indicates the system expected an integer paise value but received a Decimal, which was introduced by the deployment of 'deploy_a202f09a18'.
3. The fixture tests also failed for the same reason, confirming the regression.

**Pointers** (string matching only, not a verdict):

- sentence 2, quoted `InvalidOrderAmount`: in a cited item: ev_worker_errors: text (cited); ev_fixture: text (cited); ev_fixture: facts.results[0].detail (cited); and 2 more
- sentence 2, deployment id `deploy_a202f09a18`: in a cited item: ev_deploy_a202f09a18: title (cited); ev_deploy_a202f09a18: facts.deploy_id (cited); ev_log_02: text (not cited); and 1 more

**The incident card**, not a citable item, exactly as the model received it:

```text
Evidence packet pkt-2
Incident card
Symptom: Fewer orders are completing than usual
Trigger: merchant_processing_gap
Cohort: {"merchant_id": "merchant_SYNTHETIC_A", "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]}
Window: 2026-11-04 10:10:00Z to 2026-11-04 11:05:00Z; evidence cutoff 2026-11-04 12:30:00Z
Summary counts: {"orders_with_attempts": 135, "orders_completed": 52, "payment_attempts": 141}
Candidate deployment IDs: deploy_a202f09a18, deploy_20e68482a7
Candidate payment methods: card, netbanking, upi
```

**ev_metric_processing**, cited as supporting, exactly as the model received it:

```text
[ev_metric_processing] metric, known 12:30:00Z: Merchant processing of captured payments
facts: {"captured_orders": 38, "processed": 0, "pending_captured": 38, "held_for_review": 0, "pending_captured_value_paise": 5133700, "processing_completion_ratio": 0.0, "baseline_processing_completion_ratio": 0.712, "grace": "5m00s", "lookback": "1h00m"}
```

**ev_worker_errors**, cited as supporting, exactly as the model received it:

```text
[ev_worker_errors] worker_error, known 12:30:00Z: Order worker failures, most common stack trace
facts: {"exception_category": null, "failed_events": 80, "failed_by_event_type": {"payment.captured": 80}, "first_failure": "10:49:30Z", "last_failure": "12:30:00Z", "worker_versions": {"deploy_20e68482a7": 233}, "distinct_trace_signatures": 1}
--- untrusted text ev_worker_errors ---
Traceback (most recent call last):
  File "merchant_app/handler.py", line 24, in handle_event
    return orders.build_instruction(parsed)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "merchant_app/orders.py", line 19, in build_instruction
    raise InvalidOrderAmount(f"order {parsed['order_id']}: expected positive integer paise, got {amount_paise!r}")
merchant_app.orders.InvalidOrderAmount: order order_SYNTHETIC_011ddfdf_0135: expected positive integer paise, got Decimal('1284')
--- end of untrusted text ev_worker_errors ---
```

**ev_fixture**, cited as supporting, exactly as the model received it:

```text
[ev_fixture] fixture_check, known 12:30:00Z: Prewritten fixture tests against the deployed version deploy_20e68482a7
facts: {"deployed_version": "deploy_20e68482a7", "tests_run": 4, "tests_failed": 1, "results": [{"test_id": "captured_payment_builds_fulfilment", "passed": false, "detail": "merchant_app.orders.InvalidOrderAmount: order order_SYNTHETIC_9001: expected positive integer paise, got Decimal('1200')", "top_frame": "merchant_app/orders.py:19 in build_instruction"}, {"test_id": "authorized_payment_changes_nothing", "passed": true, "detail": null, "top_frame": null}, {"test_id": "failed_payment_changes_nothing", "passed": true, "detail": null, "top_frame": null}, {"test_id": "signed_delivery_verifies", "passed": true, "detail": null, "top_frame": null}]}
--- untrusted text ev_fixture ---
Traceback (most recent call last):
  File "merchant_app/handler.py", line 24, in handle_event
    return orders.build_instruction(parsed)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "merchant_app/orders.py", line 19, in build_instruction
    raise InvalidOrderAmount(f"order {parsed['order_id']}: expected positive integer paise, got {amount_paise!r}")
merchant_app.orders.InvalidOrderAmount: order order_SYNTHETIC_9001: expected positive integer paise, got Decimal('1200')
--- end of untrusted text ev_fixture ---
```

**ev_deploy_a202f09a18**, cited as supporting, exactly as the model received it:

```text
[ev_deploy_a202f09a18] deployment, known 10:14:25Z: Deployment deploy_a202f09a18 to order_worker
facts: {"deploy_id": "deploy_a202f09a18", "service": "order_worker", "kind": "code", "deployed_at": "10:14:25Z", "files_changed": ["merchant_app/payload.py"], "summary": "Convert amounts to rupees when parsing events"}
--- untrusted text ev_deploy_a202f09a18 ---
diff --git a/merchant_app/payload.py b/merchant_app/payload.py
index e881edd..f75c6e0 100644
--- a/merchant_app/payload.py
+++ b/merchant_app/payload.py
@@ -1,4 +1,6 @@
 """Turn a verified webhook event into the flat fields the order flow uses."""
+from decimal import Decimal
+
 from merchant_app import config
 
 SUPPORTED_EVENTS = ("payment.authorized", "payment.captured", "payment.failed")
@@ -6,6 +8,11 @@
 
 class UnsupportedEvent(ValueError):
     """The integration does not subscribe to this event type."""
+
+
+def to_rupees(amount_paise):
+    """Order totals are shown and stored in rupees."""
+    return Decimal(amount_paise) / 100
 
 
 def parse_event(event):
@@ -19,6 +26,6 @@
         "order_id": entity[config.ORDER_REFERENCE_FIELD],
         "status": entity["status"],
         "method": entity.get("method"),
-        "amount_paise": entity["amount"],
+        "amount_paise": to_rupees(entity["amount"]),
         "currency": entity["currency"],
     }
--- end of untrusted text ev_deploy_a202f09a18 ---
```

**Verdicts for run_7988d37192:**

| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |
|---|---|---|---|---|
| ev_metric_processing | metric |  |  |  |
| ev_worker_errors | worker_error |  |  |  |
| ev_fixture | fixture_check |  |  |  |
| ev_deploy_a202f09a18 | deployment |  |  |  |

Sentences of the claim that no cited item supports (numbers, or none): 

---

## 4. run_8dbc8185b6

**The answer:** hypothesis `provider_method_degradation`; faulting change `none`; method `netbanking`; operation `escalate`; evidence level `supported`. It cites 3 supporting and 0 contradicting items of the packet's 13.

**The claim** (the rationale, verbatim, split at sentence ends):

1. The incident card indicates a drop in netbanking authorizations.
2. Metrics show a significant decrease in netbanking authorization ratio compared to baseline.
3. The provider state shows failed payments with netbanking as the method, and no evidence of merchant processing issues.
4. The worker health is normal, indicating the issue is on the provider side.

**Pointers** (string matching only, not a verdict):

- (the claim states no number, quoted term, clock time or ID)

**The incident card**, not a citable item, exactly as the model received it:

```text
Evidence packet pkt-2
Incident card
Symptom: Fewer orders are completing than usual
Trigger: method_authorization_drop[netbanking]
Cohort: {"merchant_id": "merchant_SYNTHETIC_A", "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]}
Window: 2026-11-06 12:40:00Z to 2026-11-06 13:40:00Z; evidence cutoff 2026-11-06 14:00:00Z
Summary counts: {"orders_with_attempts": 1300, "orders_completed": 1217, "payment_attempts": 1383}
Candidate deployment IDs: none in this packet
Candidate payment methods: card, netbanking, upi
```

**ev_trigger**, cited as supporting, exactly as the model received it:

```text
[ev_trigger] incident_trigger, known 14:00:00Z: Detector state at the evidence cutoff
facts: {"fired": [], "not_fired": ["delivery_rejection_spike", "merchant_processing_gap", "method_authorization_drop[card]", "method_authorization_drop[netbanking]", "method_authorization_drop[upi]", "orders_not_completing", "unresolved_spike"], "fired_since_open": ["method_authorization_drop[netbanking]"], "first_breach": "13:50:00Z", "reasons": {"delivery_rejection_spike": "too_few_rejections", "merchant_processing_gap": "too_few_pending", "method_authorization_drop[card]": "insufficient_samples", "method_authorization_drop[netbanking]": "insufficient_samples", "method_authorization_drop[upi]": "insufficient_samples", "orders_not_completing": "within_baseline", "unresolved_spike": "within_baseline"}}
```

**ev_metric_netbanking**, cited as supporting, exactly as the model received it:

```text
[ev_metric_netbanking] metric, known 14:00:00Z: Provider authorisation and capture for netbanking, mature cohort
facts: {"method": "netbanking", "mature_cohort_start": "13:20:00Z", "mature_cohort_end": "13:50:00Z", "attempts": 66, "authorised": 42, "captured": 42, "unresolved": 0, "authorisation_ratio": 0.636, "authorisation_ratio_95ci": [0.516, 0.742], "baseline_attempts": 348, "baseline_authorisation_ratio": 0.83, "baseline_95ci": [0.787, 0.866], "share_of_attempts": 0.468, "baseline_share_of_attempts": 0.411}
```

**ev_provider_state**, cited as supporting, exactly as the model received it:

```text
[ev_provider_state] provider_state, known 14:00:00Z: Provider payment states and snapshot freshness
facts: {"snapshot_age": "35m00s", "observation_coverage": 1.0, "payments_by_status": {"captured": 1217, "failed": 166}, "failed_by_reason": {"authentication_failed": 41, "bank_technical_error": 56, "insufficient_balance": 37, "issuer_unavailable": 32}, "failed_by_method_and_reason": {"card": {"authentication_failed": 7, "bank_technical_error": 8, "insufficient_balance": 11}, "netbanking": {"authentication_failed": 25, "bank_technical_error": 36, "insufficient_balance": 18, "issuer_unavailable": 32}, "upi": {"authentication_failed": 9, "bank_technical_error": 12, "insufficient_balance": 8}}, "failed_by_error_source": {"bank": 88, "customer": 78}, "failed_then_authorised_later": 15, "authorised_not_captured": 0, "captured_without_received_event": 0, "payments_needing_reconciliation": 0, "reconciliation_reasons": {}, "stale_payments": 0, "lookback_start": "08:00:00Z"}
```

**Verdicts for run_8dbc8185b6:**

| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |
|---|---|---|---|---|
| ev_trigger | incident_trigger |  |  |  |
| ev_metric_netbanking | metric |  |  |  |
| ev_provider_state | provider_state |  |  |  |

Sentences of the claim that no cited item supports (numbers, or none): 

---

## 5. run_b33647f1c5

**The answer:** hypothesis `provider_method_degradation`; faulting change `none`; method `card`; operation `escalate`; evidence level `supported`. It cites 3 supporting and 0 contradicting items of the packet's 15.

**The claim** (the rationale, verbatim, split at sentence ends):

1. The incident card indicates a drop in card method authorizations.
2. The card method's authorisation ratio is significantly lower than its baseline, suggesting a degradation at the provider.
3. The merchant's processing metrics are normal, indicating the issue is not with their systems.

**Pointers** (string matching only, not a verdict):

- (the claim states no number, quoted term, clock time or ID)

**The incident card**, not a citable item, exactly as the model received it:

```text
Evidence packet pkt-2
Incident card
Symptom: Fewer orders are completing than usual
Trigger: method_authorization_drop[card]
Cohort: {"merchant_id": "merchant_SYNTHETIC_A", "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]}
Window: 2026-11-06 11:40:00Z to 2026-11-06 12:40:00Z; evidence cutoff 2026-11-06 14:00:00Z
Summary counts: {"orders_with_attempts": 1300, "orders_completed": 1190, "payment_attempts": 1411}
Candidate deployment IDs: deploy_97b08ee5e0
Candidate payment methods: card, netbanking, upi
```

**ev_trigger**, cited as supporting, exactly as the model received it:

```text
[ev_trigger] incident_trigger, known 14:00:00Z: Detector state at the evidence cutoff
facts: {"fired": ["method_authorization_drop[card]"], "not_fired": ["delivery_rejection_spike", "merchant_processing_gap", "method_authorization_drop[netbanking]", "method_authorization_drop[upi]", "orders_not_completing", "unresolved_spike"], "fired_since_open": ["method_authorization_drop[card]", "orders_not_completing"], "first_breach": "12:50:00Z", "reasons": {"delivery_rejection_spike": "too_few_rejections", "merchant_processing_gap": "too_few_pending", "method_authorization_drop[card]": "degraded_in_two_successive_windows", "method_authorization_drop[netbanking]": "insufficient_samples", "method_authorization_drop[upi]": "insufficient_samples", "orders_not_completing": "within_baseline", "unresolved_spike": "within_baseline"}}
```

**ev_metric_card**, cited as supporting, exactly as the model received it:

```text
[ev_metric_card] metric, known 14:00:00Z: Provider authorisation and capture for card, mature cohort
facts: {"method": "card", "mature_cohort_start": "13:20:00Z", "mature_cohort_end": "13:50:00Z", "attempts": 69, "authorised": 34, "captured": 34, "unresolved": 0, "authorisation_ratio": 0.493, "authorisation_ratio_95ci": [0.378, 0.608], "baseline_attempts": 371, "baseline_authorisation_ratio": 0.784, "baseline_95ci": [0.74, 0.823], "share_of_attempts": 0.463, "baseline_share_of_attempts": 0.428}
```

**ev_metric_all**, cited as supporting, exactly as the model received it:

```text
[ev_metric_all] metric, known 14:00:00Z: Provider authorisation and capture for all, mature cohort
facts: {"method": "all", "mature_cohort_start": "13:20:00Z", "mature_cohort_end": "13:50:00Z", "attempts": 149, "authorised": 108, "captured": 108, "unresolved": 0, "authorisation_ratio": 0.725, "authorisation_ratio_95ci": [0.648, 0.79], "baseline_attempts": 867, "baseline_authorisation_ratio": 0.863, "baseline_95ci": [0.838, 0.884], "share_of_attempts": 1.0, "baseline_share_of_attempts": 1.0}
```

**Verdicts for run_b33647f1c5:**

| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |
|---|---|---|---|---|
| ev_trigger | incident_trigger |  |  |  |
| ev_metric_card | metric |  |  |  |
| ev_metric_all | metric |  |  |  |

Sentences of the claim that no cited item supports (numbers, or none): 

---

## 6. run_d40e7cae7e

**The answer:** hypothesis `provider_method_degradation`; faulting change `none`; method `netbanking`; operation `escalate`; evidence level `supported`. It cites 3 supporting and 0 contradicting items of the packet's 15.

**The claim** (the rationale, verbatim, split at sentence ends):

1. The incident card indicates a drop in netbanking authorizations.
2. Metrics show netbanking authorisation ratios dropped significantly compared to baseline.
3. The merchant processing metrics are healthy, suggesting the issue is on the provider side.
4. No evidence suggests a failure in the merchant's processing pipeline.

**Pointers** (string matching only, not a verdict):

- (the claim states no number, quoted term, clock time or ID)

**The incident card**, not a citable item, exactly as the model received it:

```text
Evidence packet pkt-2
Incident card
Symptom: Fewer orders are completing than usual
Trigger: method_authorization_drop[netbanking]
Cohort: {"merchant_id": "merchant_SYNTHETIC_A", "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]}
Window: 2026-11-02 11:50:00Z to 2026-11-02 12:50:00Z; evidence cutoff 2026-11-02 14:00:00Z
Summary counts: {"orders_with_attempts": 1300, "orders_completed": 1161, "payment_attempts": 1396}
Candidate deployment IDs: deploy_da5387f733
Candidate payment methods: card, netbanking, upi
```

**ev_trigger**, cited as supporting, exactly as the model received it:

```text
[ev_trigger] incident_trigger, known 14:00:00Z: Detector state at the evidence cutoff
facts: {"fired": ["method_authorization_drop[netbanking]", "orders_not_completing"], "not_fired": ["delivery_rejection_spike", "merchant_processing_gap", "method_authorization_drop[card]", "method_authorization_drop[upi]", "unresolved_spike"], "fired_since_open": ["method_authorization_drop[netbanking]", "orders_not_completing"], "first_breach": "13:00:00Z", "reasons": {"delivery_rejection_spike": "too_few_rejections", "merchant_processing_gap": "too_few_pending", "method_authorization_drop[card]": "insufficient_samples", "method_authorization_drop[netbanking]": "degraded_in_two_successive_windows", "method_authorization_drop[upi]": "insufficient_samples", "orders_not_completing": "degraded_in_two_successive_windows", "unresolved_spike": "within_baseline"}}
```

**ev_metric_netbanking**, cited as supporting, exactly as the model received it:

```text
[ev_metric_netbanking] metric, known 14:00:00Z: Provider authorisation and capture for netbanking, mature cohort
facts: {"method": "netbanking", "mature_cohort_start": "13:20:00Z", "mature_cohort_end": "13:50:00Z", "attempts": 101, "authorised": 45, "captured": 45, "unresolved": 0, "authorisation_ratio": 0.446, "authorisation_ratio_95ci": [0.352, 0.543], "baseline_attempts": 373, "baseline_authorisation_ratio": 0.796, "baseline_95ci": [0.752, 0.834], "share_of_attempts": 0.526, "baseline_share_of_attempts": 0.437}
```

**ev_provider_state**, cited as supporting, exactly as the model received it:

```text
[ev_provider_state] provider_state, known 14:00:00Z: Provider payment states and snapshot freshness
facts: {"snapshot_age": "1h15m", "observation_coverage": 1.0, "payments_by_status": {"captured": 1161, "failed": 235}, "failed_by_reason": {"authentication_failed": 41, "bank_technical_error": 92, "insufficient_balance": 42, "issuer_unavailable": 60}, "failed_by_method_and_reason": {"card": {"authentication_failed": 13, "bank_technical_error": 13, "insufficient_balance": 8}, "netbanking": {"authentication_failed": 22, "bank_technical_error": 73, "insufficient_balance": 21, "issuer_unavailable": 60}, "upi": {"authentication_failed": 6, "bank_technical_error": 6, "insufficient_balance": 13}}, "failed_by_error_source": {"bank": 152, "customer": 83}, "failed_then_authorised_later": 13, "authorised_not_captured": 0, "captured_without_received_event": 0, "payments_needing_reconciliation": 0, "reconciliation_reasons": {}, "stale_payments": 0, "lookback_start": "08:00:00Z"}
```

**Verdicts for run_d40e7cae7e:**

| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |
|---|---|---|---|---|
| ev_trigger | incident_trigger |  |  |  |
| ev_metric_netbanking | metric |  |  |  |
| ev_provider_state | provider_state |  |  |  |

Sentences of the claim that no cited item supports (numbers, or none): 

---

## 7. run_ed42890977

**The answer:** hypothesis `provider_method_degradation`; faulting change `none`; method `netbanking`; operation `escalate`; evidence level `supported`. It cites 4 supporting and 0 contradicting items of the packet's 12.

**The claim** (the rationale, verbatim, split at sentence ends):

1. The incident card shows a drop in completed orders, but the merchant's processing completion ratio is 100%.
2. The provider's state shows 452 captured payments, which aligns with the completed orders.
3. The netbanking method has a significantly lower authorisation ratio compared to its baseline, suggesting a degradation at the provider.
4. No evidence indicates a failure in the merchant's processing or webhook delivery.

**Pointers** (string matching only, not a verdict):

- sentence 1, percent `100%`: **in a cited item by value only**; the best match by name is not cited: ev_metric_processing: facts.baseline_processing_completion_ratio = 1.0. All matches: ev_metric_processing: facts.baseline_processing_completion_ratio = 1.0 (not cited); ev_metric_processing: facts.processing_completion_ratio = 1.0 (not cited); ev_provider_state: facts.observation_coverage = 1.0 (cited); and 4 more
- sentence 2, number `452`: in a cited item: ev_provider_state: facts.payments_by_status.captured = 452 (cited); incident card: incident.summary_counts.orders_completed = 452 (incident card, not an item)

**The incident card**, not a citable item, exactly as the model received it:

```text
Evidence packet pkt-2
Incident card
Symptom: Fewer orders are completing than usual
Trigger: orders_not_completing
Cohort: {"merchant_id": "merchant_SYNTHETIC_A", "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]}
Window: 2026-11-02 10:50:00Z to 2026-11-02 11:50:00Z; evidence cutoff 2026-11-02 13:00:00Z
Summary counts: {"orders_with_attempts": 600, "orders_completed": 452, "payment_attempts": 610}
Candidate deployment IDs: none in this packet
Candidate payment methods: card, netbanking, upi
```

**ev_trigger**, cited as supporting, exactly as the model received it:

```text
[ev_trigger] incident_trigger, known 13:00:00Z: Detector state at the evidence cutoff
facts: {"fired": ["orders_not_completing"], "not_fired": ["delivery_rejection_spike", "merchant_processing_gap", "method_authorization_drop[card]", "method_authorization_drop[netbanking]", "method_authorization_drop[upi]", "unresolved_spike"], "fired_since_open": ["orders_not_completing"], "first_breach": "12:00:00Z", "reasons": {"delivery_rejection_spike": "too_few_rejections", "merchant_processing_gap": "too_few_pending", "method_authorization_drop[card]": "insufficient_samples", "method_authorization_drop[netbanking]": "insufficient_samples", "method_authorization_drop[upi]": "insufficient_samples", "orders_not_completing": "degraded_in_two_successive_windows", "unresolved_spike": "within_baseline"}}
```

**ev_metric_netbanking**, cited as supporting, exactly as the model received it:

```text
[ev_metric_netbanking] metric, known 13:00:00Z: Provider authorisation and capture for netbanking, mature cohort
facts: {"method": "netbanking", "mature_cohort_start": "12:20:00Z", "mature_cohort_end": "12:50:00Z", "attempts": 54, "authorised": 19, "captured": 19, "unresolved": 0, "authorisation_ratio": 0.352, "authorisation_ratio_95ci": [0.238, 0.485], "baseline_attempts": 120, "baseline_authorisation_ratio": 0.433, "baseline_95ci": [0.348, 0.523], "share_of_attempts": 0.75, "baseline_share_of_attempts": 0.274}
```

**ev_provider_state**, cited as supporting, exactly as the model received it:

```text
[ev_provider_state] provider_state, known 13:00:00Z: Provider payment states and snapshot freshness
facts: {"snapshot_age": "20m00s", "observation_coverage": 1.0, "payments_by_status": {"authorized": 2, "captured": 452, "failed": 156}, "failed_by_reason": {"authentication_failed": 52, "bank_technical_error": 54, "insufficient_balance": 50}, "failed_by_method_and_reason": {"card": {"authentication_failed": 3, "bank_technical_error": 2, "insufficient_balance": 1}, "netbanking": {"authentication_failed": 47, "bank_technical_error": 48, "insufficient_balance": 46}, "upi": {"authentication_failed": 2, "bank_technical_error": 4, "insufficient_balance": 3}}, "failed_by_error_source": {"bank": 54, "customer": 102}, "failed_then_authorised_later": 10, "authorised_not_captured": 2, "captured_without_received_event": 0, "payments_needing_reconciliation": 0, "reconciliation_reasons": {}, "stale_payments": 0, "lookback_start": "07:00:00Z"}
```

**ev_metric_all**, cited as supporting, exactly as the model received it:

```text
[ev_metric_all] metric, known 13:00:00Z: Provider authorisation and capture for all, mature cohort
facts: {"method": "all", "mature_cohort_start": "12:20:00Z", "mature_cohort_end": "12:50:00Z", "attempts": 72, "authorised": 36, "captured": 36, "unresolved": 0, "authorisation_ratio": 0.5, "authorisation_ratio_95ci": [0.387, 0.613], "baseline_attempts": 438, "baseline_authorisation_ratio": 0.815, "baseline_95ci": [0.776, 0.849], "share_of_attempts": 1.0, "baseline_share_of_attempts": 1.0}
```

**Verdicts for run_ed42890977:**

| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |
|---|---|---|---|---|
| ev_trigger | incident_trigger |  |  |  |
| ev_metric_netbanking | metric |  |  |  |
| ev_provider_state | provider_state |  |  |  |
| ev_metric_all | metric |  |  |  |

Sentences of the claim that no cited item supports (numbers, or none): 

---

## 8. run_ef12850923

**The answer:** hypothesis `provider_method_degradation`; faulting change `none`; method `card`; operation `escalate`; evidence level `supported`. It cites 5 supporting and 0 contradicting items of the packet's 11.

**The claim** (the rationale, verbatim, split at sentence ends):

1. The incident card shows a drop in completed orders, but the merchant processing ratio is 100% complete.
2. The provider authorisation and capture metrics for all methods show 0 authorised or captured payments, with all payments unresolved.
3. This indicates that the payments failed at the provider, not the merchant's systems.
4. The delivery health and worker health metrics show no issues with receiving or processing events.

**Pointers** (string matching only, not a verdict):

- sentence 1, percent `100%`: in a cited item: ev_metric_processing: facts.baseline_processing_completion_ratio = 1.0 (cited); ev_metric_processing: facts.processing_completion_ratio = 1.0 (cited); ev_metric_all: facts.baseline_share_of_attempts = 1.0 (cited); and 1 more
- sentence 2, number `0`: in a cited item: ev_metric_all: facts.authorisation_ratio = 0.0 (cited); ev_metric_all: facts.authorisation_ratio_95ci[0] = 0.0 (cited); ev_metric_all: facts.authorised = 0 (cited); and 32 more

**The incident card**, not a citable item, exactly as the model received it:

```text
Evidence packet pkt-2
Incident card
Symptom: Fewer orders are completing than usual
Trigger: orders_not_completing
Cohort: {"merchant_id": "merchant_SYNTHETIC_A", "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]}
Window: 2026-11-03 10:50:00Z to 2026-11-03 11:50:00Z; evidence cutoff 2026-11-03 13:00:00Z
Summary counts: {"orders_with_attempts": 192, "orders_completed": 128, "payment_attempts": 201}
Candidate deployment IDs: none in this packet
Candidate payment methods: card, netbanking, upi
```

**ev_trigger**, cited as supporting, exactly as the model received it:

```text
[ev_trigger] incident_trigger, known 13:00:00Z: Detector state at the evidence cutoff
facts: {"fired": ["orders_not_completing"], "not_fired": ["delivery_rejection_spike", "merchant_processing_gap", "method_authorization_drop[card]", "method_authorization_drop[netbanking]", "method_authorization_drop[upi]", "unresolved_spike"], "fired_since_open": ["orders_not_completing"], "first_breach": "12:00:00Z", "reasons": {"delivery_rejection_spike": "too_few_rejections", "merchant_processing_gap": "too_few_pending", "method_authorization_drop[card]": "insufficient_samples", "method_authorization_drop[netbanking]": "insufficient_samples", "method_authorization_drop[upi]": "insufficient_samples", "orders_not_completing": "degraded_in_two_successive_windows", "unresolved_spike": "insufficient_samples"}}
```

**ev_metric_all**, cited as supporting, exactly as the model received it:

```text
[ev_metric_all] metric, known 13:00:00Z: Provider authorisation and capture for all, mature cohort
facts: {"method": "all", "mature_cohort_start": "12:20:00Z", "mature_cohort_end": "12:50:00Z", "attempts": 30, "authorised": 0, "captured": 0, "unresolved": 30, "authorisation_ratio": 0.0, "authorisation_ratio_95ci": [0.0, 0.114], "baseline_attempts": 137, "baseline_authorisation_ratio": 0.876, "baseline_95ci": [0.81, 0.921], "share_of_attempts": 1.0, "baseline_share_of_attempts": 1.0}
```

**ev_metric_processing**, cited as supporting, exactly as the model received it:

```text
[ev_metric_processing] metric, known 13:00:00Z: Merchant processing of captured payments
facts: {"captured_orders": 32, "processed": 32, "pending_captured": 0, "held_for_review": 0, "pending_captured_value_paise": 0, "processing_completion_ratio": 1.0, "baseline_processing_completion_ratio": 1.0, "grace": "5m00s", "lookback": "1h00m"}
```

**ev_delivery**, cited as supporting, exactly as the model received it:

```text
[ev_delivery] delivery_health, known 13:00:00Z: Webhook delivery health
facts: {"window_start": "07:00:00Z", "delivery_attempts": 275, "accepted": 263, "duplicates": 12, "conflicting_ids": 0, "rejected_signature": 0, "rejected_schema": 0, "rejected_size": 0, "distinct_events_rejected": 0, "first_rejection_ms": null, "accepted_since_first_rejection": null}
```

**ev_worker**, cited as supporting, exactly as the model received it:

```text
[ev_worker] worker_health, known 13:00:00Z: Order worker health
facts: {"inbox_pending": 0, "inbox_failed": 0, "inbox_manual_review": 0, "oldest_unprocessed_age_ms": null, "processed_last_30m": 46, "failed_attempts_last_60m": 0, "last_event_received": "12:59:27Z", "retry_policy": "3 attempts per event, then failed until approved reprocessing"}
```

**Verdicts for run_ef12850923:**

| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |
|---|---|---|---|---|
| ev_trigger | incident_trigger |  |  |  |
| ev_metric_all | metric |  |  |  |
| ev_metric_processing | metric |  |  |  |
| ev_delivery | delivery_health |  |  |  |
| ev_worker | worker_health |  |  |  |

Sentences of the claim that no cited item supports (numbers, or none): 

---

## 9. run_fc1654e2ef

**The answer:** hypothesis `receiver_rejecting_deliveries`; faulting change `deploy_4c9878d04e`; method `none`; operation `prepare_provider_replay`; evidence level `supported`. It cites 5 supporting and 0 contradicting items of the packet's 18.

**The claim** (the rationale, verbatim, split at sentence ends):

1. The incident card indicates a 'merchant_processing_gap' and 'orders_not_completing' due to captured orders not being processed.
2. The 'ev_metric_processing' metric shows 58 captured orders with 0 processed, suggesting the merchant's system failed to process them.
3. The 'ev_fixture' test failed due to a signature verification error, which aligns with the 'ev_log_04' warning about rejected signatures, indicating the webhook receiver is rejecting valid deliveries.

**Pointers** (string matching only, not a verdict):

- sentence 1, quoted `merchant_processing_gap`: in a cited item: ev_trigger: facts.reasons.merchant_processing_gap (key) (cited); ev_trigger: facts.fired[1] (cited); ev_trigger: facts.fired_since_open[1] (cited)
- sentence 1, quoted `orders_not_completing`: in a cited item: ev_trigger: facts.reasons.orders_not_completing (key) (cited); ev_trigger: facts.fired[2] (cited); ev_trigger: facts.fired_since_open[2] (cited)
- sentence 2, evidence id `ev_metric_processing`: cited
- sentence 2, number `58`: in a cited item: ev_metric_processing: facts.captured_orders = 58 (cited); ev_metric_processing: facts.pending_captured = 58 (cited)
- sentence 2, number `0`: in a cited item: ev_metric_processing: facts.held_for_review = 0 (cited); ev_metric_processing: facts.processed = 0 (cited); ev_metric_processing: facts.processing_completion_ratio = 0.0 (cited); and 19 more
- sentence 3, evidence id `ev_fixture`: cited
- sentence 3, evidence id `ev_log_04`: cited

**The incident card**, not a citable item, exactly as the model received it:

```text
Evidence packet pkt-2
Incident card
Symptom: Fewer orders are completing than usual
Trigger: delivery_rejection_spike
Cohort: {"merchant_id": "merchant_SYNTHETIC_A", "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]}
Window: 2026-11-02 10:00:00Z to 2026-11-02 10:30:00Z; evidence cutoff 2026-11-02 11:30:00Z
Summary counts: {"orders_with_attempts": 150, "orders_completed": 73, "payment_attempts": 153}
Candidate deployment IDs: deploy_4c9878d04e, deploy_59d3343303
Candidate payment methods: card, netbanking, upi
```

**ev_trigger**, cited as supporting, exactly as the model received it:

```text
[ev_trigger] incident_trigger, known 11:30:00Z: Detector state at the evidence cutoff
facts: {"fired": ["delivery_rejection_spike", "merchant_processing_gap", "orders_not_completing"], "not_fired": ["method_authorization_drop[card]", "method_authorization_drop[netbanking]", "method_authorization_drop[upi]", "unresolved_spike"], "fired_since_open": ["delivery_rejection_spike", "merchant_processing_gap", "orders_not_completing"], "first_breach": "10:30:00Z", "reasons": {"delivery_rejection_spike": "rejections_high", "merchant_processing_gap": "captured_orders_unprocessed", "method_authorization_drop[card]": "insufficient_samples", "method_authorization_drop[netbanking]": "insufficient_samples", "method_authorization_drop[upi]": "insufficient_samples", "orders_not_completing": "degraded_in_two_successive_windows", "unresolved_spike": "insufficient_samples"}}
```

**ev_metric_processing**, cited as supporting, exactly as the model received it:

```text
[ev_metric_processing] metric, known 11:30:00Z: Merchant processing of captured payments
facts: {"captured_orders": 58, "processed": 0, "pending_captured": 58, "held_for_review": 0, "pending_captured_value_paise": 7976200, "processing_completion_ratio": 0.0, "baseline_processing_completion_ratio": 1.0, "grace": "5m00s", "lookback": "1h00m"}
```

**ev_fixture**, cited as supporting, exactly as the model received it:

```text
[ev_fixture] fixture_check, known 11:30:00Z: Prewritten fixture tests against the deployed version deploy_59d3343303
facts: {"deployed_version": "deploy_59d3343303", "tests_run": 4, "tests_failed": 1, "results": [{"test_id": "captured_payment_builds_fulfilment", "passed": true, "detail": null, "top_frame": null}, {"test_id": "authorized_payment_changes_nothing", "passed": true, "detail": null, "top_frame": null}, {"test_id": "failed_payment_changes_nothing", "passed": true, "detail": null, "top_frame": null}, {"test_id": "signed_delivery_verifies", "passed": false, "detail": "signature verification failed for a delivery signed by the provider", "top_frame": null}]}
```

**ev_log_04**, cited as supporting, exactly as the model received it:

```text
[ev_log_04] log, known 11:29:58Z: warning log from webhook_receiver
facts: {"source": "webhook_receiver", "level": "warning", "occurred_at": "11:29:58Z", "occurrences": 547}
--- untrusted text ev_log_04 ---
webhook delivery answered HTTP 401 (rejected_signature); sender will retry
--- end of untrusted text ev_log_04 ---
```

**ev_delivery**, cited as supporting, exactly as the model received it:

```text
[ev_delivery] delivery_health, known 11:30:00Z: Webhook delivery health
facts: {"window_start": "05:30:00Z", "delivery_attempts": 704, "accepted": 155, "duplicates": 2, "conflicting_ids": 0, "rejected_signature": 547, "rejected_schema": 0, "rejected_size": 0, "distinct_events_rejected": 137, "first_rejection": "10:15:39Z", "accepted_since_first_rejection": 0}
```

**Verdicts for run_fc1654e2ef:**

| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |
|---|---|---|---|---|
| ev_trigger | incident_trigger |  |  |  |
| ev_metric_processing | metric |  |  |  |
| ev_fixture | fixture_check |  |  |  |
| ev_log_04 | log |  |  |  |
| ev_delivery | delivery_health |  |  |  |

Sentences of the claim that no cited item supports (numbers, or none): 

---

## 10. run_fe111b6f00

**The answer:** hypothesis `provider_method_degradation`; faulting change `none`; method `card`; operation `escalate`; evidence level `supported`. It cites 4 supporting and 4 contradicting items of the packet's 13.

**The claim** (the rationale, verbatim, split at sentence ends):

1. The incident card shows a spike in orders not completing, but no method-specific authorization drops were detected.
2. The provider state shows failed payments with various reasons, including customer and gateway issues.
3. The merchant's processing completion ratio is 100%, indicating no failure in processing.
4. The webhook delivery health is strong, suggesting events are received.
5. The order journey shows some payments being authorized and captured, but others failed.
6. This supports the hypothesis that the problem is a degradation in the provider's method performance, not the merchant's system.

**Pointers** (string matching only, not a verdict):

- sentence 3, percent `100%`: in a cited item: ev_metric_processing: facts.baseline_processing_completion_ratio = 1.0 (cited); ev_metric_processing: facts.processing_completion_ratio = 1.0 (cited); ev_metric_all: facts.baseline_share_of_attempts = 1.0 (cited); and 1 more

**The incident card**, not a citable item, exactly as the model received it:

```text
Evidence packet pkt-2
Incident card
Symptom: Fewer orders are completing than usual
Trigger: orders_not_completing
Cohort: {"merchant_id": "merchant_SYNTHETIC_A", "integration_id": "int_SYNTHETIC_MAIN", "methods": ["card", "netbanking", "upi"]}
Window: 2026-11-04 09:50:00Z to 2026-11-04 10:50:00Z; evidence cutoff 2026-11-04 12:00:00Z
Summary counts: {"orders_with_attempts": 360, "orders_completed": 304, "payment_attempts": 373}
Candidate deployment IDs: none in this packet
Candidate payment methods: card, netbanking, upi
```

**ev_metric_all**, cited as supporting, exactly as the model received it:

```text
[ev_metric_all] metric, known 12:00:00Z: Provider authorisation and capture for all, mature cohort
facts: {"method": "all", "mature_cohort_start": "11:20:00Z", "mature_cohort_end": "11:50:00Z", "attempts": 72, "authorised": 49, "captured": 49, "unresolved": 22, "authorisation_ratio": 0.681, "authorisation_ratio_95ci": [0.566, 0.777], "baseline_attempts": 228, "baseline_authorisation_ratio": 0.904, "baseline_95ci": [0.858, 0.935], "share_of_attempts": 1.0, "baseline_share_of_attempts": 1.0}
```

**ev_metric_processing**, cited as supporting, exactly as the model received it:

```text
[ev_metric_processing] metric, known 12:00:00Z: Merchant processing of captured payments
facts: {"captured_orders": 104, "processed": 104, "pending_captured": 0, "held_for_review": 0, "pending_captured_value_paise": 0, "processing_completion_ratio": 1.0, "baseline_processing_completion_ratio": 1.0, "grace": "5m00s", "lookback": "1h00m"}
```

**ev_delivery**, cited as supporting, exactly as the model received it:

```text
[ev_delivery] delivery_health, known 12:00:00Z: Webhook delivery health
facts: {"window_start": "06:00:00Z", "delivery_attempts": 653, "accepted": 643, "duplicates": 10, "conflicting_ids": 0, "rejected_signature": 0, "rejected_schema": 0, "rejected_size": 0, "distinct_events_rejected": 0, "first_rejection_ms": null, "accepted_since_first_rejection": null}
```

**ev_fixture**, cited as supporting, exactly as the model received it:

```text
[ev_fixture] fixture_check, known 12:00:00Z: Prewritten fixture tests against the deployed version baseline
facts: {"deployed_version": "baseline", "tests_run": 4, "tests_failed": 0, "results": [{"test_id": "captured_payment_builds_fulfilment", "passed": true, "detail": null, "top_frame": null}, {"test_id": "authorized_payment_changes_nothing", "passed": true, "detail": null, "top_frame": null}, {"test_id": "failed_payment_changes_nothing", "passed": true, "detail": null, "top_frame": null}, {"test_id": "signed_delivery_verifies", "passed": true, "detail": null, "top_frame": null}]}
```

**ev_trigger**, cited as contradicting, exactly as the model received it:

```text
[ev_trigger] incident_trigger, known 12:00:00Z: Detector state at the evidence cutoff
facts: {"fired": ["orders_not_completing", "unresolved_spike"], "not_fired": ["delivery_rejection_spike", "merchant_processing_gap", "method_authorization_drop[card]", "method_authorization_drop[netbanking]", "method_authorization_drop[upi]"], "fired_since_open": ["orders_not_completing", "unresolved_spike"], "first_breach": "11:00:00Z", "reasons": {"delivery_rejection_spike": "too_few_rejections", "merchant_processing_gap": "too_few_pending", "method_authorization_drop[card]": "insufficient_samples", "method_authorization_drop[netbanking]": "insufficient_samples", "method_authorization_drop[upi]": "insufficient_samples", "orders_not_completing": "degraded_in_two_successive_windows", "unresolved_spike": "unresolved_high_in_two_successive_windows"}}
```

**ev_metric_card**, cited as contradicting, exactly as the model received it:

```text
[ev_metric_card] metric, known 12:00:00Z: Provider authorisation and capture for card, mature cohort
facts: {"method": "card", "mature_cohort_start": "11:20:00Z", "mature_cohort_end": "11:50:00Z", "attempts": 22, "authorised": 15, "captured": 15, "unresolved": 7, "authorisation_ratio": 0.682, "authorisation_ratio_95ci": [0.473, 0.836], "baseline_attempts": 72, "baseline_authorisation_ratio": 0.903, "baseline_95ci": [0.813, 0.952], "share_of_attempts": 0.306, "baseline_share_of_attempts": 0.316}
```

**ev_metric_netbanking**, cited as contradicting, exactly as the model received it:

```text
[ev_metric_netbanking] metric, known 12:00:00Z: Provider authorisation and capture for netbanking, mature cohort
facts: {"method": "netbanking", "mature_cohort_start": "11:20:00Z", "mature_cohort_end": "11:50:00Z", "attempts": 11, "authorised": 5, "captured": 5, "unresolved": 6, "authorisation_ratio": 0.455, "authorisation_ratio_95ci": [0.213, 0.72], "baseline_attempts": 39, "baseline_authorisation_ratio": 0.923, "baseline_95ci": [0.797, 0.973], "share_of_attempts": 0.153, "baseline_share_of_attempts": 0.171}
```

**ev_metric_upi**, cited as contradicting, exactly as the model received it:

```text
[ev_metric_upi] metric, known 12:00:00Z: Provider authorisation and capture for upi, mature cohort
facts: {"method": "upi", "mature_cohort_start": "11:20:00Z", "mature_cohort_end": "11:50:00Z", "attempts": 39, "authorised": 29, "captured": 29, "unresolved": 9, "authorisation_ratio": 0.744, "authorisation_ratio_95ci": [0.589, 0.854], "baseline_attempts": 117, "baseline_authorisation_ratio": 0.897, "baseline_95ci": [0.829, 0.94], "share_of_attempts": 0.542, "baseline_share_of_attempts": 0.513}
```

**Verdicts for run_fe111b6f00:**

| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |
|---|---|---|---|---|
| ev_metric_all | metric |  |  |  |
| ev_metric_processing | metric |  |  |  |
| ev_delivery | delivery_health |  |  |  |
| ev_fixture | fixture_check |  |  |  |

| Cited as contradicting | Kind | Argues against the hypothesis, or for another? (yes / partly / no) | Note |
|---|---|---|---|
| ev_trigger | incident_trigger |  |  |
| ev_metric_card | metric |  |  |
| ev_metric_netbanking | metric |  |  |
| ev_metric_upi | metric |  |  |

Sentences of the claim that no cited item supports (numbers, or none): 

---

## Answer key (read after judging)

From `evaluation/results.json`: each run's family and variant, the oracle's answer, and whether this diagnosis was scored outcome-correct.

| Run | Family and variant | Expected hypothesis | Expected change | Expected method | Outcome correct |
|---|---|---|---|---|---|
| run_6aa1fe76d4 | D4 same_time_contradiction | ambiguous_payment_state | none | none | no |
| run_6c9ea0253e | D2 severe_method_drop | provider_method_degradation | none | card | yes |
| run_7988d37192 | D1 upstream_of_failing_frame | merchant_worker_regression | deploy_a202f09a18 | none | yes |
| run_8dbc8185b6 | D2 moderate_method_drop | provider_method_degradation | none | netbanking | yes |
| run_b33647f1c5 | D6 innocent_deploy_near_method_drop | provider_method_degradation | none | card | yes |
| run_d40e7cae7e | D6 innocent_deploy_near_method_drop | provider_method_degradation | none | netbanking | yes |
| run_ed42890977 | D5 mix_shift | no_incident | none | none | no |
| run_ef12850923 | D5 missing_observations | no_incident | none | none | no |
| run_fc1654e2ef | D3 receiver_code | receiver_rejecting_deliveries | deploy_4c9878d04e | none | yes |
| run_fe111b6f00 | D4 stale_snapshot | ambiguous_payment_state | none | none | no |
