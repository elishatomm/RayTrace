"""The six scored diagnosis families (EVALUATION.md §5) as ScenarioSpec builders.

``fixtures/manifests/evaluation_manifest.json`` lists 48 runs (6 families x 8: 3 dev, 5 test),
each with a family, a variant label and the variant parameters. ``spec_for`` turns one entry
into a ScenarioSpec. Every evaluation run uses the neutral scenario name ``eval``, so neither
the run ID nor the scenario column says which family a run belongs to. Family, variant and
the expected outcome live only in the manifest and the per-run oracle file.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import JsonValue

from recovery_workbench.contracts import Hypothesis, Operation, StrictModel
from recovery_workbench.simulator.scenarios import (
    DeliveryDelay,
    Deploy,
    Distraction,
    Episode,
    Method,
    MethodDegradation,
    MixShift,
    ScenarioSpec,
    TimeoutEpisode,
    Window,
)

MANIFEST_PATH = Path(__file__).resolve().parents[3] / "fixtures" / "manifests" / "evaluation_manifest.json"
EVALUATION_SCENARIO_NAME = "eval"
FAMILIES: tuple[str, ...] = ("D1", "D2", "D3", "D4", "D5", "D6")

# Fault class of each faulting variant (the same labels as merchant_variant_truth.json).
D1_FAULT_CLASSES = MappingProxyType(
    {"payload@2": "top_frame_in_changed_file", "payload@3": "upstream_of_failing_frame", "config@2": "configuration_only"}
)
# Held-out D1 faults (the same labels as heldout_variant_truth.json). Kept apart from D1_FAULT_CLASSES because they
# were written after the attribution approaches were developed; only fixtures/manifests/heldout_manifest.json uses them.
HELDOUT_D1_FAULT_CLASSES = MappingProxyType(
    {
        "payload@7": "heldout_upstream_value_rejected",
        "payload@8": "heldout_cross_module_contract",
        "payload@9": "heldout_same_file_raises",
        "config@8": "heldout_configuration_only",
        "orders@4": "heldout_refactor_changes_behaviour",
        "orders@6": "heldout_unit_mismatch_partial",
    }
)
_D1_CLASS_BY_FAULT = MappingProxyType({**D1_FAULT_CLASSES, **HELDOUT_D1_FAULT_CLASSES})
D3_FAULT_CLASSES = MappingProxyType({"receiver@2": "receiver_code", "config@3": "receiver_config"})
DEGRADED_AUTHORIZATION = MappingProxyType({"severe": 0.45, "moderate": 0.65})

_BASE_MIX: dict[Method, float] = {"upi": 0.5, "card": 0.3, "netbanking": 0.2}
_BASE_AUTH: dict[Method, float] = {"upi": 0.93, "card": 0.91, "netbanking": 0.89}


class FamilyRun(StrictModel):
    """One frozen evaluation run: which family and variant, with what parameters and seed."""

    run_key: str
    family: Literal["D1", "D2", "D3", "D4", "D5", "D6"]
    variant: str
    split: Literal["dev", "test"]
    seed: int
    params: dict[str, JsonValue]


class EvaluationManifest(StrictModel):
    manifest_version: str
    generator_version: str
    notice: str = ""
    runs: tuple[FamilyRun, ...]


def load_manifest(path: Path = MANIFEST_PATH) -> EvaluationManifest:
    return EvaluationManifest.model_validate(json.loads(path.read_text(encoding="utf-8")))


def spec_for(run: FamilyRun) -> ScenarioSpec:
    builder = _BUILDERS[run.family]
    spec = builder(run.params, run.split)
    if spec.variant_label != run.variant:
        raise ValueError(f"{run.run_key}: parameters describe variant {spec.variant_label!r}, manifest says {run.variant!r}")
    return spec


# --------------------------------------------------------------------------- shared pieces


def _spec(
    family: str, variant: str, split: str, hypothesis: Hypothesis, operation: Operation, **fields: object
) -> ScenarioSpec:
    values: dict[str, object] = {
        "name": EVALUATION_SCENARIO_NAME, "family": family, "variant_label": variant, "split": split,
        "expected_hypothesis": hypothesis, "expected_operation": operation,
        "method_mix": _BASE_MIX, "authorization_rate": _BASE_AUTH,
    }
    values.update(fields)
    return ScenarioSpec.model_validate(values)


def _text(params: Mapping[str, JsonValue], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str):
        raise ValueError(f"parameter {key!r} must be a string, got {value!r}")
    return value


def _harmless(  # NOSONAR(S8495) zero or one innocent deploy by design; the result is typed tuple[Deploy, ...]
    params: Mapping[str, JsonValue], *, before: float, after: float, gap: float | None = None
) -> tuple[Deploy, ...]:
    """An innocent deploy before or after the fault, inside a traffic lull, or none.

    "After" lands a few minutes after the fault, before the incident opens, so at the diagnosis
    cutoff the innocent deploy is the most recent one. "Gap" (delayed onset only) lands inside
    the lull, after the faulting deploy and before the first failure.
    """
    position = params.get("harmless_position", "none")
    if position == "none":
        return ()
    fractions = {"before": before, "after": after, "gap": gap}
    if position not in fractions or fractions[position] is None:
        raise ValueError(f"harmless_position must be one of {sorted(k for k, v in fractions.items() if v)}, got {position!r}")
    return (Deploy(variant=_text(params, "harmless"), at_fraction=fractions[position]),)  # type: ignore[arg-type]


def _degradation_mix(method: str) -> dict[Method, float]:
    """The degraded method carries 45% of traffic so its cohort clears the detector minimums."""
    others = [m for m in ("upi", "card", "netbanking") if m != method]
    return {method: 0.45, others[0]: 0.3, others[1]: 0.25}  # type: ignore[dict-item]


# --------------------------------------------------------------------------- family builders


_D1_MINUTES = 210
_D1_FAULT_AT = 0.35


def _d1(params: Mapping[str, JsonValue], split: str) -> ScenarioSpec:
    fault = _text(params, "fault")
    if fault not in _D1_CLASS_BY_FAULT:
        raise ValueError(f"D1 fault must be one of {sorted(_D1_CLASS_BY_FAULT)}, got {fault!r}")
    onset = params.get("onset", "prompt")
    delayed: dict[str, object] = {}
    gap_deploy: float | None = None
    if onset == "delayed":
        # The faulting deploy lands 10 minutes into a 45-minute traffic lull and an innocent deploy
        # lands inside the lull 12 minutes later. Failures begin only when shoppers return, after
        # both deploys, so "latest change before the first failure" names the innocent one.
        fault_minute = _D1_FAULT_AT * _D1_MINUTES
        delayed = {
            "quiet_gap": Window(start_fraction=(fault_minute - 10) / _D1_MINUTES,
                                end_fraction=(fault_minute + 35) / _D1_MINUTES),
            "late_authorization_rate": 0.0,  # nothing may reach the worker during the lull
        }
        gap_deploy = (fault_minute + 12) / _D1_MINUTES
    elif onset != "prompt":
        raise ValueError(f"D1 onset must be prompt or delayed, got {onset!r}")
    return _spec(
        "D1", _D1_CLASS_BY_FAULT[fault], split, Hypothesis.MERCHANT_WORKER_REGRESSION, Operation.REPROCESS_INBOX,
        duration_minutes=_D1_MINUTES, orders_per_hour=48, fault=Deploy(variant=fault, at_fraction=_D1_FAULT_AT),
        harmless=_harmless(params, before=0.2, after=0.37, gap=gap_deploy),
        emits_categories=bool(params.get("categories", True)),
        distractions=(Distraction(pool="network", at_fraction=0.33), Distraction(pool="cache", at_fraction=0.7)),
        **delayed,
    )


def _degradation_spec(family: str, variant: str, params: Mapping[str, JsonValue], split: str,
                      harmless: tuple[Deploy, ...]) -> ScenarioSpec:
    method = _text(params, "method")
    magnitude = _text(params, "magnitude")
    return _spec(
        family, variant, split, Hypothesis.PROVIDER_METHOD_DEGRADATION, Operation.ESCALATE,
        duration_minutes=300, orders_per_hour=260, method_mix=_degradation_mix(method),
        degradation=MethodDegradation(method=method, authorization_rate=DEGRADED_AUTHORIZATION[magnitude],
                                      start_fraction=0.62),
        harmless=harmless, distractions=(Distraction(pool="storefront", at_fraction=0.6),),
    )


def _d2(params: Mapping[str, JsonValue], split: str) -> ScenarioSpec:
    return _degradation_spec("D2", f"{_text(params, 'magnitude')}_method_drop", params, split, ())


def _d3(params: Mapping[str, JsonValue], split: str) -> ScenarioSpec:
    fault = _text(params, "fault")
    if fault not in D3_FAULT_CLASSES:
        raise ValueError(f"D3 fault must be one of {sorted(D3_FAULT_CLASSES)}, got {fault!r}")
    return _spec(
        "D3", D3_FAULT_CLASSES[fault], split, Hypothesis.RECEIVER_REJECTING_DELIVERIES,
        Operation.PREPARE_PROVIDER_REPLAY, duration_minutes=150, orders_per_hour=60,
        fault=Deploy(variant=fault, at_fraction=0.5), harmless=_harmless(params, before=0.3, after=0.51),
        distractions=(Distraction(pool="network", at_fraction=0.48),),
    )


def _d4(params: Mapping[str, JsonValue], split: str) -> ScenarioSpec:
    mode = _text(params, "mode")
    common: dict[str, object] = {"duration_minutes": 180, "orders_per_hour": 120,
                                 "distractions": (Distraction(pool="cache", at_fraction=0.5),)}
    timeouts = TimeoutEpisode(start_fraction=0.4, end_fraction=0.9, rate=0.4, resolution_minutes=(30, 90))
    if mode == "late_authorization":
        common |= {"late_authorization_rate": 0.35, "timeout_episode": timeouts, "stale_snapshot_rate": 0.2}
    elif mode == "stale_snapshot":
        common |= {"late_authorization_rate": 0.1, "timeout_episode": timeouts, "stale_snapshot_rate": 0.7}
    elif mode == "same_time_contradiction":
        common |= {"contradiction": Episode(start_fraction=0.45, end_fraction=0.95, rate=0.35)}
    else:
        raise ValueError(f"D4 mode must be late_authorization, stale_snapshot or same_time_contradiction, got {mode!r}")
    return _spec("D4", mode, split, Hypothesis.AMBIGUOUS_PAYMENT_STATE, Operation.REFRESH_PROVIDER_STATE, **common)


def _no_incident_fields(mode: str) -> dict[str, object]:
    if mode == "mix_shift":  # per-method ratios never change; the aggregate falls as traffic moves
        return {
            "duration_minutes": 240, "orders_per_hour": 150, "customer_retry_rate": 0.1,
            "authorization_rate": {"upi": 0.95, "card": 0.92, "netbanking": 0.40},
            "method_mix": {"upi": 0.6, "card": 0.3, "netbanking": 0.1},
            "mix_shift": MixShift(method_mix={"upi": 0.15, "card": 0.15, "netbanking": 0.7}, start_fraction=0.55),
        }
    if mode == "sparse_cohort":  # few orders, and many shoppers leave before paying
        return {"duration_minutes": 240, "orders_per_hour": 36,
                "abandonment": Episode(start_fraction=0.6, end_fraction=1.0, rate=0.55)}
    if mode == "missing_observations":  # webhooks arrive late and the reconciler is paused
        return {"duration_minutes": 240, "orders_per_hour": 48,
                "delivery_delay": DeliveryDelay(start_fraction=0.55, end_fraction=1.0, delay_minutes=(45, 90)),
                "reconciler_pause": Window(start_fraction=0.55, end_fraction=1.0)}
    raise ValueError(f"D5 mode must be mix_shift, sparse_cohort or missing_observations, got {mode!r}")


def _d5(params: Mapping[str, JsonValue], split: str) -> ScenarioSpec:
    mode = _text(params, "mode")
    return _spec("D5", mode, split, Hypothesis.NO_INCIDENT, Operation.NO_ACTION, **_no_incident_fields(mode))


def _d6(params: Mapping[str, JsonValue], split: str) -> ScenarioSpec:
    base = _text(params, "base")
    innocent = _text(params, "innocent")
    if base == "D2":
        return _degradation_spec("D6", "innocent_deploy_near_method_drop", params, split,
                                 (Deploy(variant=innocent, at_fraction=0.58),))
    if base == "D5":
        return _spec("D6", "innocent_deploy_near_mix_shift", split, Hypothesis.NO_INCIDENT, Operation.NO_ACTION,
                     harmless=(Deploy(variant=innocent, at_fraction=0.52),), **_no_incident_fields("mix_shift"))
    raise ValueError(f"D6 base must be D2 or D5, got {base!r}")


_BUILDERS: Mapping[str, Callable[[Mapping[str, JsonValue], str], ScenarioSpec]] = MappingProxyType(
    {"D1": _d1, "D2": _d2, "D3": _d3, "D4": _d4, "D5": _d5, "D6": _d6}
)
