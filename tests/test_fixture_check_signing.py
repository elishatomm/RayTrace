"""The receiver fixture check signs as the provider signs at the check's time, and verifies as the receiver does.

``signed_delivery_verifies`` signed every delivery with ``merchant.PROVIDER_SIGNING_REF``, the provider's key
before any rotation. After a coordinated rotation (the provider and config@3 both on the next key) it therefore
reported a failure that was not there, and both rules versions blamed the rotation's own config deploy with
``reproduced_in_fixture``. Now the deployed merchant carries the secret the provider signs with while it is live
(``DeployedMerchant.provider_signing_ref``, set by ``runtime.active_merchant``), and the check verifies with the
deployed config's secret or the next one it stages, as ``ingestion.receive_webhook`` does since Q22.
"""
from __future__ import annotations

import pytest

from recovery_workbench.contracts import ContractError
from recovery_workbench.simulator import merchant
from recovery_workbench.simulator.world import ROTATED_SIGNING_REF

RECEIVER_TEST = "signed_delivery_verifies"
OLD = merchant.PROVIDER_SIGNING_REF
NEXT = ROTATED_SIGNING_REF
CONFIGS = {"baseline": {}, "config@3": {"config": 3}, "config@5": {"config": 5}, "config@6": {"config": 6}}


def receiver_check(config: str, provider_signs_with: str | None = None) -> merchant.FixtureCheckResult:
    signed_with = {} if provider_signs_with is None else {"provider_signing_ref": provider_signs_with}
    return merchant.run_fixture_check(RECEIVER_TEST, merchant.compose(CONFIGS[config], **signed_with))


def test_by_default_the_provider_signs_with_its_first_secret_and_d3s_config_change_is_reproduced() -> None:
    assert merchant.compose().provider_signing_ref == OLD
    assert receiver_check("baseline").passed
    failed = receiver_check("config@3")  # D3: verification moved to the next key, and the provider never rotated
    assert (failed.passed, failed.actual, failed.detail) == (
        False, {"verified": False}, "signature verification failed for a delivery signed by the provider")


@pytest.mark.parametrize(("config", "provider_signs_with", "verified"), [
    ("baseline", OLD, True),
    ("baseline", NEXT, False),  # the provider rotated and the merchant did not: a real failure, reproduced
    ("config@3", OLD, False),  # the merchant moved alone (D3)
    ("config@3", NEXT, True),  # both moved: a coordinated rotation, nothing to reproduce
    ("config@6", OLD, True),  # the staging config verifies with the current secret ...
    ("config@6", NEXT, True),  # ... and with the next one it stages
    ("config@6", "fixture_secret_a", False),  # and with nothing else
])
def test_the_check_signs_with_the_providers_secret_and_verifies_with_the_current_or_staged_one(
    config: str, provider_signs_with: str, verified: bool
) -> None:
    result = receiver_check(config, provider_signs_with)
    assert (result.passed, result.actual) == (verified, {"verified": verified})


def test_a_corrected_handler_is_checked_against_the_same_provider_secret() -> None:
    rotated = merchant.compose(CONFIGS["config@3"], provider_signing_ref=NEXT, version="deploy_SYNTHETIC_rotated")
    for corrected, verified in (("config@5", False), ("config@6", True)):
        candidate = merchant.with_corrected_variant(rotated, corrected)
        assert candidate.provider_signing_ref == NEXT
        assert merchant.run_fixture_check(RECEIVER_TEST, candidate).passed is verified, corrected


def test_a_provider_secret_that_names_no_fixture_secret_is_refused() -> None:
    with pytest.raises(ContractError, match="names no fixture secret"):
        merchant.compose(provider_signing_ref="webhook_secret_unknown")
