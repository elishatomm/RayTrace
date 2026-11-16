"""Fixture merchant app: compose numbered module variants and run them as a deployed handler.

The merchant app's source lives in ``fixtures/merchant_repo/modules/<module>/v<n>.py``. A
deployment switches one module (or the config module) to another variant, so the live code at
any moment is a composition ``{module: variant number}``. Each composition is executed as its own
isolated package called ``merchant_app``. Nothing is registered in ``sys.modules``, so several
compositions coexist in one process, and their tracebacks carry logical paths
(``merchant_app/payload.py``) with true line numbers and source lines.

This is runtime-side code. It never reads ``fixtures/manifests/merchant_variant_truth.json``:
what a variant means (whether it fails, what repairs it) is oracle knowledge.
"""
from __future__ import annotations

import builtins
import copy
import difflib
import hashlib
import hmac
import json
import traceback
import types
from collections.abc import Callable, Mapping
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import JsonValue

from recovery_workbench.contracts import ContractError, FailureReport, StrictModel, canonical_json, sha256_hex
from recovery_workbench.simulator import signing

MERCHANT_REPO = Path(__file__).resolve().parents[3] / "fixtures" / "merchant_repo"
PACKAGE = "merchant_app"
MODULE_NAMES: tuple[str, ...] = ("config", "payload", "orders", "handler", "receiver", "checkout", "notifications")
BASELINE: Mapping[str, int] = MappingProxyType(dict.fromkeys(MODULE_NAMES, 1))

# Category recorded when categories are on but the exception escaped every error boundary.
UNLABELLED_CATEGORY = "unhandled_error"

# Allowlisted fixture tests: identical in every run, so their presence reveals nothing.
FIXTURE_TEST_IDS: tuple[str, ...] = (
    "captured_payment_builds_fulfilment",
    "authorized_payment_changes_nothing",
    "failed_payment_changes_nothing",
    "signed_delivery_verifies",
)

# Allowlisted corrected variants an operator may validate after a diagnosis. The list is the same
# in every run; which entry (if any) repairs a given run is oracle knowledge.
CORRECTED_VARIANT_IDS: tuple[str, ...] = ("config@5", "config@6", "payload@5", "payload@6", "receiver@4")

# The simulated provider signs with this secret unless the run's provider rotates (scenarios.SecretRotation); the
# receiver fixture check signs with whichever secret the provider uses at the check's time (DeployedMerchant).
# Secrets live in the one shared table, simulator.signing.FIXTURE_SECRETS; merchant config only names them.
PROVIDER_SIGNING_REF = "webhook_secret_primary"
# One fix validation runs the whole allowlisted suite; this names that suite in fix_validations.
FIXTURE_SUITE_ID = "merchant_fixture_suite_v1"

Composition = Mapping[str, int]


# --------------------------------------------------------------------------- variants and sources


def logical_path(module: str) -> str:
    """Repository path of a module as it appears in traces and diffs, e.g. ``merchant_app/payload.py``."""
    return f"{PACKAGE}/{module}.py"


def variant_id(module: str, number: int) -> str:
    return f"{module}@{number}"


def parse_variant_id(text: str) -> tuple[str, int]:
    module, separator, number = text.partition("@")
    if not separator or module not in MODULE_NAMES or not number.isdigit():
        raise ContractError(f"variant ID must look like 'payload@3' with a module in {MODULE_NAMES}, got {text!r}")
    return module, int(number)


def available_variants(module: str) -> tuple[int, ...]:
    _require_module(module)
    numbers = (path.stem.removeprefix("v") for path in (MERCHANT_REPO / "modules" / module).glob("v*.py"))
    return tuple(sorted(int(number) for number in numbers if number.isdigit()))


@lru_cache(maxsize=None)
def variant_source(module: str, number: int) -> str:
    _require_module(module)
    path = MERCHANT_REPO / "modules" / module / f"v{number}.py"
    if not path.is_file():
        raise ContractError(
            f"no merchant variant {variant_id(module, number)}; available for {module}: {available_variants(module)}"
        )
    return path.read_text(encoding="utf-8")


def _require_module(module: str) -> None:
    if module not in MODULE_NAMES:
        raise ContractError(f"unknown merchant module {module!r}; expected one of {MODULE_NAMES}")


def resolve_composition(overrides: Composition) -> Composition:
    """Baseline variants with ``overrides`` applied; every referenced variant must exist."""
    unknown = sorted(set(overrides) - set(MODULE_NAMES))
    if unknown:
        raise ContractError(f"unknown merchant modules {unknown}; expected a subset of {MODULE_NAMES}")
    for module, number in overrides.items():
        if isinstance(number, bool) or not isinstance(number, int) or number < 1:
            raise ContractError(f"variant number for {module!r} must be a positive integer, got {number!r}")
    composition = {**BASELINE, **overrides}
    for module, number in composition.items():
        variant_source(module, number)
    return MappingProxyType(composition)


def composition_version(composition: Composition) -> str:
    """Stable, neutral identifier of a composition (no variant numbers or meanings visible)."""
    resolved = resolve_composition(composition)
    digest = sha256_hex(canonical_json({module: resolved[module] for module in MODULE_NAMES}))
    return f"rev_{digest[:12]}"


# --------------------------------------------------------------------------- diffs


def _git_blob_id(text: str) -> str:
    raw = text.encode("utf-8")
    # Mimics git's abbreviated blob ID so diff headers look ordinary; not a security use of SHA-1.
    return hashlib.sha1(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest()[:7]  # nosemgrep: python.lang.security.insecure-hash-algorithms.insecure-hash-algorithm-sha1 -- mimics git's blob IDs in diff headers, not a security use; another hash would change evidence-packet text


def variant_diff(module: str, before: int, after: int) -> str:
    """Git-style unified diff of one module between two variants, with logical ``a/``/``b/`` paths."""
    old_source, new_source = variant_source(module, before), variant_source(module, after)
    if old_source == new_source:
        return ""
    path = logical_path(module)
    hunks = difflib.unified_diff(
        old_source.splitlines(keepends=True),
        new_source.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    header = f"diff --git a/{path} b/{path}\nindex {_git_blob_id(old_source)}..{_git_blob_id(new_source)} 100644\n"
    return header + "".join(hunks)


def changed_files(before: Composition, after: Composition) -> tuple[str, ...]:
    old, new = resolve_composition(before), resolve_composition(after)
    return tuple(logical_path(module) for module in MODULE_NAMES if old[module] != new[module])


def composition_diff(before: Composition, after: Composition, *, max_lines: int | None = None) -> str:
    """Diff between two deployed compositions, optionally bounded to ``max_lines`` lines."""
    old, new = resolve_composition(before), resolve_composition(after)
    text = "".join(variant_diff(module, old[module], new[module]) for module in MODULE_NAMES if old[module] != new[module])
    if max_lines is None:
        return text
    lines = text.splitlines(keepends=True)
    if len(lines) <= max_lines:
        return text
    hidden = len(lines) - max_lines
    return "".join(lines[:max_lines]) + f"... {hidden} more diff lines not shown\n"


@lru_cache(maxsize=1)
def _commit_summary_table() -> Mapping[str, tuple[str, ...]]:
    raw = json.loads((MERCHANT_REPO / "commit_summaries.json").read_text(encoding="utf-8"))
    return MappingProxyType({variant: tuple(texts) for variant, texts in raw["summaries"].items()})


def commit_summaries(variant: str) -> tuple[str, ...]:
    """Ordinary commit summaries for deploying ``variant``; several, so runs can use disjoint texts."""
    parse_variant_id(variant)
    texts = _commit_summary_table().get(variant)
    if not texts:
        raise ContractError(f"no commit summaries recorded for {variant!r} in {MERCHANT_REPO / 'commit_summaries.json'}")
    return texts


# --------------------------------------------------------------------------- isolated execution


class _ComposedPackage:
    """Executes one composition's sources as an isolated ``merchant_app`` package.

    Imports of ``merchant_app`` inside the fixture code are answered from this object instead of
    ``sys.modules``; every other import falls through to the normal import system.
    """

    def __init__(self, composition: Composition) -> None:
        self._composition = composition
        self._sources = {module: variant_source(module, composition[module]) for module in MODULE_NAMES}
        self._package = types.ModuleType(PACKAGE)
        self._package.__path__ = []
        self._modules: dict[str, types.ModuleType] = {}
        self._loading: set[str] = set()
        scoped_builtins = dict(vars(builtins))
        scoped_builtins["__import__"] = self._import
        self._builtins = scoped_builtins
        for module in MODULE_NAMES:
            self.module(module)

    def source_line(self, filename: str, lineno: int) -> str | None:
        module = filename.removeprefix(f"{PACKAGE}/").removesuffix(".py")
        source = self._sources.get(module)
        if source is None or logical_path(module) != filename:
            return None
        # Keep the line ending: CPython's caret placement assumes lines as linecache returns them.
        lines = source.splitlines(keepends=True)
        if not 1 <= lineno <= len(lines):
            return None
        line = lines[lineno - 1]
        return line if line.endswith("\n") else line + "\n"

    def module(self, name: str) -> types.ModuleType:
        loaded = self._modules.get(name)
        if loaded is not None:
            return loaded
        if name not in self._sources:
            raise ModuleNotFoundError(f"No module named '{PACKAGE}.{name}'")
        if name in self._loading:
            raise ImportError(f"circular import of {PACKAGE}.{name} in the merchant fixture")
        self._loading.add(name)
        try:
            module = types.ModuleType(f"{PACKAGE}.{name}")
            module.__file__ = logical_path(name)
            module.__dict__["__builtins__"] = self._builtins
            # The fixture repo is first-party code shipped with this project; executing it is the point.
            code = compile(self._sources[name], logical_path(name), "exec", dont_inherit=True)
            exec(code, module.__dict__)  # nosemgrep: python.lang.security.audit.exec-detected.exec-detected -- runs this project's own fixture modules in an isolated module with restricted built-ins; that is its purpose
        finally:
            self._loading.discard(name)
        self._modules[name] = module
        setattr(self._package, name, module)
        return module

    def _import(
        self,
        name: str,
        module_globals: Mapping[str, object] | None = None,
        module_locals: Mapping[str, object] | None = None,
        fromlist: tuple[str, ...] | None = (),
        level: int = 0,
    ) -> types.ModuleType:
        # Python calls __import__ positionally, so the parameter names here are free to differ.
        if level != 0 or (name != PACKAGE and not name.startswith(f"{PACKAGE}.")):
            return builtins.__import__(name, module_globals, module_locals, fromlist or (), level)
        if name == PACKAGE:
            for submodule in fromlist or ():
                self.module(submodule)
            return self._package
        loaded = self.module(name.removeprefix(f"{PACKAGE}."))
        return loaded if fromlist else self._package

    def call(self, module: str, function: str) -> Callable[..., object]:
        target = getattr(self.module(module), function, None)
        if not callable(target):
            raise ContractError(
                f"{logical_path(module)} (variant {variant_id(module, self._composition[module])}) "
                f"defines no callable {function!r}"
            )
        return target


# --------------------------------------------------------------------------- deployed handler


class DeployedMerchant:
    """One deployed composition of the merchant app; satisfies ``contracts.MerchantHandler``.

    ``emits_categories`` models whether this deployment writes structured error categories.
    When it does, the category is the error boundary that the exception escaped from (for
    example ``event_parse_error``), which names a processing stage, never a cause.

    ``provider_signing_ref`` is not merchant code: it names the secret the simulated provider
    signs with while this composition is live, so the receiver fixture check signs a delivery as
    the provider would at the check's time. ``runtime.active_merchant`` sets it.
    """

    def __init__(
        self,
        composition: Composition,
        *,
        emits_categories: bool = True,
        version: str | None = None,
        provider_signing_ref: str = PROVIDER_SIGNING_REF,
    ) -> None:
        if provider_signing_ref not in signing.FIXTURE_SECRETS:
            raise ContractError(
                f"the provider signs with {provider_signing_ref!r}, which names no fixture secret; "
                f"use one of {sorted(signing.FIXTURE_SECRETS)}"
            )
        self._composition = resolve_composition(composition)
        self._emits_categories = emits_categories
        self._version = version or composition_version(self._composition)
        self._provider_signing_ref = provider_signing_ref
        self._package = _ComposedPackage(self._composition)

    @property
    def version(self) -> str:
        return self._version

    @property
    def composition(self) -> Composition:
        return self._composition

    @property
    def emits_categories(self) -> bool:
        return self._emits_categories

    @property
    def provider_signing_ref(self) -> str:
        """Name of the secret the simulated provider signs with while this composition is live."""
        return self._provider_signing_ref

    def with_variants(self, overrides: Composition, *, version: str | None = None) -> DeployedMerchant:
        """Another composition at the same moment: the provider still signs with the same secret."""
        return DeployedMerchant(
            {**self._composition, **overrides}, emits_categories=self._emits_categories, version=version,
            provider_signing_ref=self._provider_signing_ref,
        )

    def handle(self, event: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
        """Run the deployed ``handle_event`` on a private copy of the event; merchant exceptions propagate."""
        outcome = self._package.call("handler", "handle_event")(copy.deepcopy(dict(event)))
        if not isinstance(outcome, Mapping):
            raise TypeError(f"merchant handle_event returned {type(outcome).__name__}, expected a mapping")
        return dict(outcome)

    def describe_failure(self, exc: BaseException) -> FailureReport:
        category: str | None = None
        if self._emits_categories:
            boundary = getattr(exc, "error_boundary", None)
            category = boundary if isinstance(boundary, str) and boundary else UNLABELLED_CATEGORY
        return FailureReport(exception_category=category, stack_trace=self._render_traceback(exc))

    def body_for_verification(self, raw_body: bytes) -> bytes:
        """Receiver hook: the bytes the deployed receiver checks the signature against."""
        prepared = self._package.call("receiver", "body_for_verification")(raw_body)
        if not isinstance(prepared, bytes):
            raise TypeError(f"merchant receiver returned {type(prepared).__name__} for verification, expected bytes")
        return prepared

    @property
    def secret_ref(self) -> str:
        """Receiver hook: name of the signing secret the deployed receiver verifies with."""
        ref = self._package.call("receiver", "verification_secret_ref")()
        if not isinstance(ref, str) or not ref:
            raise TypeError(f"merchant receiver returned {ref!r} as its secret_ref, expected a non-empty string")
        return ref

    @property
    def staged_secret_ref(self) -> str | None:
        """Config setting: name of the signing secret staged for the next rotation, or None when none is staged.

        Read from the config module itself (``NEXT_WEBHOOK_SECRET_REF``): no receiver variant has a hook for it,
        because the workbench's receiver honours it (``runtime.staged_secret_ref``).
        """
        ref = getattr(self._package.module("config"), "NEXT_WEBHOOK_SECRET_REF", None)
        if ref is not None and (not isinstance(ref, str) or not ref):
            raise TypeError(f"merchant config stages {ref!r} as its next secret_ref, expected a non-empty string")
        return ref

    def _render_traceback(self, exc: BaseException) -> str:
        """Standard Python traceback text restricted to fixture frames, with fixture source lines.

        Frames from the simulator or the worker are dropped so no host path ever appears; the
        remaining frames keep their real line and column positions, so carets render as Python
        itself would render them.
        """
        rendered = traceback.TracebackException(type(exc), exc, exc.__traceback__, lookup_lines=False)
        pending = [rendered]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            current.stack = traceback.StackSummary.from_list(self._fixture_frames(current.stack))
            pending.extend(link for link in (current.__cause__, current.__context__) if link is not None)
        return "".join(rendered.format())

    def _fixture_frames(self, stack: traceback.StackSummary) -> list[traceback.FrameSummary]:
        kept = []
        for frame in stack:
            if frame.lineno is None:
                continue
            line = self._package.source_line(frame.filename, frame.lineno)
            if line is None:
                continue
            kept.append(
                traceback.FrameSummary(
                    frame.filename,
                    frame.lineno,
                    frame.name,
                    lookup_line=False,
                    line=line,
                    end_lineno=frame.end_lineno,
                    colno=frame.colno,
                    end_colno=frame.end_colno,
                )
            )
        return kept


def compose(
    overrides: Composition | None = None,
    *,
    emits_categories: bool = True,
    version: str | None = None,
    provider_signing_ref: str = PROVIDER_SIGNING_REF,
) -> DeployedMerchant:
    """Deploy the baseline with ``overrides`` ({module: variant number}) applied."""
    return DeployedMerchant(overrides or {}, emits_categories=emits_categories, version=version,
                            provider_signing_ref=provider_signing_ref)


def with_corrected_variant(deployed: DeployedMerchant, corrected_variant: str) -> DeployedMerchant:
    """The deployed composition with one allowlisted corrected variant applied (operator step, post-diagnosis)."""
    if corrected_variant not in CORRECTED_VARIANT_IDS:
        raise ContractError(
            f"corrected variant {corrected_variant!r} is not allowlisted; choose one of {CORRECTED_VARIANT_IDS}"
        )
    module, number = parse_variant_id(corrected_variant)
    return deployed.with_variants({module: number}, version=f"{deployed.version}+{corrected_variant}")


# --------------------------------------------------------------------------- fixture checks


class FixtureTest(StrictModel):
    test_id: str
    kind: Literal["handler", "receiver"]
    description: str
    event: dict[str, JsonValue]
    expected: dict[str, JsonValue]


class FixtureCheckResult(StrictModel):
    """Outcome of one allowlisted fixture test against one deployed composition."""

    test_id: str
    deployed_version: str
    passed: bool
    expected: dict[str, JsonValue]
    actual: dict[str, JsonValue] | None
    failure: FailureReport | None
    detail: str | None


@lru_cache(maxsize=1)
def fixture_tests() -> Mapping[str, FixtureTest]:
    path = MERCHANT_REPO / "fixture_tests.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    tests = {entry["test_id"]: FixtureTest.model_validate(entry) for entry in raw["tests"]}
    if tuple(tests) != FIXTURE_TEST_IDS:
        raise ContractError(f"{path} defines tests {tuple(tests)}, but the allowlist is {FIXTURE_TEST_IDS}")
    return MappingProxyType(tests)


def provider_delivery_body(event: Mapping[str, JsonValue]) -> bytes:
    """Body bytes as the simulated provider sends them (the shared producer serialisation)."""
    return signing.serialise_event(event)


def run_fixture_check(test_id: str, composition: DeployedMerchant | Composition) -> FixtureCheckResult:
    """Run one allowlisted fixture test against a deployed composition ({module: variant} is deployed first)."""
    if test_id not in FIXTURE_TEST_IDS:
        raise ContractError(f"fixture test {test_id!r} is not allowlisted; choose one of {FIXTURE_TEST_IDS}")
    deployed = composition if isinstance(composition, DeployedMerchant) else compose(composition)
    test = fixture_tests()[test_id]
    if test.kind == "receiver":
        return _check_receiver(test, deployed)
    return _check_handler(test, deployed)


def _check_handler(test: FixtureTest, deployed: DeployedMerchant) -> FixtureCheckResult:
    try:
        outcome = dict(deployed.handle(test.event))
    except Exception as exc:  # the deployed code raised: that is the recorded test outcome
        report = deployed.describe_failure(exc)
        return _failed(test, deployed, failure=report, detail=report.stack_trace.rstrip().splitlines()[-1])
    try:
        json.dumps(outcome, allow_nan=False)
    except (TypeError, ValueError) as exc:
        return _failed(test, deployed, failure=None, detail=f"handler outcome is not plain JSON: {exc}")
    passed = outcome == test.expected
    return FixtureCheckResult(
        test_id=test.test_id,
        deployed_version=deployed.version,
        passed=passed,
        expected=test.expected,
        actual=outcome,
        failure=None,
        detail=None if passed else "outcome differs from the expected instruction",
    )


def _check_receiver(test: FixtureTest, deployed: DeployedMerchant) -> FixtureCheckResult:
    """A delivery signed as the provider signs it at the check's time, verified as the workbench's receiver does.

    That is the deployed config's secret, or the next secret it stages (``ingestion.receive_webhook``, Q22).
    """
    raw_body = provider_delivery_body(test.event)
    signature = signing.sign_body(raw_body, deployed.provider_signing_ref)
    try:
        verified_body = deployed.body_for_verification(raw_body)
        secret_refs = (deployed.secret_ref, deployed.staged_secret_ref)
    except Exception as exc:  # the deployed receiver raised: that is the recorded test outcome
        report = deployed.describe_failure(exc)
        return _failed(test, deployed, failure=report, detail=report.stack_trace.rstrip().splitlines()[-1])
    # A secret_ref that names no provisioned secret cannot verify anything: the receiver rejects.
    verified = any(
        ref in signing.FIXTURE_SECRETS and hmac.compare_digest(signing.sign_body(verified_body, ref), signature)
        for ref in secret_refs if ref is not None
    )
    actual: dict[str, JsonValue] = {"verified": verified}
    passed = actual == test.expected
    return FixtureCheckResult(
        test_id=test.test_id,
        deployed_version=deployed.version,
        passed=passed,
        expected=test.expected,
        actual=actual,
        failure=None,
        detail=None if passed else "signature verification failed for a delivery signed by the provider",
    )


def _failed(
    test: FixtureTest, deployed: DeployedMerchant, *, failure: FailureReport | None, detail: str
) -> FixtureCheckResult:
    return FixtureCheckResult(
        test_id=test.test_id,
        deployed_version=deployed.version,
        passed=False,
        expected=test.expected,
        actual=None,
        failure=failure,
        detail=detail,
    )
