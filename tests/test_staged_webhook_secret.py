"""During a provider secret rotation the receiver also accepts the next secret the merchant staged.

The merchant config live at a delivery's receipt stages it (config@6's ``NEXT_WEBHOOK_SECRET_REF``). The receiver
accepts it only for an integration that config verifies with, only while it is staged, and records in the delivery
attempt's ``detail`` that the staged secret verified the delivery. It accepts no other secret. Seeds 19601-19700 are
reserved for Q22 and Q25 (agent B); this file uses 19601.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest
from test_webhook_conformance import BUSY

from recovery_workbench import ingestion, storage
from recovery_workbench.contracts import ModelMode, PaymentStatus, ReceiveOutcome, sha256_hex
from recovery_workbench.simulator import driver, merchant, runtime, signing
from recovery_workbench.simulator.scenarios import Deploy, LostDeliveries, SecretRotation, WebhookConformance
from recovery_workbench.simulator.world import ROTATED_SIGNING_REF, ROTATION_CONFIG_VARIANT, generate_world

RUN = "run_staged_secret_unit"
MAIN = "int_SYNTHETIC_MAIN"
OTHER = "int_SYNTHETIC_OTHER"  # verified with a fixture secret no merchant config names
T0 = 1_793_577_600_000
CURRENT = merchant.PROVIDER_SIGNING_REF  # what the baseline config verifies with
NEXT = ROTATED_SIGNING_REF
STAGING_CONFIG = "config@6"  # verifies with CURRENT and stages NEXT
PROMOTING_CONFIG = ROTATION_CONFIG_VARIANT  # config@3: verifies with NEXT and stages nothing
STAGED_NOTE = f"verified with the staged next secret {NEXT}"
MISMATCH = "signature does not match the raw body"
STAGED_ROTATION_SEED = 19601


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A generator database: the merchant's deployments live in the simulator's private tables."""
    with closing(storage.connect(tmp_path / "staged.sqlite")) as connection:
        storage.migrate_simulator(connection)
        with storage.write_transaction(connection):
            storage.create_run(connection, storage.RunRecord(
                run_id=RUN, seed=1, scenario="unit", split="unit", merchant_id="merchant_SYNTHETIC_A",
                generator_version="unit", clock_ms=T0, model_mode=ModelMode.RULES))
            for integration_id, secret_ref in ((MAIN, merchant.compose().secret_ref), (OTHER, "fixture_secret_a")):
                storage.add_integration(connection, storage.IntegrationRecord(
                    run_id=RUN, integration_id=integration_id, secret_ref=secret_ref, max_body_bytes=65_536))
            storage.set_merchant_profile(connection, run_id=RUN, emits_categories=True)
        yield connection


def deploy(conn: sqlite3.Connection, variant: str, at_ms: int) -> None:
    runtime.record_deployment(conn, run_id=RUN, deploy_id=f"deploy_staged_{at_ms}", variant=variant,
                              deployed_at_ms=at_ms, summary=merchant.commit_summaries(variant)[0])


def signed_body(number: int, at_ms: int) -> bytes:
    """One distinct captured-payment body per number, so no two deliveries here share their bytes."""
    envelope = signing.build_payment_event(
        payment_id=signing.synthetic_id("pay", number), order_id=signing.synthetic_id("order", number),
        amount_paise=120_000, status=PaymentStatus.CAPTURED, occurred_at_ms=at_ms - 100, payment_created_at_ms=T0)
    return signing.serialise_event(envelope)


def receive(
    conn: sqlite3.Connection, raw_body: bytes, *, signed_with: str, at_ms: int, event_id: str, integration: str = MAIN,
) -> tuple[ingestion.DeliveryResponse, storage.DeliveryAttemptRecord]:
    """One delivery signed with ``signed_with``, through the receiver deployed at ``at_ms``, and the row it left."""
    headers = {signing.EVENT_ID_HEADER: event_id, signing.SIGNATURE_HEADER: signing.sign_body(raw_body, signed_with)}
    receiver = runtime.active_merchant(conn, run_id=RUN, at_ms=at_ms)
    response = ingestion.receive_webhook(conn, run_id=RUN, integration_id=integration, headers=headers,
                                         raw_body=receiver.body_for_verification(raw_body), received_at_ms=at_ms)
    return response, storage.list_delivery_attempts(conn, run_id=RUN)[-1]


def outcome(response: ingestion.DeliveryResponse, attempt: storage.DeliveryAttemptRecord) -> tuple[object, ...]:
    return response.outcome, response.http_status, attempt.outcome, attempt.detail


def stored(conn: sqlite3.Connection) -> tuple[int, int]:
    """Inbox rows and payment observations: what an accepted delivery adds and a rejected one never does."""
    return (len(storage.list_inbox_rows(conn, run_id=RUN)),
            len(storage.list_observations(conn, run_id=RUN, as_of_ms=None)))


def test_the_fixture_configs_name_the_secrets_this_file_relies_on() -> None:
    staging, promoting = (merchant.compose(dict([merchant.parse_variant_id(variant)]))
                          for variant in (STAGING_CONFIG, PROMOTING_CONFIG))
    assert (merchant.compose().secret_ref, merchant.compose().staged_secret_ref) == (CURRENT, None)
    assert (staging.secret_ref, staging.staged_secret_ref) == (CURRENT, NEXT)
    assert (promoting.secret_ref, promoting.staged_secret_ref) == (NEXT, None)


# --------------------------------------------------------------------------- the receiver


def test_while_the_next_secret_is_staged_both_secrets_verify_and_the_staged_one_is_recorded(conn) -> None:
    early = receive(conn, signed_body(1, T0 + 500), signed_with=NEXT, at_ms=T0 + 500, event_id="evt_SYNTHETIC_0001")
    assert outcome(*early) == (ReceiveOutcome.REJECTED_SIGNATURE, 401, ReceiveOutcome.REJECTED_SIGNATURE, MISMATCH)
    deploy(conn, STAGING_CONFIG, T0 + 1_000)
    [integration, _] = storage.list_integrations(conn, run_id=RUN)
    assert integration.secret_ref == CURRENT  # staging moves nothing; only a promoting config deploy would
    current = receive(conn, signed_body(2, T0 + 2_000), signed_with=CURRENT, at_ms=T0 + 2_000,
                      event_id="evt_SYNTHETIC_0002")
    assert outcome(*current) == (ReceiveOutcome.ACCEPTED, 200, ReceiveOutcome.ACCEPTED, None)
    staged_body = signed_body(3, T0 + 3_000)
    staged = receive(conn, staged_body, signed_with=NEXT, at_ms=T0 + 3_000, event_id="evt_SYNTHETIC_0003")
    assert outcome(*staged) == (ReceiveOutcome.ACCEPTED, 200, ReceiveOutcome.ACCEPTED, STAGED_NOTE)
    assert stored(conn) == (2, 2)
    [row] = [row for row in storage.list_inbox_rows(conn, run_id=RUN) if row.event_id == "evt_SYNTHETIC_0003"]
    assert row.body_sha256 == sha256_hex(staged_body)
    # Every answer a staged signature earns says which secret verified it: a retry, and a resend under a new ID.
    retry = receive(conn, staged_body, signed_with=NEXT, at_ms=T0 + 4_000, event_id="evt_SYNTHETIC_0003")
    assert outcome(*retry) == (ReceiveOutcome.DUPLICATE, 200, ReceiveOutcome.DUPLICATE, STAGED_NOTE)
    resend = receive(conn, staged_body, signed_with=NEXT, at_ms=T0 + 5_000, event_id="evt_SYNTHETIC_0004")
    assert outcome(*resend) == (
        ReceiveOutcome.DUPLICATE, 200, ReceiveOutcome.DUPLICATE,
        f"body already received as event evt_SYNTHETIC_0003 (inbox row {row.inbox_id}); {STAGED_NOTE}",
    )
    assert stored(conn) == (2, 2)


def test_without_a_staged_secret_a_next_secret_signature_is_refused(conn) -> None:
    body = signed_body(1, T0 + 500)
    baseline = receive(conn, body, signed_with=NEXT, at_ms=T0 + 500, event_id="evt_SYNTHETIC_0001")
    assert outcome(*baseline) == (ReceiveOutcome.REJECTED_SIGNATURE, 401, ReceiveOutcome.REJECTED_SIGNATURE, MISMATCH)
    deploy(conn, "config@4", T0 + 1_000)  # another config setting changes; this config stages nothing either
    other_config = receive(conn, body, signed_with=NEXT, at_ms=T0 + 2_000, event_id="evt_SYNTHETIC_0001")
    assert outcome(*other_config) == (
        ReceiveOutcome.REJECTED_SIGNATURE, 401, ReceiveOutcome.REJECTED_SIGNATURE, MISMATCH)
    assert stored(conn) == (0, 0)


def test_while_a_secret_is_staged_any_other_secret_or_a_malformed_signature_is_refused(conn) -> None:
    deploy(conn, STAGING_CONFIG, T0 + 1_000)
    body = signed_body(1, T0 + 2_000)
    for number, wrong in enumerate(("fixture_secret_a", "fixture_secret_b"), start=1):
        refused = receive(conn, body, signed_with=wrong, at_ms=T0 + 2_000 + number, event_id="evt_SYNTHETIC_0001")
        assert outcome(*refused) == (ReceiveOutcome.REJECTED_SIGNATURE, 401, ReceiveOutcome.REJECTED_SIGNATURE,
                                     MISMATCH), wrong
    forged = {signing.EVENT_ID_HEADER: "evt_SYNTHETIC_0001", signing.SIGNATURE_HEADER: "ab" * 31}
    response = ingestion.receive_webhook(conn, run_id=RUN, integration_id=MAIN, headers=forged, raw_body=body,
                                         received_at_ms=T0 + 3_000)
    assert (response.outcome, storage.list_delivery_attempts(conn, run_id=RUN)[-1].detail) == (
        ReceiveOutcome.REJECTED_SIGNATURE, "signature header is not 64 hexadecimal characters")
    assert stored(conn) == (0, 0)


def test_the_staged_secret_counts_only_for_an_integration_its_config_verifies(conn) -> None:
    deploy(conn, STAGING_CONFIG, T0 + 1_000)
    body = signed_body(1, T0 + 2_000)
    refused = receive(conn, body, signed_with=NEXT, at_ms=T0 + 2_000, event_id="evt_SYNTHETIC_0001", integration=OTHER)
    assert outcome(*refused) == (ReceiveOutcome.REJECTED_SIGNATURE, 401, ReceiveOutcome.REJECTED_SIGNATURE, MISMATCH)
    own = receive(conn, body, signed_with="fixture_secret_a", at_ms=T0 + 3_000, event_id="evt_SYNTHETIC_0001",
                  integration=OTHER)
    assert outcome(*own) == (ReceiveOutcome.ACCEPTED, 200, ReceiveOutcome.ACCEPTED, None)


def test_a_promoting_config_deploy_makes_the_receiver_refuse_the_old_secret(conn) -> None:
    deploy(conn, PROMOTING_CONFIG, T0 + 1_000)  # the receiver now verifies with NEXT, and nothing is staged
    assert [item.secret_ref for item in storage.list_integrations(conn, run_id=RUN)] == [NEXT, "fixture_secret_a"]
    old = receive(conn, signed_body(1, T0 + 2_000), signed_with=CURRENT, at_ms=T0 + 2_000,
                  event_id="evt_SYNTHETIC_0001")
    assert outcome(*old) == (ReceiveOutcome.REJECTED_SIGNATURE, 401, ReceiveOutcome.REJECTED_SIGNATURE, MISMATCH)
    new = receive(conn, signed_body(2, T0 + 3_000), signed_with=NEXT, at_ms=T0 + 3_000, event_id="evt_SYNTHETIC_0002")
    assert outcome(*new) == (ReceiveOutcome.ACCEPTED, 200, ReceiveOutcome.ACCEPTED, None)  # the current secret now


def test_once_the_integration_verifies_with_the_staged_secret_the_old_one_is_refused(conn) -> None:
    deploy(conn, STAGING_CONFIG, T0 + 1_000)
    # A run takes one config deploy, so the integration is moved the way a promoting deploy's _follow_secret_change
    # would move it. The staging config is still live, but it no longer names the integration's secret.
    with storage.write_transaction(conn):
        storage.update_integration_secret_ref(conn, run_id=RUN, integration_id=MAIN, secret_ref=NEXT)
    old = receive(conn, signed_body(1, T0 + 2_000), signed_with=CURRENT, at_ms=T0 + 2_000,
                  event_id="evt_SYNTHETIC_0001")
    assert outcome(*old) == (ReceiveOutcome.REJECTED_SIGNATURE, 401, ReceiveOutcome.REJECTED_SIGNATURE, MISMATCH)
    new = receive(conn, signed_body(2, T0 + 3_000), signed_with=NEXT, at_ms=T0 + 3_000, event_id="evt_SYNTHETIC_0002")
    assert outcome(*new) == (ReceiveOutcome.ACCEPTED, 200, ReceiveOutcome.ACCEPTED, None)


def test_an_application_database_without_simulator_tables_stages_nothing(tmp_path: Path) -> None:
    with closing(storage.connect(tmp_path / "application.sqlite")) as application:
        storage.migrate(application)
        with storage.write_transaction(application):
            storage.create_run(application, storage.RunRecord(
                run_id=RUN, seed=1, scenario="unit", split="unit", merchant_id="merchant_SYNTHETIC_A",
                generator_version="unit", clock_ms=T0, model_mode=ModelMode.RULES))
            storage.add_integration(application, storage.IntegrationRecord(
                run_id=RUN, integration_id=MAIN, secret_ref=CURRENT, max_body_bytes=65_536))
        assert runtime.staged_secret_ref(application, run_id=RUN, secret_ref=CURRENT, at_ms=T0 + 1_000) is None
        body = signed_body(1, T0 + 1_000)
        headers = {signing.EVENT_ID_HEADER: "evt_SYNTHETIC_0001",
                   signing.SIGNATURE_HEADER: signing.sign_body(body, NEXT)}
        response = ingestion.receive_webhook(application, run_id=RUN, integration_id=MAIN, headers=headers,
                                             raw_body=body, received_at_ms=T0 + 1_000)
        assert (response.outcome, response.http_status) == (ReceiveOutcome.REJECTED_SIGNATURE, 401)


# --------------------------------------------------------------------------- a rotation the merchant staged


def test_a_rotation_the_merchant_staged_beforehand_loses_no_delivery_to_a_401(tmp_path: Path) -> None:
    """P12a's rotation world, except that the merchant staged the next secret (config@6) before the provider rotated.

    In P12a's coordinated rotation (config@3 at T_r) the old-signature retries of in-flight events were rejected 40
    times. Here those retries verify with the current secret, and events signed with the next secret verify with the
    staged one, which their delivery attempts record.
    """
    spec = BUSY.model_copy(update={"harmless": (Deploy(variant=STAGING_CONFIG, at_fraction=0.25),)})
    overlay = WebhookConformance(
        secret_rotation=SecretRotation(at_fraction=0.5),  # the provider rotates; the merchant deploys nothing at T_r
        lost_deliveries=LostDeliveries(start_fraction=0.47, end_fraction=0.5, rate=1.0, lost="request"),
    )
    world = generate_world(spec, STAGED_ROTATION_SEED, conformance=overlay)
    rotation_at = world.signing_rotation_at_ms
    assert rotation_at is not None
    with closing(storage.connect(tmp_path / "workbench.sqlite")) as conn:
        replayed = driver.replay_world(conn, world, oracle_dir=None)
        run_id = world.run_id
        [staging] = storage.list_deployments(conn, run_id=run_id, as_of_ms=world.cutoff_ms)
        assert staging.deployed_at_ms < rotation_at
        assert staging.files_changed == ("merchant_app/config.py",)
        assert [item.secret_ref for item in storage.list_integrations(conn, run_id=run_id)] == [CURRENT]
        attempts = storage.list_delivery_attempts(conn, run_id=run_id)
        assert [row for row in attempts if row.outcome is not ReceiveOutcome.ACCEPTED] == []  # no 401, no duplicate
        # Each accepted attempt says which secret verified it, and that is the secret the provider signed it with.
        signed_with = {(item.event_id, item.at_ms): item.secret_ref
                       for plan in world.deliveries for item in replayed.provider_attempts(plan.event_id)}
        expected_detail = {CURRENT: None, NEXT: STAGED_NOTE}
        assert all(row.detail == expected_detail[signed_with[(row.event_id, row.received_at_ms)]] for row in attempts)
        staged_count = sum(row.detail == STAGED_NOTE for row in attempts)
        assert staged_count > 100
        # The in-flight events P12a lost to 401s: first attempted before T_r, retried after it with the old signature.
        crossing = [plan.event_id for plan in world.deliveries
                    if any(item.at_ms >= rotation_at and item.secret_ref == CURRENT
                           for item in replayed.provider_attempts(plan.event_id))]
        assert len(crossing) >= 5
        for event_id in crossing:
            record = replayed.provider_attempts(event_id)
            assert [item.http_status for item in record] == [None, 200], event_id  # lost, then accepted at once
        inbox_events = [row.event_id for row in storage.list_inbox_rows(conn, run_id=run_id)]
        assert len(inbox_events) == len(set(inbox_events)) == len(attempts)
