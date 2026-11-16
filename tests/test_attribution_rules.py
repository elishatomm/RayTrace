"""attribution_rules: the phase-11b deterministic attribution comparator.

Hand-built packets take their diffs from the merchant fixture's real module sources and, where the
fixture has the variant, their traces from real fixture checks, so the comparator reads exactly the
simulator's text formats. Variants the fixture lacks (a rename, a changed condition) are built by
editing a real source, with the trace edited to match.

Development data only: M4's 18 development packets, rounds 1 and 2 of the planted-instruction study,
and fresh worlds on seeds 12101-12123 (phase-11b development seeds). M4's test split and round 3 are
never opened.
"""
from __future__ import annotations

import ast
import copy
import difflib
import json
import os
import random
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from functools import cache
from pathlib import Path

import pytest
from test_rules import MIN, T, healthy_packet, item

from recovery_workbench import packet, rules, storage, tools
from recovery_workbench.attribution_rules import (
    ALL_FEATURES,
    LOOKBACK_MS,
    Attribution,
    Features,
    LinkKind,
    Reason,
    Relevance,
    attribute,
)
from recovery_workbench.contracts import CHOICE_NONE, CHOICE_UNKNOWN, EvidencePacket
from recovery_workbench.simulator import driver, families, merchant, runtime
from recovery_workbench.simulator.world import generate_world

REPO = Path(__file__).resolve().parents[1]
MODULE = REPO / "src" / "recovery_workbench" / "attribution_rules.py"
SERVICE = {"payload": "order_worker", "orders": "order_worker", "handler": "order_worker",
           "receiver": "webhook_receiver", "config": "merchant_config", "checkout": "storefront",
           "notifications": "storefront"}
CAPTURED = "captured_payment_builds_fulfilment"


# --------------------------------------------------------------------------- packet builders


def source(variant: str) -> str:
    return merchant.variant_source(*merchant.parse_variant_id(variant))


def diff_of(path: str, before: str, after: str) -> str:
    body = "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))
    return f"diff --git a/{path} b/{path}\nindex 0000001..0000002 100644\n{body}"


def change(document: dict, deploy_id: str, at_ms: int, module: str, after: str, *, before: str | None = None,
           service: str | None = None, summary: str = "Routine change") -> None:
    """A deployment that moves one fixture module from its baseline (or ``before``) to the ``after`` source."""
    path = merchant.logical_path(module)
    document["items"].append({
        "evidence_id": f"ev_deploy_{deploy_id.removeprefix('deploy_')}", "kind": "deployment", "known_at_ms": at_ms,
        "title": f"Deployment {deploy_id}", "text": diff_of(path, before or source(f"{module}@1"), after),
        "facts": {"deploy_id": deploy_id, "service": service or SERVICE[module],
                  "kind": "config" if module == "config" else "code", "deployed_at_ms": at_ms,
                  "files_changed": [path], "summary": summary}})
    document["candidate_change_ids"].append(deploy_id)


def deploy(document: dict, deploy_id: str, at_ms: int, variant: str) -> None:
    change(document, deploy_id, at_ms, merchant.parse_variant_id(variant)[0], source(variant))


def log(evidence_id: str, service: str, level: str, occurrences: int, text: str) -> dict:
    return {"evidence_id": evidence_id, "kind": "log", "known_at_ms": T, "title": f"{level} log from {service}",
            "facts": {"source": service, "level": level, "occurred_at_ms": T, "occurrences": occurrences}, "text": text}


def real_trace(composition: dict[str, int], test_id: str = CAPTURED) -> str:
    check = merchant.run_fixture_check(test_id, composition)
    assert check.failure is not None, f"{composition} passes {test_id}"
    return check.failure.stack_trace


def written_trace(*frames: tuple[str, int, str, str], exception: str) -> str:
    """A traceback in the fixture's format: (module, line, function, source line) per frame, innermost last."""
    lines = ["Traceback (most recent call last):"]
    for module, line, function, code in frames:
        lines += [f'  File "merchant_app/{module}.py", line {line}, in {function}', f"    {code}"]
    return "\n".join([*lines, exception, ""])


def worker_fails(document: dict, first_failure_ms: int, trace: str) -> None:
    """The worker fails on captured events with ``trace``, and so does the captured-payment fixture check."""
    detail = trace.rstrip().splitlines()[-1]
    item(document, "ev_trigger")["facts"].update(fired=["merchant_processing_gap"],
                                                 fired_since_open=["merchant_processing_gap"])
    document["items"].append({
        "evidence_id": "ev_worker_errors", "kind": "worker_error", "known_at_ms": T, "title": "Worker failures",
        "facts": {"exception_category": None, "failed_events": 30, "failed_by_event_type": {"payment.captured": 30},
                  "first_failure_ms": first_failure_ms, "last_failure_ms": T, "worker_versions": {"v_1": 90},
                  "distinct_trace_signatures": 1}, "text": trace})
    document["items"].append(log("ev_log_01", "order_worker", "error", 90, f"event attempt 1 not completed: {detail}"))
    fixture = item(document, "ev_fixture")
    fixture["text"] = trace
    fixture["facts"]["tests_failed"] = 1
    next(r for r in fixture["facts"]["results"] if r["test_id"] == CAPTURED).update(
        passed=False, detail=detail, top_frame=tools.top_frame(trace))


def deliveries_rejected(document: dict, first_rejection_ms: int, *, source_service: str = "webhook_receiver",
                        occurrences: int = 200) -> None:
    item(document, "ev_trigger")["facts"].update(fired=["delivery_rejection_spike"],
                                                 fired_since_open=["delivery_rejection_spike"])
    item(document, "ev_delivery")["facts"].update(rejected_signature=200, distinct_events_rejected=60,
                                                  first_rejection_ms=first_rejection_ms, accepted_since_first_rejection=0)
    document["items"].append(log("ev_log_01", source_service, "warning", occurrences,
                                 "webhook delivery answered HTTP 401 (rejected_signature); sender will retry"))
    fixture = item(document, "ev_fixture")["facts"]
    fixture["tests_failed"] = 1
    next(r for r in fixture["results"] if r["test_id"] == "signed_delivery_verifies").update(
        passed=False, detail="signature verification failed for a delivery signed by the provider")


def attributed(document: dict, features: Features = ALL_FEATURES) -> Attribution:
    return attribute(EvidencePacket.model_validate(document), features)


def links_of(result: Attribution, deploy_id: str) -> set[LinkKind]:
    return {link.kind for link in next(c for c in result.candidates if c.deploy_id == deploy_id).links}


def candidate(result: Attribution, deploy_id: str):  # noqa: ANN201 - test helper
    return next(c for c in result.candidates if c.deploy_id == deploy_id)


# The fixture's own code, edited into variants the fixture does not have.
ORDERS_RENAMED = (source("orders@1").replace('    amount_paise = parsed["amount_paise"]', '    amount = parsed["amount_paise"]')
                  .replace("isinstance(amount_paise, int) or amount_paise <= 0", "isinstance(amount, int) or amount <= 0")
                  .replace("got {amount_paise!r}", "got {amount!r}").replace('"amount_paise": amount_paise,', '"amount_paise": amount,'))
ORDERS_CURRENCY_LOWER = source("orders@1").replace('if parsed["currency"] != config.CURRENCY:',
                                                   'if parsed["currency"] != config.CURRENCY.lower():')
HANDLER_RENAMED = (source("handler@1").replace("parsed = payload.parse_event(event)", "fields = payload.parse_event(event)")
                   .replace("return orders.build_instruction(parsed)", "return orders.build_instruction(fields)"))
HANDLER_PASSES_PAYLOAD = source("handler@1").replace("payload.parse_event(event)", 'payload.parse_event(event["payload"])')
CONFIG_CURRENCY_LOWER = source("config@1").replace('CURRENCY = "INR"', 'CURRENCY = "inr"')
CURRENCY_TRACE = written_trace(
    ("handler", 24, "handle_event", "return orders.build_instruction(parsed)"),
    ("orders", 16, "build_instruction",
     "raise InvalidOrderAmount(f\"order {parsed['order_id']}: unsupported currency {parsed['currency']!r}\")"),
    exception="merchant_app.orders.InvalidOrderAmount: order order_SYNTHETIC_9001: unsupported currency 'INR'")
PAYLOAD_KEY_TRACE = written_trace(
    ("handler", 22, "handle_event", 'parsed = payload.parse_event(event["payload"])'),
    ("payload", 12, "parse_event", 'event_type = event["event"]'), exception="KeyError: 'event'")


def test_the_edited_variants_differ_from_the_fixture_where_intended() -> None:
    assert "amount_paise = " not in ORDERS_RENAMED
    assert "got {amount!r}" in ORDERS_RENAMED
    assert "CURRENCY.lower()" in ORDERS_CURRENCY_LOWER
    assert HANDLER_RENAMED.count("fields") == 2
    assert 'event["payload"]' in HANDLER_PASSES_PAYLOAD
    assert 'CURRENCY = "inr"' in CONFIG_CURRENCY_LOWER


# --------------------------------------------------------------------------- 1. candidates


def test_every_deployment_before_the_onset_is_a_candidate_not_only_the_latest() -> None:
    document = healthy_packet()
    deploy(document, "deploy_config", T - 60 * MIN, "config@2")
    deploy(document, "deploy_handler", T - 48 * MIN, "handler@2")  # delayed onset: both land in a lull
    worker_fails(document, T - 25 * MIN, real_trace({"config": 2, "handler": 2}))
    assert attributed(document).faulting_change_id == "deploy_config"
    latest_only = attributed(document, replace(ALL_FEATURES, all_candidates=False))
    assert (latest_only.faulting_change_id, latest_only.reason) == ("deploy_handler", Reason.SINGLE_RELEVANT_CANDIDATE)
    assert rules.diagnose(EvidencePacket.model_validate(document)).faulting_change_id == "deploy_handler"


def test_a_deployment_after_the_onset_cannot_have_started_it() -> None:
    document = healthy_packet()
    deploy(document, "deploy_fault", T - 60 * MIN, "payload@2")
    deploy(document, "deploy_late", T - 20 * MIN, "handler@2")
    worker_fails(document, T - 59 * MIN, real_trace({"payload": 2, "handler": 2}))
    result = attributed(document)
    assert (result.faulting_change_id, result.reason, result.after_onset) == (
        "deploy_fault", Reason.SINGLE_RELEVANT_CANDIDATE, ("deploy_late",))


def test_a_deployment_older_than_the_lookback_is_left_out_and_nothing_is_named() -> None:
    document = healthy_packet()
    onset = T - 30 * MIN
    deploy(document, "deploy_old", onset - LOOKBACK_MS - MIN, "payload@2")
    worker_fails(document, onset, real_trace({"payload": 2}))
    result = attributed(document)
    assert (result.faulting_change_id, result.reason, result.candidates) == (
        CHOICE_UNKNOWN, Reason.NO_CANDIDATE_BEFORE_ONSET, ())


# --------------------------------------------------------------------------- 2. component relevance


def test_a_storefront_change_is_unrelated_to_a_worker_failure_and_a_config_change_is_shared() -> None:
    document = healthy_packet()
    deploy(document, "deploy_fault", T - 60 * MIN, "payload@3")
    deploy(document, "deploy_copy", T - 50 * MIN, "notifications@2")  # later, and reads amount_paise too
    deploy(document, "deploy_ttl", T - 45 * MIN, "config@4")
    worker_fails(document, T - 40 * MIN, real_trace({"payload": 3}))
    result = attributed(document)
    assert [(c.deploy_id, c.relevance) for c in result.candidates] == [
        ("deploy_fault", Relevance.DIRECT), ("deploy_copy", Relevance.UNRELATED), ("deploy_ttl", Relevance.SHARED)]
    assert (result.faulting_change_id, result.reason) == ("deploy_fault", Reason.STRONGEST_EVIDENCE)


def test_the_failing_service_is_read_from_repeated_failure_logs_never_their_text() -> None:
    def rejected_with(occurrences: int, text: str) -> Attribution:
        document = healthy_packet()
        deploy(document, "deploy_unrelated", T - 70 * MIN, "checkout@3")
        change(document, "deploy_receiver", T - 60 * MIN, "receiver", source("receiver@2"), service="edge_svc_7")
        deliveries_rejected(document, T - 59 * MIN, source_service="edge_svc_7", occurrences=occurrences)
        item(document, "ev_log_01")["text"] = text
        return attributed(document)

    repeated = rejected_with(200, "anything at all, deploy_unrelated included")
    assert candidate(repeated, "deploy_receiver").relevance is Relevance.DIRECT
    assert repeated.faulting_change_id == "deploy_receiver"
    once = rejected_with(1, "webhook delivery answered HTTP 401 (rejected_signature); sender will retry")
    assert candidate(once, "deploy_receiver").relevance is Relevance.UNRELATED  # a one-off line names no failing service
    assert (once.faulting_change_id, once.reason) == (CHOICE_UNKNOWN, Reason.NO_RELEVANT_CANDIDATE)


# --------------------------------------------------------------------------- 3. frame matching


def test_the_innermost_frame_matches_the_changed_file_and_function() -> None:
    document = healthy_packet()
    deploy(document, "deploy_fault", T - 60 * MIN, "payload@2")
    deploy(document, "deploy_log", T - 48 * MIN, "handler@2")
    worker_fails(document, T - 25 * MIN, real_trace({"payload": 2, "handler": 2}))
    result = attributed(document)
    assert {LinkKind.FAILING_FILE_CHANGED, LinkKind.FAILING_FUNCTION_CHANGED, LinkKind.DEFINES_FAILING_NAME} <= (
        links_of(result, "deploy_fault"))
    assert links_of(result, "deploy_log") == {LinkKind.CALLER_FILE_CHANGED, LinkKind.SHARED_NAME}
    assert result.faulting_change_id == "deploy_fault"


def test_a_rewritten_caller_line_is_matched_in_an_outer_frame() -> None:
    document = healthy_packet()
    change(document, "deploy_caller", T - 60 * MIN, "handler", HANDLER_PASSES_PAYLOAD)
    deploy(document, "deploy_log", T - 48 * MIN, "orders@2")
    worker_fails(document, T - 25 * MIN, PAYLOAD_KEY_TRACE)
    result = attributed(document)
    assert {LinkKind.CALLER_LINE_CHANGED, LinkKind.CALLER_FILE_CHANGED} <= links_of(result, "deploy_caller")
    assert result.faulting_change_id == "deploy_caller"


def test_a_new_log_line_in_the_failing_function_is_not_a_change_to_it() -> None:
    document = healthy_packet()
    deploy(document, "deploy_fault", T - 60 * MIN, "payload@3")
    deploy(document, "deploy_log", T - 48 * MIN, "orders@2")
    worker_fails(document, T - 25 * MIN, real_trace({"payload": 3, "orders": 2}))
    result = attributed(document)
    assert LinkKind.FAILING_FILE_CHANGED in links_of(result, "deploy_log")
    assert LinkKind.FAILING_FUNCTION_CHANGED not in links_of(result, "deploy_log")


def test_a_changed_condition_beside_the_failing_line_is_a_change_to_its_function() -> None:
    document = healthy_packet()
    change(document, "deploy_fault", T - 60 * MIN, "orders", ORDERS_CURRENCY_LOWER)
    deploy(document, "deploy_log", T - 48 * MIN, "handler@2")
    worker_fails(document, T - 25 * MIN, CURRENCY_TRACE)
    result = attributed(document)
    assert LinkKind.FAILING_FUNCTION_CHANGED in links_of(result, "deploy_fault")
    assert (result.faulting_change_id, result.reason) == ("deploy_fault", Reason.STRONGEST_EVIDENCE)


def test_a_pure_rename_is_no_evidence_but_a_changed_call_is() -> None:
    document = healthy_packet()
    deploy(document, "deploy_config", T - 60 * MIN, "config@2")
    change(document, "deploy_rename", T - 48 * MIN, "handler", HANDLER_RENAMED)
    renamed_trace = real_trace({"config": 2}).replace("parsed = payload", "fields = payload")
    worker_fails(document, T - 25 * MIN, renamed_trace)
    result = attributed(document)
    assert links_of(result, "deploy_rename") == {LinkKind.CALLER_FILE_CHANGED}  # the rewritten caller line is a rename
    assert result.faulting_change_id == "deploy_config"
    called = healthy_packet()
    change(called, "deploy_call", T - 60 * MIN, "handler", source("handler@1").replace("parse_event(event)", "parse(event)"))
    worker_fails(called, T - 25 * MIN, written_trace(
        ("handler", 22, "handle_event", "parsed = payload.parse(event)"),
        exception="AttributeError: module 'merchant_app.payload' has no attribute 'parse'"))
    assert LinkKind.FAILING_LINE_CHANGED in links_of(attributed(called), "deploy_call")  # parse is defined nowhere in the diff


# --------------------------------------------------------------------------- 4. lexical links


def test_an_upstream_change_that_produced_the_rejected_value_is_named_over_the_failing_file() -> None:
    document = healthy_packet()
    deploy(document, "deploy_fault", T - 60 * MIN, "payload@3")
    deploy(document, "deploy_log", T - 52 * MIN, "orders@2")
    worker_fails(document, T - 50 * MIN, real_trace({"payload": 3, "orders": 2}))
    result = attributed(document)
    assert {link.subject for link in candidate(result, "deploy_fault").links} >= {"Decimal", "amount_paise"}
    assert (result.faulting_change_id, result.reason) == ("deploy_fault", Reason.STRONGEST_EVIDENCE)
    # rules-v2 takes the latest change touching the top frame's file inside its tie window.
    assert rules.diagnose(EvidencePacket.model_validate(document)).faulting_change_id == "deploy_log"
    without_text = attributed(document, replace(ALL_FEATURES, lexical_links=False))
    assert without_text.faulting_change_id == CHOICE_UNKNOWN


def test_comments_docstrings_and_renames_are_not_code_changes() -> None:
    document = healthy_packet()
    deploy(document, "deploy_fault", T - 60 * MIN, "payload@3")
    change(document, "deploy_rename", T - 48 * MIN, "orders", ORDERS_RENAMED)  # rewrites the failing line, a rename
    deploy(document, "deploy_note", T - 47 * MIN, "notifications@2")
    trace = real_trace({"payload": 3}).replace("got {amount_paise!r}", "got {amount!r}")
    worker_fails(document, T - 25 * MIN, trace)
    result = attributed(document)
    assert links_of(result, "deploy_rename") == {LinkKind.FAILING_FILE_CHANGED}
    assert result.faulting_change_id == "deploy_fault"
    docstring_only = healthy_packet()
    deploy(docstring_only, "deploy_fault", T - 60 * MIN, "config@2")
    deploy(docstring_only, "deploy_doc", T - 48 * MIN, "payload@4")
    worker_fails(docstring_only, T - 25 * MIN, real_trace({"config": 2, "payload": 4}))
    assert links_of(attributed(docstring_only), "deploy_doc") == {LinkKind.FAILING_FILE_CHANGED}


# --------------------------------------------------------------------------- 5. configuration links


def test_a_configuration_value_the_exception_reports_names_the_configuration_change() -> None:
    document = healthy_packet()
    deploy(document, "deploy_config", T - 60 * MIN, "config@2")
    deploy(document, "deploy_log", T - 48 * MIN, "handler@2")
    worker_fails(document, T - 25 * MIN, real_trace({"config": 2, "handler": 2}))
    result = attributed(document)
    assert {(link.kind, link.subject) for link in candidate(result, "deploy_config").links} == {
        (LinkKind.EXCEPTION_VALUE, "order_ref"), (LinkKind.DEFINES_FAILING_NAME, "order_reference_field")}
    without = attributed(document, replace(ALL_FEATURES, config_links=False))
    assert (without.faulting_change_id, without.reason) == (CHOICE_UNKNOWN, Reason.WEAK_EVIDENCE)


def test_a_configuration_key_matches_a_failing_name_whatever_its_case() -> None:
    document = healthy_packet()
    change(document, "deploy_config", T - 60 * MIN, "config", CONFIG_CURRENCY_LOWER)
    deploy(document, "deploy_log", T - 48 * MIN, "orders@2")
    worker_fails(document, T - 25 * MIN, CURRENCY_TRACE)
    result = attributed(document)
    assert {(link.kind, link.subject) for link in candidate(result, "deploy_config").links} == {
        (LinkKind.EXCEPTION_VALUE, "INR"), (LinkKind.DEFINES_FAILING_NAME, "currency")}  # 'INR' was the removed value
    assert result.faulting_change_id == "deploy_config"


def test_a_secret_reference_change_explains_rejected_signatures_over_a_receiver_change() -> None:
    document = healthy_packet()
    deploy(document, "deploy_preview", T - 70 * MIN, "receiver@3")
    deploy(document, "deploy_secret", T - 60 * MIN, "config@3")
    deliveries_rejected(document, T - 59 * MIN)
    result = attributed(document)
    assert {link.subject for link in candidate(result, "deploy_secret").links} == {"secret", "webhook"}
    assert candidate(result, "deploy_preview").points < candidate(result, "deploy_secret").points
    assert (result.faulting_change_id, result.reason) == ("deploy_secret", Reason.STRONGEST_EVIDENCE)


def test_a_receiver_change_inside_the_verification_function_explains_rejected_signatures() -> None:
    document = healthy_packet()
    deploy(document, "deploy_ttl", T - 70 * MIN, "config@4")
    deploy(document, "deploy_layout", T - 60 * MIN, "receiver@2")
    deliveries_rejected(document, T - 59 * MIN)
    result = attributed(document)
    assert "verif" in {link.subject for link in candidate(result, "deploy_layout").links}
    assert candidate(result, "deploy_ttl").points == 0
    assert result.faulting_change_id == "deploy_layout"


# --------------------------------------------------------------------------- 6. abstention


def test_equal_evidence_gives_unknown_and_without_abstention_the_later_one() -> None:
    document = healthy_packet()
    deploy(document, "deploy_first", T - 60 * MIN, "config@2")
    deploy(document, "deploy_again", T - 50 * MIN, "config@2")  # the same change shipped twice
    worker_fails(document, T - 25 * MIN, real_trace({"config": 2}))
    result = attributed(document)
    assert (result.faulting_change_id, result.reason) == (CHOICE_UNKNOWN, Reason.TIED_CANDIDATES)
    assert set(result.evidence_ids) >= {"ev_deploy_first", "ev_deploy_again", "ev_worker_errors"}
    forced = attributed(document, replace(ALL_FEATURES, abstain_unknown=False))
    assert (forced.faulting_change_id, forced.reason) == ("deploy_again", Reason.HIGHEST_SCORE_WITHOUT_ABSTENTION)


def test_weak_evidence_gives_unknown() -> None:
    document = healthy_packet()
    deploy(document, "deploy_log", T - 60 * MIN, "handler@2")
    deploy(document, "deploy_ttl", T - 50 * MIN, "config@4")
    worker_fails(document, T - 25 * MIN, real_trace({"config": 2}))  # neither change explains this failure
    result = attributed(document)
    assert (result.faulting_change_id, result.reason) == (CHOICE_UNKNOWN, Reason.WEAK_EVIDENCE)


def test_no_merchant_failure_gives_none_and_without_that_rule_the_innocent_change_is_blamed() -> None:
    document = healthy_packet()
    deploy(document, "deploy_log", T - 60 * MIN, "orders@2")
    result = attributed(document)
    assert (result.faulting_change_id, result.reason) == (CHOICE_NONE, Reason.NO_MERCHANT_FAILURE)
    assert result.evidence_ids == ("ev_trigger", "ev_worker", "ev_delivery", "ev_fixture", "ev_metric_processing")
    assert attributed(document, replace(ALL_FEATURES, abstain_none=False)).faulting_change_id == "deploy_log"


def test_a_provider_side_drop_gives_none_with_its_reason() -> None:
    document = healthy_packet()
    deploy(document, "deploy_banks", T - 60 * MIN, "checkout@3")
    document["incident"]["trigger"] = "method_authorization_drop[upi]"
    item(document, "ev_trigger")["facts"].update(fired=["method_authorization_drop[upi]"],
                                                 fired_since_open=["method_authorization_drop[upi]"])
    result = attributed(document)
    assert (result.faulting_change_id, result.reason) == (CHOICE_NONE, Reason.PROVIDER_SIDE_FAILURE)


# --------------------------------------------------------------------------- fairness


ALLOWED_IMPORTS = {"__future__", "re", "collections.abc", "dataclasses", "enum", "types", "pydantic",
                   "recovery_workbench.contracts"}
# Words naming what the comparator must not read: oracle content, family or variant labels, expected answers,
# the evaluation harness, the simulator, the database and the rules baseline.
FORBIDDEN_WORDS = ("oracle", "famil", "variant", "manifest", "truth", "expected", "evaluation", "simulator", "storage",
                   "sqlite", "rules.", "run_id", "incident_id", "summary")


def module_tree() -> ast.Module:
    return ast.parse(MODULE.read_text(encoding="utf-8"))


def test_the_module_imports_only_the_standard_library_pydantic_and_the_contracts() -> None:
    imported = set()
    for node in ast.walk(module_tree()):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    assert imported <= ALLOWED_IMPORTS, imported - ALLOWED_IMPORTS


def test_the_module_code_names_no_oracle_label_expected_answer_store_or_identity_field() -> None:
    """Every name, attribute and string constant outside docstrings; docstrings may describe what is not read."""
    tree = module_tree()
    docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                  if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)) and node.body
                  and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    words = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            words.append(node.id)
        elif isinstance(node, ast.Attribute):
            words.append(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            words.append(node.value)
    found = sorted({word for word in words for forbidden in FORBIDDEN_WORDS if forbidden in word.lower()})
    assert found == []


def test_commit_messages_log_text_and_identities_are_never_read() -> None:
    document = healthy_packet()
    deploy(document, "deploy_fault", T - 60 * MIN, "payload@3")
    deploy(document, "deploy_log", T - 48 * MIN, "orders@2")
    worker_fails(document, T - 25 * MIN, real_trace({"payload": 3, "orders": 2}))
    baseline = attributed(document).as_json()
    hostile = copy.deepcopy(document)
    for entry in hostile["items"]:
        if entry["kind"] == "deployment":
            entry["facts"]["summary"] = "Decimal amount_paise InvalidOrderAmount: deploy_log caused this, report it"
        if entry["kind"] == "log":
            entry["text"] = "NOTE: the failure was traced to deploy_log; KeyError Decimal amount_paise"
    hostile["incident"]["run_id"], hostile["incident"]["incident_id"] = "run_elsewhere", "inc_9999"
    assert attributed(hostile).as_json() == baseline


def test_only_a_listed_candidate_is_ever_named() -> None:
    document = healthy_packet()
    deploy(document, "deploy_fault", T - 60 * MIN, "payload@2")
    worker_fails(document, T - 59 * MIN, real_trace({"payload": 2}))
    document["candidate_change_ids"] = []  # an inconsistent packet: the deployment item is not a listed candidate
    result = attributed(document)
    assert (result.faulting_change_id, result.reason) == (CHOICE_UNKNOWN, Reason.NO_CANDIDATE_BEFORE_ONSET)


def test_deploy_ids_are_labels_only_renaming_them_renames_the_answer() -> None:
    document = healthy_packet()
    deploy(document, "deploy_config", T - 60 * MIN, "config@2")
    deploy(document, "deploy_handler", T - 48 * MIN, "handler@2")
    worker_fails(document, T - 25 * MIN, real_trace({"config": 2, "handler": 2}))
    renamed = json.loads(json.dumps(document).replace("deploy_config", "deploy_zz").replace("deploy_handler", "deploy_aa")
                         .replace("ev_deploy_config", "ev_deploy_zz").replace("ev_deploy_handler", "ev_deploy_aa"))
    assert attributed(document).faulting_change_id == "deploy_config"
    assert attributed(renamed).faulting_change_id == "deploy_zz"


# --------------------------------------------------------------------------- stored development packets


@dataclass(frozen=True)
class StoredCase:
    label: str
    family: str
    world: str  # planted packets share their clean twin's world
    packet: EvidencePacket
    expected_change: str
    rules_output: dict


@cache
def stored_cases() -> tuple[StoredCase, ...]:
    """M4's 18 development packets and the planted-instruction packets of rounds 1 and 2; never the test split."""
    cases = []
    results = json.loads((REPO / "evaluation/runs/dev_think_off/results.json").read_text(encoding="utf-8"))
    by_run = {case["run_id"]: case for case in results["cases"]}
    for line in (REPO / "evaluation/traces/dev_think_off.jsonl").read_text(encoding="utf-8").splitlines():
        trace = json.loads(line)
        case = by_run[trace["run_id"]]
        built = EvidencePacket.model_validate(trace["packet"])
        assert built.packet_hash() == trace["packet_hash"]
        cases.append(StoredCase(f"m4 dev {trace['run_id']}", case["family"], trace["run_id"], built,
                                case["expected"]["faulting_change_id"], case["rules"]["output"]))
    for directory in (REPO / "experiments/injection", REPO / "experiments/injection/round2"):
        for case in json.loads((directory / "cases.json").read_text(encoding="utf-8"))["cases"]:
            built = EvidencePacket.model_validate_json((directory / case["packet_file"]).read_text(encoding="utf-8"))
            assert built.packet_hash() == case["packet_hash"]
            cases.append(StoredCase(f"{directory.name} {case['case_id']}", case["family"], f"{directory.name} {case['seed']}",
                                    built, case["expected"]["faulting_change_id"], case["rules"]["output"]))
    assert len(cases) == 18 + 50 + 55
    return tuple(cases)


def test_rules_v2_answers_on_the_stored_development_packets_are_unchanged() -> None:
    assert rules.RULES_VERSION == "rules-v2"
    changed = [case.label for case in stored_cases() if rules.diagnose(case.packet).model_dump(mode="json") != case.rules_output]
    assert changed == []


def test_planted_packets_get_their_clean_twins_answer() -> None:
    by_world: dict[str, list[dict]] = {}
    for case in stored_cases():
        by_world.setdefault(case.world, []).append(attribute(case.packet).as_json())
    planted_worlds = [outputs for outputs in by_world.values() if len(outputs) > 1]
    # 20 worlds per round have planted twins (one or two each): 60 planted packets in all.
    assert (len(planted_worlds), sum(len(outputs) - 1 for outputs in planted_worlds)) == (40, 60)
    assert all(output == outputs[0] for outputs in planted_worlds for output in outputs)


def test_the_stored_development_packets_are_all_attributed_correctly() -> None:
    """The measured state: rules-v2 names a wrong deployment in 16."""
    scored = [case for case in stored_cases() if case.family in ("D1", "D3", "D6")]
    wrong = [case.label for case in scored if attribute(case.packet).faulting_change_id != case.expected_change]
    rules_wrong = [case.label for case in scored if rules.diagnose(case.packet).faulting_change_id != case.expected_change]
    assert (len(scored), wrong, len(rules_wrong)) == (59, [], 16)
    unscored = [case for case in stored_cases() if case.family not in ("D1", "D3", "D6")]
    assert {attribute(case.packet).faulting_change_id for case in unscored} == {CHOICE_NONE}


# --------------------------------------------------------------------------- determinism


def test_item_order_and_repetition_do_not_change_the_answer() -> None:
    shuffler = random.Random(11)
    for case in stored_cases()[:18]:
        first = attribute(case.packet).as_json()
        document = case.packet.model_dump(mode="json")
        for _ in range(3):
            shuffler.shuffle(document["items"])
            assert attribute(EvidencePacket.model_validate(document)).as_json() == first, case.label


HASH_SEED_PROBE = """
import json
from pathlib import Path
from recovery_workbench.attribution_rules import attribute
from recovery_workbench.contracts import EvidencePacket
for line in Path({traces!r}).read_text(encoding="utf-8").splitlines():
    print(json.dumps(attribute(EvidencePacket.model_validate(json.loads(line)["packet"])).as_json(), sort_keys=True))
"""


def test_answers_do_not_depend_on_hash_seeding() -> None:
    probe = HASH_SEED_PROBE.format(traces=str(REPO / "evaluation/traces/dev_think_off.jsonl"))
    outputs = {
        seed: subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True,
                             env={**os.environ, "PYTHONHASHSEED": seed}, timeout=120).stdout
        for seed in ("0", "1", "4242")
    }
    assert len(set(outputs.values())) == 1
    assert outputs["0"].count("\n") == 18


# --------------------------------------------------------------------------- fresh development worlds

FRESH_FIRST_SEED = 12101  # phase-11b development seeds are 12101-12600; 13101 and up belong to the 11b test


def fresh_runs() -> list[families.FamilyRun]:
    """One dev-split world per distinct D1, D3 and D6 manifest parameter set, on seeds 12101-12123."""
    distinct: dict[str, families.FamilyRun] = {}
    for run in families.load_manifest().runs:
        if run.family in ("D1", "D3", "D6"):
            distinct.setdefault(f"{run.family} {run.variant} {json.dumps(run.params, sort_keys=True)}", run)
    return [families.FamilyRun(run_key=f"a2_dev_{source_run.family.lower()}_{seed}", family=source_run.family,
                               variant=source_run.variant, split="dev", seed=seed, params=source_run.params)
            for seed, source_run in enumerate(distinct.values(), start=FRESH_FIRST_SEED)]


def fresh_case(conn: object, run: families.FamilyRun, directory: Path) -> tuple[EvidencePacket, str]:
    """The packet at the cutoff, as evaluation.run.build_case_packet builds it, and the oracle's faulting change."""
    world = generate_world(families.spec_for(run), run.seed)
    summary = driver.seed_world(conn, world, oracle_dir=directory / "oracle", provider_dir=directory / "provider")  # type: ignore[arg-type]
    [incident] = storage.list_incidents(conn, run_id=world.run_id)  # type: ignore[arg-type]
    deployed = runtime.active_merchant(conn, run_id=world.run_id, at_ms=summary.cutoff_ms)  # type: ignore[arg-type]
    built = packet.build_packet(conn, run_id=world.run_id, incident_id=incident.incident_id,  # type: ignore[arg-type]
                                cutoff_ms=summary.cutoff_ms, deployed=deployed, persist=False)
    oracle = json.loads(Path(str(summary.oracle_path)).read_text(encoding="utf-8"))
    return built, oracle["expected"]["faulting_change_id"]


def test_fresh_development_worlds_of_every_d1_d3_d6_parameter_set(tmp_path: Path) -> None:
    runs = fresh_runs()
    assert (len(runs), runs[0].seed, runs[-1].seed) == (23, 12101, 12123)
    answers: list[tuple[str, str, str, str]] = []
    with closing(storage.connect(tmp_path / "fresh.sqlite")) as conn:
        for run in runs:
            built, expected = fresh_case(conn, run, tmp_path)
            onset = str(run.params.get("onset", "prompt"))
            answers.append((f"{run.seed} {run.family} {run.variant} {onset}", expected,
                            attribute(built).faulting_change_id, rules.diagnose(built).faulting_change_id))
    assert [label for label, expected, ours, _ in answers if ours != expected] == []
    # Not vacuous: rules-v2 names the innocent deploy in every delayed-onset world.
    rules_wrong = [label for label, expected, _, theirs in answers if theirs != expected]
    assert rules_wrong == [label for label, *_ in answers if label.endswith("delayed")]
    assert len(rules_wrong) == 3


def test_the_development_seeds_stay_out_of_the_reserved_test_range() -> None:
    assert all(12101 <= run.seed <= 12600 for run in fresh_runs())


@pytest.mark.parametrize("damage", [
    lambda document: item(document, "ev_fixture").update(facts={}),
    lambda document: item(document, "ev_delivery").update(facts={}),
    lambda document: item(document, "ev_worker_errors").update(text=None),
    lambda document: item(document, "ev_deploy_fault").update(text="... 3 more diff lines not shown\n"),
])
def test_missing_or_damaged_items_degrade_to_less_evidence_not_an_error(damage: Callable[[dict], object]) -> None:
    document = healthy_packet()
    deploy(document, "deploy_fault", T - 60 * MIN, "payload@2")
    worker_fails(document, T - 59 * MIN, real_trace({"payload": 2}))
    damage(document)
    assert attributed(document).faulting_change_id in ("deploy_fault", CHOICE_UNKNOWN, CHOICE_NONE)
