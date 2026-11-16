"""Simulator runtime: deployments, the live merchant composition and the private release tables."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from recovery_workbench import ingestion, storage
from recovery_workbench.contracts import ContractError, ModelMode, PaymentStatus, ReceiveOutcome
from recovery_workbench.simulator import merchant, runtime, signing

RUN = "run_runtime_unit"
INTEGRATION = "int_SYNTHETIC_MAIN"
T0 = 1_793_577_600_000
SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src" / "recovery_workbench"


@pytest.fixture
def conn(tmp_path: Path):
    connection = storage.connect(tmp_path / "runtime.sqlite")
    storage.migrate_simulator(connection)
    yield connection
    connection.close()


def create_run(conn: sqlite3.Connection, run_id: str = RUN, *, emits_categories: bool | None = True) -> None:
    with storage.write_transaction(conn):
        storage.create_run(conn, storage.RunRecord(
            run_id=run_id, seed=1, scenario="unit", split="unit", merchant_id="merchant_SYNTHETIC_A",
            generator_version="unit", clock_ms=T0, model_mode=ModelMode.RULES))
        storage.add_integration(conn, storage.IntegrationRecord(
            run_id=run_id, integration_id=INTEGRATION, secret_ref=merchant.compose().secret_ref, max_body_bytes=65_536))
        if emits_categories is not None:
            storage.set_merchant_profile(conn, run_id=run_id, emits_categories=emits_categories)


def deploy(conn: sqlite3.Connection, variant: str, at_ms: int, deploy_id: str) -> storage.DeploymentRecord:
    return runtime.record_deployment(conn, run_id=RUN, deploy_id=deploy_id, variant=variant, deployed_at_ms=at_ms,
                                     summary=merchant.commit_summaries(variant)[0])


def provider_delivery(conn: sqlite3.Connection, at_ms: int, event_id: str) -> ingestion.DeliveryResponse:
    """A delivery signed the way the simulated provider always signs, through the live receiver."""
    envelope = signing.build_payment_event(
        payment_id="pay_SYNTHETIC_0001", order_id="order_SYNTHETIC_0001", amount_paise=120_000,
        status=PaymentStatus.CAPTURED, occurred_at_ms=at_ms - 100, payment_created_at_ms=T0)
    raw = signing.serialise_event(envelope)
    headers = {signing.EVENT_ID_HEADER: event_id,
               signing.SIGNATURE_HEADER: signing.sign_body(raw, merchant.PROVIDER_SIGNING_REF)}
    receiver = runtime.active_merchant(conn, run_id=RUN, at_ms=at_ms)
    return ingestion.receive_webhook(conn, run_id=RUN, integration_id=INTEGRATION, headers=headers,
                                     raw_body=receiver.body_for_verification(raw), received_at_ms=at_ms)


def test_a_deployment_writes_the_observable_diff_and_the_private_release(conn):
    create_run(conn)
    record = deploy(conn, "payload@2", T0 + 1_000, "deploy_aaaa000001")
    assert (record.service_id, record.kind, record.files_changed) == ("order_worker", "code", ("merchant_app/payload.py",))
    assert record.diff_text.startswith("diff --git a/merchant_app/payload.py b/merchant_app/payload.py")
    assert "+def payment_entity(event):" in record.diff_text
    [stored] = storage.list_deployments(conn, run_id=RUN, as_of_ms=T0 + 1_000)
    assert stored == record
    [release] = storage.list_merchant_releases(conn, run_id=RUN, at_ms=None)
    assert (release.deploy_id, release.module, release.variant) == ("deploy_aaaa000001", "payload", 2)
    assert "payload@2" not in record.diff_text + record.summary  # variant IDs stay private


def test_the_live_composition_and_version_follow_deployment_times(conn):
    create_run(conn)
    deploy(conn, "payload@2", T0 + 1_000, "deploy_aaaa000001")
    deploy(conn, "handler@2", T0 + 5_000, "deploy_aaaa000002")
    assert runtime.active_composition(conn, run_id=RUN, at_ms=T0 + 999) == merchant.BASELINE
    assert runtime.active_merchant(conn, run_id=RUN, at_ms=T0 + 999).version == runtime.BASELINE_VERSION
    middle = runtime.active_merchant(conn, run_id=RUN, at_ms=T0 + 1_000)
    assert (middle.version, middle.composition["payload"], middle.composition["handler"]) == ("deploy_aaaa000001", 2, 1)
    late = runtime.active_merchant(conn, run_id=RUN, at_ms=T0 + 9_000)
    assert (late.version, late.composition["payload"], late.composition["handler"]) == ("deploy_aaaa000002", 2, 2)
    assert late.emits_categories


def test_the_category_profile_controls_exception_categories(conn):
    create_run(conn, emits_categories=False)
    deploy(conn, "payload@2", T0 + 1_000, "deploy_aaaa000001")
    live = runtime.active_merchant(conn, run_id=RUN, at_ms=T0 + 2_000)
    event = signing.build_payment_event(
        payment_id="pay_SYNTHETIC_0001", order_id="order_SYNTHETIC_0001", amount_paise=100,
        status=PaymentStatus.CAPTURED, occurred_at_ms=T0 + 1_500, payment_created_at_ms=T0)
    with pytest.raises(KeyError) as raised:
        live.handle(event)
    report = live.describe_failure(raised.value)
    assert report.exception_category is None
    assert "merchant_app/payload.py" in report.stack_trace


def test_one_deployment_per_module_per_run(conn):
    create_run(conn)
    deploy(conn, "payload@2", T0 + 1_000, "deploy_aaaa000001")
    with pytest.raises(ContractError, match="silently revert"):
        deploy(conn, "payload@4", T0 + 2_000, "deploy_aaaa000002")
    assert len(storage.list_deployments(conn, run_id=RUN, as_of_ms=T0 + 9_000)) == 1
    with storage.write_transaction(conn):  # a second deployment row is allowed on its own ...
        conn.execute("INSERT INTO deployments VALUES (?, 'deploy_x', 'order_worker', 'code', ?, ?, 's', '[]', '')",
                     (RUN, T0 + 3_000, T0 + 3_000))
    with pytest.raises(sqlite3.IntegrityError), storage.write_transaction(conn):  # ... but the database enforces it too
        storage.insert_merchant_release(conn, run_id=RUN, deploy_id="deploy_x", module="payload", variant=4)


def test_deployments_are_recorded_in_time_order(conn):
    create_run(conn)
    deploy(conn, "payload@2", T0 + 5_000, "deploy_aaaa000001")
    with pytest.raises(ContractError, match="time order"):
        deploy(conn, "handler@2", T0 + 4_000, "deploy_aaaa000002")


def test_a_config_deploy_that_switches_the_secret_is_rejected_by_real_ingestion(conn):
    create_run(conn)
    assert provider_delivery(conn, T0 + 500, "evt_SYNTHETIC_0001").outcome is ReceiveOutcome.ACCEPTED
    deploy(conn, "config@3", T0 + 1_000, "deploy_aaaa000001")
    [integration] = storage.list_integrations(conn, run_id=RUN)
    assert integration.secret_ref == "webhook_secret_next"
    rejected = provider_delivery(conn, T0 + 2_000, "evt_SYNTHETIC_0002")
    assert (rejected.outcome, rejected.http_status) == (ReceiveOutcome.REJECTED_SIGNATURE, 401)


def test_a_receiver_code_deploy_rejects_valid_deliveries_before_durable_receipt(conn):
    create_run(conn)
    assert provider_delivery(conn, T0 + 500, "evt_SYNTHETIC_0001").outcome is ReceiveOutcome.ACCEPTED
    deploy(conn, "receiver@2", T0 + 1_000, "deploy_aaaa000001")
    assert provider_delivery(conn, T0 + 2_000, "evt_SYNTHETIC_0002").outcome is ReceiveOutcome.REJECTED_SIGNATURE
    assert len(storage.list_inbox_rows(conn, run_id=RUN)) == 1
    [integration] = storage.list_integrations(conn, run_id=RUN)
    assert integration.secret_ref == "webhook_secret_primary"  # code change only; configuration untouched


def test_the_live_merchant_needs_the_runs_profile(conn):
    create_run(conn, emits_categories=None)
    with pytest.raises(ContractError, match="no merchant profile"):
        runtime.active_merchant(conn, run_id=RUN, at_ms=T0)


def test_only_storage_and_the_runtime_touch_the_private_tables():
    """Read tools, state, metrics, incidents, the CLI and diagnosis never reach simulator state."""
    private_names = re.compile(
        r"sim_merchant_|sim_provider_|list_merchant_releases|get_merchant_profile|provider_secret_at|active_composition"
    )
    allowed = {SOURCE_ROOT / "storage.py", SOURCE_ROOT / "simulator" / "runtime.py"}
    offenders = sorted(
        str(path.relative_to(SOURCE_ROOT))
        for path in SOURCE_ROOT.rglob("*.py")
        if path not in allowed and private_names.search(path.read_text(encoding="utf-8"))
    )
    assert offenders == []
    assert set(storage.PRIVATE_SIMULATOR_TABLES) == {
        "sim_merchant_releases", "sim_merchant_profiles", "sim_provider_signing"}
    assert not set(storage.PRIVATE_SIMULATOR_TABLES) & set(storage.TABLES)


def test_the_release_check_lists_exactly_the_merchant_modules(conn):
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'sim_merchant_releases'").fetchone()[0]
    listed = set(re.findall(r"'([a-z]+)'", re.search(r"module IN \(([^)]*)\)", sql).group(1)))
    assert listed == set(merchant.MODULE_NAMES)
