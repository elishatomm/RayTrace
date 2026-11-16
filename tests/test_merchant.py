"""Merchant fixture: compositions, fixture checks, realistic traces, diffs and leak-free observables.

These tests may read the oracle (merchant_variant_truth.json) to know what each variant should do;
the runtime module under test never reads it.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from recovery_workbench.contracts import ContractError, HandlerOutcome
from recovery_workbench.simulator import merchant

REPO_ROOT = Path(__file__).resolve().parents[1]
TRUTH = json.loads((REPO_ROOT / "fixtures" / "manifests" / "merchant_variant_truth.json").read_text(encoding="utf-8"))
VARIANTS = TRUTH["variants"]
FAULTS = sorted(name for name, meaning in VARIANTS.items() if meaning["role"] == "fault")
HANDLER_FAULTS = sorted(name for name in FAULTS if VARIANTS[name]["top_frame_file"] is not None)
HARMLESS = sorted(name for name, meaning in VARIANTS.items() if meaning["role"] == "harmless")
CAPTURE_TEST = "captured_payment_builds_fulfilment"
LEAK_WORDS = re.compile(
    r"\b(bugs?|buggy|faults?|faulty|broken|fix|fixes|fixed|fixing|hotfix|regressions?|revert(ed|s)?)\b", re.IGNORECASE
)
FRAME_LINE = re.compile(r'^  File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<name>\S+)$', re.MULTILINE)


def deployed_with(variant: str, *, emits_categories: bool = True) -> merchant.DeployedMerchant:
    module, number = merchant.parse_variant_id(variant)
    return merchant.compose({module: number}, emits_categories=emits_categories)


def failing_tests(deployed: merchant.DeployedMerchant) -> set[str]:
    return {test_id for test_id in merchant.FIXTURE_TEST_IDS if not merchant.run_fixture_check(test_id, deployed).passed}


def capture_trace(variant: str, *, emits_categories: bool = True) -> str:
    result = merchant.run_fixture_check(CAPTURE_TEST, deployed_with(variant, emits_categories=emits_categories))
    assert result.failure is not None, f"{variant} was expected to raise on the capture fixture"
    return result.failure.stack_trace


def test_baseline_passes_every_fixture_test() -> None:
    assert failing_tests(merchant.compose()) == set()


def test_fixture_check_accepts_a_plain_composition_mapping() -> None:
    result = merchant.run_fixture_check(CAPTURE_TEST, {"payload": 2})
    assert not result.passed
    assert result.failure is not None
    assert result.deployed_version == merchant.composition_version({"payload": 2})


@pytest.mark.parametrize("variant", FAULTS)
def test_fault_variant_fails_exactly_the_expected_fixture_tests(variant: str) -> None:
    assert failing_tests(deployed_with(variant)) == set(VARIANTS[variant]["failing_fixture_tests"])


@pytest.mark.parametrize("variant", HANDLER_FAULTS)
def test_handler_fault_top_frame_is_in_the_expected_file(variant: str) -> None:
    trace = capture_trace(variant)
    frames = FRAME_LINE.findall(trace)
    assert frames, trace
    top_file = frames[-1][0]
    assert top_file == VARIANTS[variant]["top_frame_file"]
    assert trace.rstrip().splitlines()[-1].split(":")[0].endswith(VARIANTS[variant]["exception_type"])


def test_upstream_and_config_faults_surface_outside_the_changed_file() -> None:
    # The point of these two variants: naive "blame the top frame's file" picks an unchanged file.
    assert FRAME_LINE.findall(capture_trace("payload@3"))[-1][0] == "merchant_app/orders.py"
    assert FRAME_LINE.findall(capture_trace("config@2"))[-1][0] == "merchant_app/payload.py"


@pytest.mark.parametrize("variant", FAULTS)
def test_allowlisted_corrected_variant_repairs_its_fault(variant: str) -> None:
    deployed = deployed_with(variant)
    for corrected in VARIANTS[variant]["repaired_by"]:
        assert corrected in merchant.CORRECTED_VARIANT_IDS
        assert failing_tests(merchant.with_corrected_variant(deployed, corrected)) == set()


def test_corrected_allowlist_matches_the_oracle() -> None:
    corrected_in_truth = {name for name, meaning in VARIANTS.items() if meaning["role"] == "corrected"}
    assert set(merchant.CORRECTED_VARIANT_IDS) == corrected_in_truth


@pytest.mark.parametrize("variant", HARMLESS)
def test_harmless_variant_passes_every_fixture_test(variant: str) -> None:
    assert failing_tests(deployed_with(variant)) == set()


def test_a_harmless_variant_touches_a_file_that_appears_in_traces() -> None:
    files_in_traces = {frame[0] for variant in HANDLER_FAULTS for frame in FRAME_LINE.findall(capture_trace(variant))}
    touched = {merchant.logical_path(merchant.parse_variant_id(variant)[0]) for variant in HARMLESS}
    assert "merchant_app/orders.py" in files_in_traces & touched


@pytest.mark.parametrize("variant", HANDLER_FAULTS)
def test_traces_hold_no_host_path_home_directory_or_variant_file_name(variant: str) -> None:
    trace = capture_trace(variant)
    forbidden = [str(Path.home()), str(REPO_ROOT), "/Users/", "site-packages", "fixtures", "modules/", "recovery_workbench"]
    assert not [text for text in forbidden if text in trace], trace
    assert not re.search(r"\bv\d+\.py\b", trace), trace
    assert all(frame[0].startswith("merchant_app/") for frame in FRAME_LINE.findall(trace))


@pytest.mark.parametrize("variant", HANDLER_FAULTS)
def test_trace_line_numbers_and_source_lines_are_true(variant: str) -> None:
    deployed = deployed_with(variant)
    trace = capture_trace(variant)
    for match in FRAME_LINE.finditer(trace):
        module = match["file"].removeprefix("merchant_app/").removesuffix(".py")
        source_line = merchant.variant_source(module, deployed.composition[module]).splitlines()[int(match["line"]) - 1]
        shown_line = trace[match.end() :].splitlines()[1]
        assert shown_line.strip() == source_line.strip()
        assert re.search(rf"^def {match['name']}\(", merchant.variant_source(module, deployed.composition[module]), re.M)


def test_category_switch_and_categories_name_a_stage_not_a_cause() -> None:
    for variant in HANDLER_FAULTS:
        with_categories = merchant.run_fixture_check(CAPTURE_TEST, deployed_with(variant)).failure
        without = merchant.run_fixture_check(CAPTURE_TEST, deployed_with(variant, emits_categories=False)).failure
        assert with_categories is not None
        assert without is not None
        assert with_categories.exception_category == VARIANTS[variant]["category_when_emitted"]
        assert without.exception_category is None
        assert with_categories.stack_trace == without.stack_trace
        assert "config" not in with_categories.exception_category
    # The upstream payload change is caught at the order boundary, so the category does not point at payload.
    assert VARIANTS["payload@3"]["category_when_emitted"] == "order_validation_error"


def test_exception_without_fixture_frames_still_renders() -> None:
    deployed = merchant.compose()
    report = deployed.describe_failure(RuntimeError("worker lost its database connection"))
    assert report.stack_trace == "RuntimeError: worker lost its database connection\n"
    assert report.exception_category == merchant.UNLABELLED_CATEGORY


def test_diffs_use_logical_paths_and_git_headers() -> None:
    for module in merchant.MODULE_NAMES:
        for number in merchant.available_variants(module)[1:]:
            diff = merchant.variant_diff(module, 1, number)
            path = merchant.logical_path(module)
            assert diff.startswith(f"diff --git a/{path} b/{path}\nindex ")
            assert f"\n--- a/{path}\n+++ b/{path}\n@@ " in diff
            assert "modules/" not in diff
            assert not re.search(r"\bv\d+\.py\b", diff)


def test_composition_diff_lists_only_changed_modules_and_can_be_bounded() -> None:
    diff = merchant.composition_diff({"orders": 1}, {"orders": 2, "payload": 3})
    assert diff.count("diff --git ") == 2
    assert merchant.changed_files({}, {"orders": 2, "payload": 3}) == ("merchant_app/payload.py", "merchant_app/orders.py")
    bounded = merchant.composition_diff({}, {"payload": 3}, max_lines=6)
    assert len(bounded.splitlines()) == 7
    assert bounded.endswith("more diff lines not shown\n")
    assert merchant.composition_diff({}, {}) == ""


def test_observable_text_is_free_of_words_that_give_the_answer_away() -> None:
    texts = {str(path): path.read_text(encoding="utf-8") for path in (merchant.MERCHANT_REPO / "modules").rglob("*.py")}
    texts["commit_summaries.json"] = (merchant.MERCHANT_REPO / "commit_summaries.json").read_text(encoding="utf-8")
    texts["fixture_tests.json"] = (merchant.MERCHANT_REPO / "fixture_tests.json").read_text(encoding="utf-8")
    for variant in HANDLER_FAULTS:
        texts[f"trace {variant}"] = capture_trace(variant)
    hits = {name: LEAK_WORDS.findall(text) for name, text in texts.items() if LEAK_WORDS.search(text)}
    assert hits == {}


def test_every_shipped_variant_has_several_distinct_commit_summaries() -> None:
    for module in merchant.MODULE_NAMES:
        for number in merchant.available_variants(module)[1:]:
            summaries = merchant.commit_summaries(merchant.variant_id(module, number))
            assert len(summaries) >= 3
            assert len(set(summaries)) == len(summaries)


def test_merchant_code_never_imports_the_workbench() -> None:
    for path in (merchant.MERCHANT_REPO / "modules").rglob("*.py"):
        assert "recovery_workbench" not in path.read_text(encoding="utf-8"), path


def test_handler_outcomes_are_plain_handler_outcome_dicts() -> None:
    deployed = merchant.compose()
    for test_id in merchant.FIXTURE_TEST_IDS[:3]:
        outcome = deployed.handle(merchant.fixture_tests()[test_id].event)
        assert type(outcome) is dict
        HandlerOutcome.model_validate(outcome)


def test_handle_does_not_mutate_the_callers_event() -> None:
    event = merchant.fixture_tests()[CAPTURE_TEST].event
    before = json.dumps(event, sort_keys=True)
    merchant.compose().handle(event)
    assert json.dumps(event, sort_keys=True) == before


def test_receiver_hooks_expose_preprocessing_and_secret_ref() -> None:
    event = merchant.fixture_tests()["signed_delivery_verifies"].event
    compact = merchant.provider_delivery_body(event)
    spaced = json.dumps(event).encode("utf-8")
    baseline = merchant.compose()
    assert baseline.body_for_verification(compact) == compact
    assert baseline.secret_ref == "webhook_secret_primary"
    reserialising = deployed_with("receiver@2")
    assert reserialising.body_for_verification(compact) != compact
    assert reserialising.body_for_verification(spaced) != spaced
    assert deployed_with("config@3").secret_ref == "webhook_secret_next"
    assert deployed_with("receiver@3").body_for_verification(compact) == compact


def test_compositions_are_isolated_from_each_other_and_from_sys_modules() -> None:
    baseline, faulty = merchant.compose(), deployed_with("payload@2")
    event = merchant.fixture_tests()[CAPTURE_TEST].event
    with pytest.raises(KeyError):
        faulty.handle(event)
    assert baseline.handle(event)["action"] == "fulfil"
    assert not [name for name in sys.modules if name == "merchant_app" or name.startswith("merchant_app.")]


def test_version_is_neutral_and_stable() -> None:
    version = merchant.compose({"payload": 2}).version
    assert re.fullmatch(r"rev_[0-9a-f]{12}", version)
    assert version == merchant.compose({"payload": 2}).version != merchant.compose().version
    assert merchant.compose(version="dep_1a2b3c").version == "dep_1a2b3c"


def test_unknown_modules_variants_tests_and_corrections_are_rejected() -> None:
    with pytest.raises(ContractError):
        merchant.compose({"payments": 1})
    with pytest.raises(ContractError):
        merchant.compose({"payload": 99})
    with pytest.raises(ContractError):
        merchant.compose({"payload": True})
    baseline = merchant.compose()
    with pytest.raises(ContractError):
        merchant.run_fixture_check("any_test_you_like", baseline)
    with pytest.raises(ContractError):
        merchant.with_corrected_variant(baseline, "payload@2")
    with pytest.raises(ContractError):
        merchant.parse_variant_id("payload-2")
