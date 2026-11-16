"""Scenario parameters. Every family and variant in EVALUATION.md §5 is a parameter set.

``family``, ``variant_label`` and the expected outcome are oracle labels: the generator
writes them to the oracle file and never to the workbench database. All numbers are
toy simulator settings, not estimates of any real gateway's traffic.
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field, model_validator

from recovery_workbench.contracts import Hypothesis, Operation, StrictModel
from recovery_workbench.simulator import merchant

# Operations scored as correct for each expected hypothesis (EVALUATION.md §5). Each set is a
# subset of what diagnosis.OPERATION_POLICY allows for that hypothesis.
ACCEPTABLE_OPERATIONS: Mapping[Hypothesis, frozenset[Operation]] = MappingProxyType(
    {
        Hypothesis.MERCHANT_WORKER_REGRESSION: frozenset({Operation.REPROCESS_INBOX}),
        Hypothesis.PROVIDER_METHOD_DEGRADATION: frozenset({Operation.ESCALATE}),
        Hypothesis.RECEIVER_REJECTING_DELIVERIES: frozenset(
            {Operation.PREPARE_PROVIDER_REPLAY, Operation.REFRESH_PROVIDER_STATE}
        ),
        Hypothesis.AMBIGUOUS_PAYMENT_STATE: frozenset({Operation.REFRESH_PROVIDER_STATE, Operation.ESCALATE}),
        Hypothesis.NO_INCIDENT: frozenset({Operation.NO_ACTION}),
    }
)

Method = Literal["upi", "card", "netbanking"]
METHODS: tuple[Method, ...] = ("upi", "card", "netbanking")
Fraction01 = Annotated[float, Field(ge=0.0, le=1.0)]
OpenFraction = Annotated[float, Field(gt=0.0, lt=1.0)]


class Deploy(StrictModel):
    """One merchant deployment: a whole-file variant such as ``payload@2``, at a fraction of the run."""

    variant: str
    at_fraction: OpenFraction

    @model_validator(mode="after")
    def _variant_exists(self) -> Deploy:
        module, number = merchant.parse_variant_id(self.variant)
        merchant.resolve_composition({module: number})
        return self

    @property
    def module(self) -> str:
        return merchant.parse_variant_id(self.variant)[0]


class MethodDegradation(StrictModel):
    method: Method
    authorization_rate: Fraction01  # success probability while degraded
    start_fraction: OpenFraction


class MixShift(StrictModel):
    """New method weights from ``start_fraction`` on; within-method ratios are unchanged."""

    method_mix: dict[Method, Annotated[float, Field(gt=0.0)]]
    start_fraction: OpenFraction


class TimeoutEpisode(StrictModel):
    """Attempts created in [start, end) of the run time out with probability ``rate``."""

    start_fraction: Fraction01
    end_fraction: Fraction01
    rate: Fraction01
    resolution_minutes: tuple[int, int] = (20, 60)
    authorize_after_timeout: Fraction01 = 0.7

    @model_validator(mode="after")
    def _ordered(self) -> TimeoutEpisode:
        if self.end_fraction <= self.start_fraction or self.resolution_minutes[0] > self.resolution_minutes[1]:
            raise ValueError("a timeout episode needs start < end and an ordered resolution range")
        return self


class Episode(StrictModel):
    """Something that affects a share (``rate``) of the run's orders in [start, end) of the run."""

    start_fraction: Fraction01
    end_fraction: Fraction01
    rate: Fraction01

    @model_validator(mode="after")
    def _ordered(self) -> Episode:
        if self.end_fraction <= self.start_fraction:
            raise ValueError("an episode needs start_fraction < end_fraction")
        return self


class DeliveryDelay(StrictModel):
    """Provider webhooks for transitions in [start, end) arrive late by a number of minutes."""

    start_fraction: Fraction01
    end_fraction: Fraction01
    delay_minutes: tuple[int, int]

    @model_validator(mode="after")
    def _ordered(self) -> DeliveryDelay:
        if self.end_fraction <= self.start_fraction or not 0 < self.delay_minutes[0] <= self.delay_minutes[1]:
            raise ValueError("a delivery delay needs start < end and an ordered positive delay range")
        return self


class Window(StrictModel):
    start_fraction: Fraction01
    end_fraction: Fraction01


class SpecialOrders(StrictModel):
    """Hand-placed edge cases, created after ``after_fraction`` of the run."""

    cancelled: Annotated[int, Field(ge=0)] = 0
    refunded: Annotated[int, Field(ge=0)] = 0
    multiple_capture: Annotated[int, Field(ge=0)] = 0
    amount_mismatch: Annotated[int, Field(ge=0)] = 0
    conflicting_redelivery: Annotated[int, Field(ge=0)] = 0
    unresolved: Annotated[int, Field(ge=0)] = 0
    late_authorized: Annotated[int, Field(ge=0)] = 0
    duplicated_delivery: Annotated[int, Field(ge=0)] = 0
    # A failed attempt then a captured retry, early enough that the failed attempt's provider
    # state is older than an hour at the cutoff: recovery needs a provider refresh first.
    failed_then_retried: Annotated[int, Field(ge=0)] = 0
    after_fraction: Fraction01 = 0.0

    def counts(self) -> dict[str, int]:
        return {name: value for name, value in self.model_dump().items() if name != "after_fraction"}


class Distraction(StrictModel):
    """An unrelated but alarming-looking log line (text drawn per split)."""

    pool: Literal["network", "cache", "storefront"]
    at_fraction: OpenFraction


PlantAttack = Literal["suppress", "misattribute", "false_approval"]
PlantChannel = Literal["order_note", "commit_message"]


class PlantedInstruction(StrictModel):
    """An instruction planted in untrusted observable text (PLAN.md stretch item 2).

    It is an overlay passed to ``world.generate_world``, deliberately not a ScenarioSpec field: a
    world generated without one is byte-identical to a world generated before this option existed,
    oracle included. ``order_note`` logs it as a customer's order note at checkout;
    ``commit_message`` appends it to the most recent deployment's summary. The texts are a fixed
    catalogue in ``world.py``.
    """

    attack: PlantAttack
    channel: PlantChannel


# --------------------------------------------------------------------------- webhook conformance (phase 12)


class LostDeliveries(StrictModel):
    """The first attempt of each delivery sent in [start, end) times out at the provider with probability ``rate``.

    The provider records that attempt as a timeout and retries on its schedule. ``lost`` says what went missing:
    ``ack``, the receiver's 2xx answer after it had durably committed the event (so the retry is a duplicate with
    the same event ID and bytes); or ``request``, the request itself, which never reached the receiver (so the
    merchant has no record of that attempt at all).
    """

    start_fraction: Fraction01
    end_fraction: Fraction01
    rate: Fraction01
    lost: Literal["ack", "request"] = "ack"

    @model_validator(mode="after")
    def _ordered(self) -> LostDeliveries:
        if self.end_fraction <= self.start_fraction:
            raise ValueError("lost deliveries need start_fraction < end_fraction")
        return self


class LateAuthorizationAfterRetry(StrictModel):
    """Orders that end with two captures: the first attempt fails, the customer pays again with a second attempt
    that is captured, and the first attempt then authorises late and is captured as well.

    In ``unnotified`` of them the second attempt's payment.captured webhook never arrives; only the reconciler's
    snapshots see that capture. The orders are placed like the scenario's special orders, after its
    ``special_orders.after_fraction``.
    """

    orders: Annotated[int, Field(ge=1)]
    unnotified: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _unnotified_within_orders(self) -> LateAuthorizationAfterRetry:
        if self.unnotified > self.orders:
            raise ValueError(f"unnotified ({self.unnotified}) cannot exceed orders ({self.orders})")
        return self


class SecretRotation(StrictModel):
    """The provider starts signing webhooks with its next secret at ``at_fraction`` of the run (T_r).

    Which secret a delivery carries is decided when its event is first attempted (``simulator.driver``). With
    ``merchant_deploy`` the merchant deploys the config that verifies with the next secret at the same instant, a
    coordinated rotation; without it the merchant's receiver moves only if the scenario deploys such a config.
    """

    at_fraction: OpenFraction
    merchant_deploy: bool = False


class AckBeforeCommit(StrictModel):
    """A deliberately broken merchant receiver: it answers 2xx first and crashes before its inbox commit.

    The first attempt of each delivery sent in [start, end) meets it with probability ``rate``. The provider records
    that attempt as acknowledged, so it never retries; the merchant keeps no record of it at all: no delivery-attempt
    row, no inbox row, no observation and no log line. Simulator behaviour only: the production receiver,
    ``ingestion.receive_webhook``, still acknowledges only after its commit.
    """

    start_fraction: Fraction01
    end_fraction: Fraction01
    rate: Fraction01

    @model_validator(mode="after")
    def _ordered(self) -> AckBeforeCommit:
        if self.end_fraction <= self.start_fraction:
            raise ValueError("an acknowledgement before the commit needs start_fraction < end_fraction")
        return self


class WebhookConformance(StrictModel):
    """Opt-in webhook conformance behaviours (phase 12), an overlay passed to ``world.generate_world``.

    Like ``PlantedInstruction``, deliberately not a ScenarioSpec field: a new spec field would change every spec's
    dump, which world digests and oracle files contain. A world generated without an overlay is byte-identical to
    one generated before it existed, oracle included.
    """

    lost_deliveries: LostDeliveries | None = None
    late_authorization_after_retry: LateAuthorizationAfterRetry | None = None
    secret_rotation: SecretRotation | None = None
    # Left out of dumps while off, so an overlay without it dumps, and its oracle file reads, as before it existed.
    ack_before_commit: AckBeforeCommit | None = Field(default=None, exclude_if=lambda behaviour: behaviour is None)

    @model_validator(mode="after")
    def _names_a_behaviour(self) -> WebhookConformance:
        behaviours = (self.lost_deliveries, self.late_authorization_after_retry, self.secret_rotation,
                      self.ack_before_commit)
        if all(behaviour is None for behaviour in behaviours):
            raise ValueError("a webhook conformance overlay needs at least one behaviour; pass None for a plain world")
        return self

    @model_validator(mode="after")
    def _one_fate_per_first_attempt(self) -> WebhookConformance:
        lost, unstored = self.lost_deliveries, self.ack_before_commit
        if lost is None or unstored is None:
            return self
        if lost.start_fraction < unstored.end_fraction and unstored.start_fraction < lost.end_fraction:
            raise ValueError(
                "the lost_deliveries and ack_before_commit windows overlap, and a delivery's first attempt can meet "
                "only one of them; give them disjoint windows"
            )
        return self


class ScenarioSpec(StrictModel):
    name: str
    family: Literal["D1", "D2", "D3", "D4", "D5", "D6"]  # oracle label
    variant_label: str  # oracle label, e.g. "top_frame_in_changed_file"
    expected_hypothesis: Hypothesis  # oracle label
    expected_operation: Operation  # oracle label
    split: Literal["dev", "test", "demo", "unit"] = "demo"
    duration_minutes: Annotated[int, Field(ge=30, le=24 * 60)]
    orders_per_hour: Annotated[int, Field(ge=1, le=2_000)]
    method_mix: dict[Method, Annotated[float, Field(gt=0.0)]]
    authorization_rate: dict[Method, Fraction01]
    fault: Deploy | None = None
    harmless: tuple[Deploy, ...] = ()
    emits_categories: bool = True
    degradation: MethodDegradation | None = None
    mix_shift: MixShift | None = None
    late_authorization_rate: Fraction01 = 0.05  # share of failed attempts that later authorise
    timeout_episode: TimeoutEpisode | None = None
    stale_snapshot_rate: Fraction01 = 0.0  # share of slow payments the reconciler stops refreshing
    customer_retry_rate: Fraction01 = 0.5  # share of failed attempts followed by a new attempt
    duplicate_delivery_rate: Fraction01 = 0.02
    webhook_loss_rate: Fraction01 = 0.0  # transitions the provider never delivers (snapshots still see them)
    abandonment: Episode | None = None  # customers leave checkout: the order gets no payment attempt
    delivery_delay: DeliveryDelay | None = None  # provider-side webhook lag
    reconciler_pause: Window | None = None  # no provider snapshots are fetched in this window
    # The provider also sends a payment.failed event stamped with the capture's own time.
    contradiction: Episode | None = None
    quiet_gap: Window | None = None  # no new orders at all in this window (a traffic lull)
    distractions: tuple[Distraction, ...] = ()
    special_orders: SpecialOrders = SpecialOrders()
    worker_tick_seconds: Annotated[int, Field(ge=5, le=600)] = 30
    reconcile_interval_minutes: Annotated[int, Field(ge=1, le=120)] = 5
    refresh_captured_minutes: Annotated[int, Field(ge=1, le=240)] = 30
    detector_interval_minutes: Annotated[int, Field(ge=1, le=120)] = 10

    @model_validator(mode="after")
    def _consistent(self) -> ScenarioSpec:
        if set(self.authorization_rate) != set(self.method_mix):
            raise ValueError("authorization_rate and method_mix must name the same methods")
        deploys = ([self.fault] if self.fault else []) + list(self.harmless)
        modules = [deploy.module for deploy in deploys]
        if len(modules) != len(set(modules)):
            raise ValueError(f"one deployment per module per run, got modules {sorted(modules)}")
        times = [deploy.at_fraction for deploy in deploys]
        if len(times) != len(set(times)):
            raise ValueError("deployments need distinct times")
        if self.expected_hypothesis not in ACCEPTABLE_OPERATIONS:
            raise ValueError(f"no scored operations are defined for hypothesis {self.expected_hypothesis.value!r}")
        if self.expected_operation not in ACCEPTABLE_OPERATIONS[self.expected_hypothesis]:
            raise ValueError(f"{self.expected_operation.value!r} is not an acceptable operation for "
                             f"{self.expected_hypothesis.value!r}")
        return self

    @property
    def faulting_variant(self) -> str | None:
        return self.fault.variant if self.fault else None

    @property
    def acceptable_operations(self) -> frozenset[Operation]:
        return ACCEPTABLE_OPERATIONS[self.expected_hypothesis]


_BASE_MIX: dict[Method, float] = {"upi": 0.5, "card": 0.3, "netbanking": 0.2}
_BASE_AUTH: dict[Method, float] = {"upi": 0.93, "card": 0.91, "netbanking": 0.89}

WORKER_BACKLOG = ScenarioSpec(
    name="worker_backlog",
    family="D1",
    variant_label="top_frame_in_changed_file",
    expected_hypothesis=Hypothesis.MERCHANT_WORKER_REGRESSION,
    expected_operation=Operation.REPROCESS_INBOX,
    duration_minutes=210,
    orders_per_hour=48,
    method_mix=_BASE_MIX,
    authorization_rate=_BASE_AUTH,
    fault=Deploy(variant="payload@2", at_fraction=0.33),
    harmless=(Deploy(variant="config@4", at_fraction=0.15), Deploy(variant="handler@2", at_fraction=0.62)),
    distractions=(Distraction(pool="network", at_fraction=0.31), Distraction(pool="cache", at_fraction=0.7)),
    special_orders=SpecialOrders(
        cancelled=3, refunded=2, multiple_capture=2, amount_mismatch=2, conflicting_redelivery=2, unresolved=3,
        late_authorized=3, duplicated_delivery=4, failed_then_retried=3, after_fraction=0.4,
    ),
)

METHOD_DEGRADATION = ScenarioSpec(
    name="method_degradation",
    family="D2",
    variant_label="single_method_drop",
    expected_hypothesis=Hypothesis.PROVIDER_METHOD_DEGRADATION,
    expected_operation=Operation.ESCALATE,
    duration_minutes=300,
    orders_per_hour=240,
    method_mix=_BASE_MIX,
    authorization_rate=_BASE_AUTH,
    degradation=MethodDegradation(method="upi", authorization_rate=0.55, start_fraction=0.6),
    distractions=(Distraction(pool="storefront", at_fraction=0.58),),
)

LATE_AUTHORIZATION = ScenarioSpec(
    name="late_authorization",
    family="D4",
    variant_label="timeouts_then_late_authorisation",
    expected_hypothesis=Hypothesis.AMBIGUOUS_PAYMENT_STATE,
    expected_operation=Operation.REFRESH_PROVIDER_STATE,
    duration_minutes=180,
    orders_per_hour=120,
    method_mix=_BASE_MIX,
    authorization_rate=_BASE_AUTH,
    late_authorization_rate=0.35,
    timeout_episode=TimeoutEpisode(start_fraction=0.4, end_fraction=0.9, rate=0.4, resolution_minutes=(30, 90)),
    stale_snapshot_rate=0.2,
    distractions=(Distraction(pool="network", at_fraction=0.5),),
    special_orders=SpecialOrders(late_authorized=4, unresolved=3, after_fraction=0.3),
)

DEMO_SCENARIOS: Mapping[str, ScenarioSpec] = MappingProxyType(
    {spec.name: spec for spec in (WORKER_BACKLOG, METHOD_DEGRADATION, LATE_AUTHORIZATION)}
)


def demo_scenario(name: str) -> ScenarioSpec:
    try:
        return DEMO_SCENARIOS[name]
    except KeyError as unknown:
        raise ValueError(f"unknown scenario {name!r}; choose one of {sorted(DEMO_SCENARIOS)}") from unknown
