"""Which merchant code is live in each run (private simulator state).

A deployment has two halves:

* observable: a ``deployments`` row (service, kind, summary, changed files and the code
  diff against the composition live just before). Tools and the model may read it.
* private: a ``sim_merchant_releases`` row saying which module variant went live, plus
  the run's ``sim_merchant_profiles`` row. Only this module reads them; read tools, the
  evidence packet and the model never do (``storage.PRIVATE_SIMULATOR_TABLES``).

Recovery code receives a ``DeployedMerchant`` from its caller (dependency injection); the
CLI and API obtain it here with ``active_merchant``. The one exception is the receiver's
verification secrets: the deployed config names them, so ``record_deployment`` writes the
current one to the integration, and ``ingestion`` asks ``staged_secret_ref`` for a staged
next one, because its callers pass it no merchant.
"""
from __future__ import annotations

import sqlite3
from functools import lru_cache
from types import MappingProxyType

from recovery_workbench import storage
from recovery_workbench.contracts import ContractError
from recovery_workbench.simulator import merchant

BASELINE_VERSION = "baseline"

SERVICE_BY_MODULE = MappingProxyType(
    {
        "payload": "order_worker",
        "orders": "order_worker",
        "handler": "order_worker",
        "receiver": "webhook_receiver",
        "config": "merchant_config",
        "checkout": "storefront",
        "notifications": "storefront",
    }
)


def record_deployment(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    deploy_id: str,
    variant: str,
    deployed_at_ms: int,
    summary: str,
    known_at_ms: int | None = None,
) -> storage.DeploymentRecord:
    """Deploy one whole-file module variant (``payload@2``) in one write transaction.

    Writes the observable deployment with its diff, the private release row and, when
    the deployed config verifies with another secret, the integration's ``secret_ref``,
    so a receiver configuration change is rejected by the real ingestion code.
    """
    module, number = merchant.parse_variant_id(variant)
    with storage.write_transaction(conn):
        storage.require_run(conn, run_id)
        releases = storage.list_merchant_releases(conn, run_id=run_id, at_ms=None)
        if any(release.module == module for release in releases):
            raise ContractError(
                f"run {run_id!r} already deployed a variant of {module!r}; variants are whole files, so a second "
                "deploy of the same module would silently revert the first. Use one deployment per module per run"
            )
        latest = storage.latest_deployment(conn, run_id=run_id)
        if latest is not None and deployed_at_ms <= latest.deployed_at_ms:
            raise ContractError(
                f"deployment {deploy_id!r} at {deployed_at_ms} ms is not after {latest.deploy_id!r} at "
                f"{latest.deployed_at_ms} ms; record deployments in time order so each diff has one predecessor"
            )
        before = _composition(releases)
        after = merchant.resolve_composition({**before, module: number})
        record = storage.DeploymentRecord(
            run_id=run_id,
            deploy_id=deploy_id,
            service_id=SERVICE_BY_MODULE[module],
            kind="config" if module == "config" else "code",
            deployed_at_ms=deployed_at_ms,
            known_at_ms=known_at_ms if known_at_ms is not None else deployed_at_ms,
            summary=summary,
            files_changed=merchant.changed_files(before, after),
            diff_text=merchant.composition_diff(before, after),
        )
        storage.insert_deployment(conn, record)
        storage.insert_merchant_release(conn, run_id=run_id, deploy_id=deploy_id, module=module, variant=number)
        _follow_secret_change(conn, run_id=run_id, before=before, after=after)
    return record


def _follow_secret_change(
    conn: sqlite3.Connection, *, run_id: str, before: merchant.Composition, after: merchant.Composition
) -> None:
    old_ref = merchant.compose(before).secret_ref
    new_ref = merchant.compose(after).secret_ref
    if new_ref == old_ref:
        return
    for integration in storage.list_integrations(conn, run_id=run_id):
        if integration.secret_ref == old_ref:
            storage.update_integration_secret_ref(
                conn, run_id=run_id, integration_id=integration.integration_id, secret_ref=new_ref
            )


def _composition(releases: tuple[storage.MerchantRelease, ...]) -> merchant.Composition:
    return merchant.resolve_composition({release.module: release.variant for release in releases})


def staged_secret_ref(conn: sqlite3.Connection, *, run_id: str, secret_ref: str, at_ms: int) -> str | None:
    """The next signing secret staged at ``at_ms`` for an integration that verifies with ``secret_ref``, or None.

    The merchant config deployed at ``at_ms`` stages it (config@6's ``NEXT_WEBHOOK_SECRET_REF``). It counts only for
    an integration that verifies with that config's own secret (the ones ``_follow_secret_change`` moves when a config
    deploy changes it), and only while it differs from that secret; a promoting config such as config@3 stages
    nothing. A database without the simulator's private tables has never deployed merchant code, so the baseline
    config, which stages nothing, is live there.
    """
    has_releases = _has_table(conn, "sim_merchant_releases")
    releases = storage.list_merchant_releases(conn, run_id=run_id, at_ms=at_ms) if has_releases else ()
    current, staged = _verification_refs(tuple(sorted(_composition(releases).items())))
    if staged is None or current != secret_ref or staged == secret_ref:
        return None
    return staged


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    """Only generator databases carry the simulator's private tables (``storage.migrate_simulator``)."""
    found = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
    return found is not None


@lru_cache(maxsize=256)
def _verification_refs(composition: tuple[tuple[str, int], ...]) -> tuple[str, str | None]:
    # Composing executes every module once; the secrets a composition names never change, so they are reused.
    deployed = merchant.compose(dict(composition))
    return deployed.secret_ref, deployed.staged_secret_ref


def active_composition(conn: sqlite3.Connection, *, run_id: str, at_ms: int) -> merchant.Composition:
    """The ``{module: variant}`` composition live in the run at ``at_ms``."""
    storage.require_run(conn, run_id)
    return _composition(storage.list_merchant_releases(conn, run_id=run_id, at_ms=at_ms))


def active_merchant(conn: sqlite3.Connection, *, run_id: str, at_ms: int) -> merchant.DeployedMerchant:
    """The deployed merchant handler at ``at_ms``.

    Its version is the latest deploy ID at or before ``at_ms`` (``baseline`` before the
    first one). It carries the run's category profile, and the secret the simulated provider
    signs with at ``at_ms``, which the receiver fixture check signs with.
    """
    storage.require_run(conn, run_id)
    emits_categories = storage.get_merchant_profile(conn, run_id=run_id)
    if emits_categories is None:
        raise ContractError(f"run {run_id!r} has no merchant profile; the generator sets one when it creates the run")
    releases = storage.list_merchant_releases(conn, run_id=run_id, at_ms=at_ms)
    version = releases[-1].deploy_id if releases else BASELINE_VERSION
    overrides = tuple(sorted((release.module, release.variant) for release in releases))
    return _deployed(overrides, emits_categories, version, _provider_secret(conn, run_id=run_id, at_ms=at_ms))


def _provider_secret(conn: sqlite3.Connection, *, run_id: str, at_ms: int) -> str:
    """The secret the simulated provider signs with at ``at_ms``: its latest rotation by then, else its first secret.

    A generator database not migrated since rotations were recorded has no such table, and no rotation recorded.
    """
    has_rotations = _has_table(conn, "sim_provider_signing")
    rotated = storage.provider_secret_at(conn, run_id=run_id, at_ms=at_ms) if has_rotations else None
    return merchant.PROVIDER_SIGNING_REF if rotated is None else rotated


@lru_cache(maxsize=256)
def _deployed(
    overrides: tuple[tuple[str, int], ...], emits_categories: bool, version: str, provider_signing_ref: str
) -> merchant.DeployedMerchant:
    # Composing executes every module once; a deployed merchant is immutable, so it is reused.
    return merchant.compose(dict(overrides), emits_categories=emits_categories, version=version,
                            provider_signing_ref=provider_signing_ref)
