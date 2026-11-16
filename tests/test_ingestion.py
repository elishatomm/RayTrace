"""Receiver: raw-byte verification, durable inbox, duplicates, conflicting redelivery and rejections."""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest

from conftest import (
    AMOUNT,
    ORDER_1,
    PAY_1,
    PAY_2,
    RUN_A,
    SECRET_A,
    SECRET_B,
    START_MS,
    FailingControlConnection,
    SeededRun,
    business_state,
    count_rows,
    deliver,
    payment_event,
)
from recovery_workbench import ingestion, state, storage, worker
from recovery_workbench.contracts import (
    ContractError,
    ObservationSource,
    PaymentStatus,
    ReceiveOutcome,
    ScopeError,
    canonical_json,
    sha256_hex,
)
from recovery_workbench.simulator import merchant, signing

EVENT_1 = "evt_SYNTHETIC_0001"
# Far past the JSON parser's own nesting limit, which is not sys.getrecursionlimit(), yet 50 kB: under the size cap.
TOO_DEEP = 25_000
MERCHANT_COPY_BREAKS = 600  # an extra field this deep made the merchant handler's copy.deepcopy fail (it did from 481)


def with_extra_fields(fragment: bytes) -> bytes:
    """A provider-shaped event with ``fragment`` (raw ``"name":value`` JSON) added as fields the receiver ignores."""
    return canonical_json(payment_event())[:-1] + b"," + fragment + b"}"


def nested_extra(levels: int, shape: Literal["arrays", "objects"] = "arrays") -> bytes:
    """An event whose ignored ``x_extra`` field nests ``levels`` arrays or objects; the envelope is one level more."""
    inner = b"[" * levels + b"]" * levels if shape == "arrays" else b'{"n":' * levels + b"0" + b"}" * levels
    return with_extra_fields(b'"x_extra":' + inner)


def json_depth(value: object) -> int:
    """How deep arrays and objects nest in a parsed value: an independent count, for shallow test bodies only."""
    if isinstance(value, dict):
        return 1 + max((json_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((json_depth(item) for item in value), default=0)
    return 0


def deepest_nesting_the_parser_reads() -> int:
    """Found by bisection: the C recursion limit (about 9,979 in the project's container), not sys.getrecursionlimit()."""
    reads, fails = 1, 100_000
    while fails - reads > 1:
        middle = (reads + fails) // 2
        try:
            json.loads("[" * middle + "]" * middle)
        except RecursionError:
            fails = middle
        else:
            reads = middle
    return reads


def receive(conn, run: SeededRun, headers: dict[str, str], raw_body: bytes, *, integration_id: str | None = None,
            received_at_ms: int = START_MS + 2_000) -> ingestion.DeliveryResponse:
    return ingestion.receive_webhook(
        conn, run_id=run.run_id, integration_id=integration_id or run.integration_id, headers=headers,
        raw_body=raw_body, received_at_ms=received_at_ms,
    )


def signed_headers(raw_body: bytes, event_id: str = EVENT_1, secret_ref: str = SECRET_A) -> dict[str, str]:
    return {signing.EVENT_ID_HEADER: event_id, signing.SIGNATURE_HEADER: signing.sign_body(raw_body, secret_ref)}


def test_an_accepted_delivery_stores_the_inbox_row_and_observation_together(conn, run_a):
    response = deliver(conn, run_a, EVENT_1, occurred_at_ms=START_MS + 1_234, received_at_ms=START_MS + 1_300)
    assert (response.outcome, response.http_status, response.event_id) == (ReceiveOutcome.ACCEPTED, 200, EVENT_1)
    assert response.acknowledged
    [row] = storage.list_inbox_rows(conn, run_id=RUN_A)
    assert row.inbox_id == response.inbox_id
    assert (row.processing_status, row.attempts, row.signature_result) == ("pending", 0, "verified")
    assert row.event_type == "payment.captured"
    assert row.body_sha256 == sha256_hex(row.raw_body)
    [seen] = storage.list_observations(conn, run_id=RUN_A, as_of_ms=None)
    assert (seen.source, seen.source_ref, seen.status) == (ObservationSource.WEBHOOK, EVENT_1, PaymentStatus.CAPTURED)
    assert (seen.occurred_at_ms, seen.known_at_ms) == (START_MS + 1_234, START_MS + 1_300)
    assert (seen.payment_id, seen.order_id, seen.amount_paise, seen.currency) == (PAY_1, ORDER_1, AMOUNT, "INR")
    [attempt] = storage.list_delivery_attempts(conn, run_id=RUN_A)
    assert (attempt.outcome, attempt.http_status, attempt.attempt_no, attempt.event_id) == (
        ReceiveOutcome.ACCEPTED, 200, 1, EVENT_1
    )


def test_occurrence_time_is_the_envelope_time_not_the_payment_creation_time(conn, run_a):
    deliver(conn, run_a, EVENT_1, occurred_at_ms=START_MS + 61_789)  # payment created at START_MS
    deliver(conn, run_a, "evt_SYNTHETIC_0002", payment_id=PAY_2, occurred_at_ms=START_MS + 61_789,
            include_ms_extension=False)
    by_event = {item.source_ref: item for item in storage.list_observations(conn, run_id=RUN_A, as_of_ms=None)}
    assert by_event[EVENT_1].occurred_at_ms == START_MS + 61_789
    assert by_event["evt_SYNTHETIC_0002"].occurred_at_ms == START_MS + 61_000  # seconds precision only


def test_millisecond_occurrence_keeps_same_second_events_apart(conn, run_a):
    """Failed at .100 s and captured at .600 s of one second: late capture, not a contradiction."""
    deliver(conn, run_a, EVENT_1, status=PaymentStatus.FAILED, occurred_at_ms=START_MS + 1_100)
    deliver(conn, run_a, "evt_SYNTHETIC_0002", occurred_at_ms=START_MS + 1_600)
    precise = state.project_payment(storage.list_observations(conn, run_id=RUN_A, as_of_ms=None), START_MS + 9_000)
    assert precise.status is PaymentStatus.CAPTURED
    assert precise.late_authorization
    assert not precise.needs_reconciliation
    # Without the millisecond extension both land on the same second, which is a conflict.
    deliver(conn, run_a, "evt_SYNTHETIC_0003", payment_id=PAY_2, status=PaymentStatus.FAILED,
            occurred_at_ms=START_MS + 2_100, include_ms_extension=False)
    deliver(conn, run_a, "evt_SYNTHETIC_0004", payment_id=PAY_2, occurred_at_ms=START_MS + 2_600,
            include_ms_extension=False)
    coarse = state.project_payment(
        storage.list_observations(conn, run_id=RUN_A, as_of_ms=None, payment_ids=[PAY_2]), START_MS + 9_000
    )
    assert coarse.conflict_reasons == (state.ConflictReason.SAME_TIME_CONTRADICTION,)


def test_header_names_are_matched_case_insensitively(conn, run_a):
    raw_body = signing.serialise_event(payment_event())
    lower = {key.lower(): value for key, value in signed_headers(raw_body).items()}
    assert receive(conn, run_a, lower, raw_body).outcome is ReceiveOutcome.ACCEPTED
    ambiguous = {**signed_headers(raw_body, "evt_SYNTHETIC_0002"), "x-webhook-signature": "0" * 64}
    assert receive(conn, run_a, ambiguous, raw_body).outcome is ReceiveOutcome.REJECTED_SIGNATURE


def test_repeated_delivery_is_one_logical_receipt(conn, run_a):
    outcomes = [deliver(conn, run_a, EVENT_1) for _ in range(3)]
    assert [item.outcome for item in outcomes] == [
        ReceiveOutcome.ACCEPTED, ReceiveOutcome.DUPLICATE, ReceiveOutcome.DUPLICATE
    ]
    assert all(item.http_status == 200 and item.inbox_id == outcomes[0].inbox_id for item in outcomes)
    assert count_rows(conn, "webhook_inbox", RUN_A) == 1
    assert count_rows(conn, "payment_observations", RUN_A) == 1
    assert [a.attempt_no for a in storage.list_delivery_attempts(conn, run_id=RUN_A)] == [1, 2, 3]


def test_a_reused_event_id_with_different_bytes_is_held_and_acknowledged(conn, run_a):
    original = deliver(conn, run_a, EVENT_1)
    conflicting = deliver(conn, run_a, EVENT_1, status=PaymentStatus.FAILED, amount_paise=AMOUNT + 1)
    assert (conflicting.outcome, conflicting.http_status) == (ReceiveOutcome.CONFLICTING_ID, 200)
    assert conflicting.inbox_id == original.inbox_id
    [held] = storage.list_conflicting_deliveries(conn, run_id=RUN_A, as_of_ms=None)
    assert held.id == conflicting.conflicting_delivery_id
    assert held.differing_fields == ("status", "amount")
    assert (held.payment_id, held.original_payment_id, held.review_status) == (PAY_1, PAY_1, "pending_review")
    [row] = storage.list_inbox_rows(conn, run_id=RUN_A)
    assert held.original_sha256 == row.body_sha256 != held.body_sha256
    assert count_rows(conn, "payment_observations", RUN_A) == 1  # the conflicting body never becomes evidence
    again = deliver(conn, run_a, EVENT_1, status=PaymentStatus.FAILED, amount_paise=AMOUNT + 1)
    assert again.conflicting_delivery_id == held.id
    assert count_rows(conn, "conflicting_deliveries", RUN_A) == 1
    assert count_rows(conn, "delivery_attempts", RUN_A) == 3


def test_differing_fields_name_every_business_field_and_nothing_else(conn, run_a):
    deliver(conn, run_a, EVENT_1)
    deliver(conn, run_a, EVENT_1, payment_id=PAY_2, order_id="order_SYNTHETIC_0009", currency="USD")
    deliver(conn, run_a, EVENT_1, error_reason="retry_note")  # bytes differ, business fields do not
    first, second = storage.list_conflicting_deliveries(conn, run_id=RUN_A, as_of_ms=None)
    assert first.differing_fields == ("payment_id", "currency", "order_id")
    assert second.differing_fields == ()


def _altered_after_signing(raw_body: bytes) -> tuple[dict[str, str], bytes]:
    return signed_headers(raw_body), raw_body.replace(b'"amount":120000', b'"amount":120001')


SIGNATURE_FAILURES: dict[str, Callable[[bytes], tuple[dict[str, str], bytes]]] = {
    "wrong_digest": lambda raw: ({signing.EVENT_ID_HEADER: EVENT_1, signing.SIGNATURE_HEADER: "0" * 64}, raw),
    "missing_signature": lambda raw: ({signing.EVENT_ID_HEADER: EVENT_1}, raw),
    "not_hex": lambda raw: ({signing.EVENT_ID_HEADER: EVENT_1, signing.SIGNATURE_HEADER: "bad"}, raw),
    "other_integration_secret": lambda raw: (signed_headers(raw, secret_ref=SECRET_B), raw),
    "body_altered_after_signing": _altered_after_signing,
}


@pytest.mark.parametrize("case", sorted(SIGNATURE_FAILURES))
def test_a_bad_signature_changes_no_business_state(conn, run_a, case: str):
    deliver(conn, run_a, "evt_SYNTHETIC_0000", payment_id=PAY_2)  # some prior state that must stay as it is
    before = business_state(conn, RUN_A)
    headers, raw_body = SIGNATURE_FAILURES[case](signing.serialise_event(payment_event()))
    response = receive(conn, run_a, headers, raw_body)
    assert (response.outcome, response.http_status, response.inbox_id) == (ReceiveOutcome.REJECTED_SIGNATURE, 401, None)
    assert response.event_id is None  # an unverified header is not trusted as an event ID
    assert business_state(conn, RUN_A) == before
    last = storage.list_delivery_attempts(conn, run_id=RUN_A)[-1]
    assert (last.outcome, last.event_id) == (ReceiveOutcome.REJECTED_SIGNATURE, None)
    assert last.detail


def test_oversize_bodies_are_rejected_before_verification(conn, run_a):
    raw_body = signing.serialise_event(payment_event())
    with storage.write_transaction(conn):
        for name, limit in (("int_exact", len(raw_body)), ("int_short", len(raw_body) - 1)):
            storage.add_integration(conn, storage.IntegrationRecord(
                run_id=RUN_A, integration_id=name, secret_ref=SECRET_A, max_body_bytes=limit))
    headers = signed_headers(raw_body)
    assert receive(conn, run_a, headers, raw_body, integration_id="int_exact").outcome is ReceiveOutcome.ACCEPTED
    short = receive(conn, run_a, headers, raw_body, integration_id="int_short")
    assert (short.outcome, short.http_status) == (ReceiveOutcome.REJECTED_SIZE, 413)
    huge = b"x" * (ingestion.MAX_BODY_BYTES_CEILING + 1)
    assert receive(conn, run_a, headers, huge, integration_id="int_unknown").outcome is ReceiveOutcome.REJECTED_SIZE
    assert count_rows(conn, "webhook_inbox", RUN_A) == 1


def test_the_integration_comes_from_the_runs_own_configuration(conn, run_a, run_b):
    raw_body = signing.serialise_event(payment_event())
    missing = receive(conn, run_a, signed_headers(raw_body), raw_body, integration_id="int_missing")
    assert (missing.outcome, missing.http_status) == (ReceiveOutcome.UNKNOWN_INTEGRATION, 404)
    foreign = receive(conn, run_a, signed_headers(raw_body), raw_body, integration_id=run_b.integration_id)
    assert foreign.outcome is ReceiveOutcome.UNKNOWN_INTEGRATION
    signed_for_b = receive(conn, run_a, signed_headers(raw_body, secret_ref=SECRET_B), raw_body)
    assert signed_for_b.outcome is ReceiveOutcome.REJECTED_SIGNATURE
    assert count_rows(conn, "webhook_inbox", RUN_A) == count_rows(conn, "webhook_inbox", run_b.run_id) == 0
    headers = signed_headers(raw_body)
    with pytest.raises(ScopeError):
        ingestion.receive_webhook(conn, run_id="run_missing", integration_id=run_a.integration_id,
                                  headers=headers, raw_body=raw_body, received_at_ms=START_MS)


def _envelope_with(**changes: object) -> bytes:
    envelope = payment_event()
    entity = envelope["payload"]["payment"]["entity"]  # type: ignore[index]
    for key, value in changes.items():
        if key.startswith("envelope_"):
            envelope[key.removeprefix("envelope_")] = value  # type: ignore[assignment]
        else:
            entity[key] = value  # type: ignore[index]
    return canonical_json(envelope)


def _canonical() -> bytes:
    return signing.serialise_event(payment_event())


MALFORMED_BODIES: dict[str, Callable[[], bytes]] = {
    "not_json": lambda: b"not json",
    "json_array": lambda: b"[1, 2]",
    "invalid_utf8": lambda: b'{"entity": "\xff"}',
    "amount_as_string": lambda: _envelope_with(amount="120000"),
    "amount_as_float": lambda: _envelope_with(amount=1200.0),
    "amount_as_bool": lambda: _envelope_with(amount=True),
    "amount_zero": lambda: _envelope_with(amount=0),
    "amount_too_large": lambda: _envelope_with(amount=10**13),
    "currency_lowercase": lambda: _envelope_with(currency="inr"),
    "status_contradicts_event": lambda: _envelope_with(status="failed"),
    "captured_flag_contradicts_status": lambda: _envelope_with(captured=False),
    "refund_exceeds_amount": lambda: _envelope_with(amount_refunded=AMOUNT + 1),
    "missing_order_id": lambda: _envelope_with(order_id=None),
    "unsupported_event_type": lambda: _envelope_with(envelope_event="order.paid"),
    "contains_other_entities": lambda: _envelope_with(envelope_contains=["payment", "order"]),
    "ms_extension_outside_second": lambda: _envelope_with(envelope_x_synthetic_created_at_ms=START_MS + 9_999),
    "duplicate_keys": lambda: _canonical().replace(b'"amount":120000', b'"amount":120000,"amount":1'),
    "nan_constant": lambda: _canonical().replace(b'"amount":120000', b'"amount":NaN'),
}


@pytest.mark.parametrize("case", sorted(MALFORMED_BODIES))
def test_a_signed_but_malformed_body_is_rejected_without_effect(conn, run_a, case: str):
    raw_body = MALFORMED_BODIES[case]()
    response = receive(conn, run_a, signed_headers(raw_body), raw_body)
    assert (response.outcome, response.http_status, response.event_id) == (ReceiveOutcome.REJECTED_SCHEMA, 400, EVENT_1)
    assert business_state(conn, RUN_A) == {"inbox": 0, "observations": 0, "conflicts": 0, "fulfilments": 0, "orders": []}
    [attempt] = storage.list_delivery_attempts(conn, run_id=RUN_A)
    assert attempt.outcome is ReceiveOutcome.REJECTED_SCHEMA
    assert "120000" not in (attempt.detail or "")  # payload values never enter diagnostic text


def test_a_signed_body_nested_past_the_parsers_limit_is_a_schema_rejection_not_a_crash(
    conn, run_a, monkeypatch: pytest.MonkeyPatch
):
    # The nesting cap refuses this body before the parser reads it. With the cap lifted, the parser's own
    # refusal must still end in a schema rejection: that is the second line behind the cap.
    monkeypatch.setattr(ingestion, "MAX_JSON_NESTING", 2 * TOO_DEEP)
    raw_body = nested_extra(TOO_DEEP)
    with pytest.raises(RecursionError):  # the parser's own refusal, which is not a ValueError
        json.loads(raw_body)
    integration = storage.get_integration(conn, run_id=RUN_A, integration_id=run_a.integration_id)
    assert len(raw_body) <= integration.max_body_bytes  # so the size check lets it through to the parser
    with pytest.raises(ContractError, match="the JSON parser can read"):
        ingestion.parse_payment_event(raw_body)
    response = receive(conn, run_a, signed_headers(raw_body), raw_body)
    assert (response.outcome, response.http_status, response.event_id) == (ReceiveOutcome.REJECTED_SCHEMA, 400, EVENT_1)
    assert business_state(conn, RUN_A) == {"inbox": 0, "observations": 0, "conflicts": 0, "fulfilments": 0, "orders": []}
    [attempt] = storage.list_delivery_attempts(conn, run_id=RUN_A)
    assert attempt.outcome is ReceiveOutcome.REJECTED_SCHEMA
    assert attempt.detail == "webhook body nests deeper than the JSON parser can read"


@pytest.mark.parametrize("shape", ["arrays", "objects"])
def test_nesting_at_the_limit_is_accepted_and_one_level_more_is_a_schema_rejection(
    conn, run_a, shape: Literal["arrays", "objects"]
):
    limit = ingestion.MAX_JSON_NESTING
    at_limit = nested_extra(limit - 1, shape)  # the envelope is the first level
    past_limit = nested_extra(limit, shape)
    assert [json_depth(json.loads(body)) for body in (at_limit, past_limit)] == [limit, limit + 1]
    accepted = receive(conn, run_a, signed_headers(at_limit), at_limit)
    refused = receive(conn, run_a, signed_headers(past_limit, "evt_SYNTHETIC_0002"), past_limit)
    assert (accepted.outcome, accepted.http_status) == (ReceiveOutcome.ACCEPTED, 200)
    assert (refused.outcome, refused.http_status, refused.event_id) == (
        ReceiveOutcome.REJECTED_SCHEMA, 400, "evt_SYNTHETIC_0002"
    )
    last = storage.list_delivery_attempts(conn, run_id=RUN_A)[-1]
    assert last.detail == f"webhook body nests deeper than {limit} levels of arrays and objects"
    assert count_rows(conn, "webhook_inbox", RUN_A) == 1


def test_a_supported_event_nests_four_deep_as_the_limits_comment_says():
    assert json_depth(json.loads(signing.serialise_event(payment_event()))) == 4  # envelope, payload, payment, entity


# Each fragment is added to a valid event as fields the receiver ignores, so only the nesting check can refuse it.
STRING_CASES: dict[str, tuple[bytes, ReceiveOutcome]] = {
    "brackets_in_a_value": (b'"x_note":"' + b"[{" * 100 + b'"', ReceiveOutcome.ACCEPTED),
    "brackets_in_a_key": (b'"' + b"[{" * 100 + b'":1', ReceiveOutcome.ACCEPTED),
    # An escaped quote does not end the string, so the brackets after it are still text.
    "escaped_quotes": (b'"x_note":"\\"' + b"[" * 100 + b'\\"' + b"{" * 100 + b'"', ReceiveOutcome.ACCEPTED),
    # An escaped backslash does not escape the quote after it: that quote ends the string, so the nesting is real.
    "escaped_backslash_then_real_nesting": (
        b'"x_note":"ends in a backslash \\\\","x_extra":' + b"[" * 32 + b"]" * 32, ReceiveOutcome.REJECTED_SCHEMA
    ),
    "brackets_beside_non_ascii_text": (('"x_note":"' + "é→[{" * 100 + '"').encode("utf-8"), ReceiveOutcome.ACCEPTED),
}


@pytest.mark.parametrize("case", sorted(STRING_CASES))
def test_brackets_inside_strings_do_not_count_and_escapes_are_honoured(conn, run_a, case: str):
    fragment, expected = STRING_CASES[case]
    raw_body = with_extra_fields(fragment)
    json.loads(raw_body)  # valid JSON, so the parser alone would accept every case
    assert receive(conn, run_a, signed_headers(raw_body), raw_body).outcome is expected


@pytest.mark.parametrize("depth", ["breaks_the_merchant_copy", "deepest_the_parser_reads"])
def test_a_body_the_parser_reads_but_nested_past_the_limit_is_refused_at_receipt_and_never_reaches_the_worker(
    conn, run_a, depth: str
):
    levels = MERCHANT_COPY_BREAKS if depth == "breaks_the_merchant_copy" else deepest_nesting_the_parser_reads() - 1
    raw_body = nested_extra(levels)  # the envelope is one level more
    json.loads(raw_body)  # the parser itself reads it, so without the cap the receiver accepted it
    response = receive(conn, run_a, signed_headers(raw_body), raw_body)
    assert (response.outcome, response.http_status) == (ReceiveOutcome.REJECTED_SCHEMA, 400)
    assert count_rows(conn, "webhook_inbox", RUN_A) == 0
    assert worker.run_worker(conn, run_id=RUN_A, handler=merchant.compose(), now_ms=START_MS + 9_000) == ()
    assert storage.list_worker_attempts(conn, run_id=RUN_A) == ()  # no merchant failure was recorded either


def test_a_missing_event_id_header_is_a_schema_rejection(conn, run_a):
    raw_body = _canonical()
    headers = {signing.SIGNATURE_HEADER: signing.sign_body(raw_body, SECRET_A)}
    response = receive(conn, run_a, headers, raw_body)
    assert (response.outcome, response.event_id) == (ReceiveOutcome.REJECTED_SCHEMA, None)


def test_thirty_two_concurrent_identical_deliveries_accept_exactly_once(conn, run_a, db_path: Path):
    headers, raw_body = signing.signed_delivery(payment_event(), event_id=EVENT_1, secret_ref=SECRET_A)
    start = threading.Barrier(32)

    def receive_on_own_connection(_: int) -> ReceiveOutcome:
        with closing(storage.connect(db_path)) as own:
            start.wait(timeout=30)
            return receive(own, run_a, headers, raw_body).outcome

    with ThreadPoolExecutor(max_workers=32) as pool:
        outcomes = list(pool.map(receive_on_own_connection, range(32)))
    assert outcomes.count(ReceiveOutcome.ACCEPTED) == 1
    assert outcomes.count(ReceiveOutcome.DUPLICATE) == 31
    assert count_rows(conn, "webhook_inbox", RUN_A) == 1
    assert count_rows(conn, "payment_observations", RUN_A) == 1
    assert sorted(a.attempt_no for a in storage.list_delivery_attempts(conn, run_id=RUN_A)) == list(range(1, 33))


def test_a_failed_commit_is_never_acknowledged(conn, run_a):
    failing = FailingControlConnection(conn, fail_on="COMMIT")
    with pytest.raises(ingestion.ReceiptNotDurableError, match="respond 503"):
        deliver(failing, run_a, EVENT_1)  # type: ignore[arg-type]
    assert business_state(conn, RUN_A) == {"inbox": 0, "observations": 0, "conflicts": 0, "fulfilments": 0, "orders": []}
    assert count_rows(conn, "delivery_attempts", RUN_A) == 0  # the attempt row rolled back with the receipt
    assert deliver(conn, run_a, EVENT_1).outcome is ReceiveOutcome.ACCEPTED


def test_the_same_event_id_on_two_integrations_is_two_events(conn, run_a):
    with storage.write_transaction(conn):
        storage.add_integration(conn, storage.IntegrationRecord(
            run_id=RUN_A, integration_id="int_SYNTHETIC_SECOND", secret_ref=SECRET_B, max_body_bytes=65_536))
    assert deliver(conn, run_a, EVENT_1).outcome is ReceiveOutcome.ACCEPTED
    second = deliver(conn, run_a, EVENT_1, integration_id="int_SYNTHETIC_SECOND", secret_ref=SECRET_B)
    assert second.outcome is ReceiveOutcome.ACCEPTED
    assert count_rows(conn, "webhook_inbox", RUN_A) == 2
