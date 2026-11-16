"""Command line: ``python -m recovery_workbench.cli <command>``. Synthetic sandbox only.

Run setup and inspection:

* ``seed --scenario NAME --seed N`` generates a synthetic run and replays it through the real
  workbench; ``status --run-id ID`` prints measures, backlog, failures, deployments and incidents.

Diagnosis and recovery (each operator action happens one replay minute after the previous one):

* ``diagnose --run-id ID --mode local|recorded|rules|hosted|auto`` builds the evidence packet (``pkt-2``) and
  diagnoses it;
* ``fixes`` lists the allowlisted corrected handlers (the same list in every run);
* ``validate-fix``, ``plan``, ``approve`` and ``execute`` run Flow A;
* ``escalate`` records an exception and the evidence needed; ``refresh`` fetches read-only provider state;
* ``report --run-id ID --output FILE`` exports the run;
* ``receipt --run-id ID --action-id ACT --output-dir DIR`` writes one recovery action's receipt as JSON and
  Markdown, and prints its hash and the export time, which is never written into the receipt;
* ``demo --scenario NAME --seed N --model-mode MODE`` runs one scripted flow end to end and prints
  each step. It follows the operation the diagnosis proposes, never the scenario's hidden labels.

Webhook deliveries over HTTP:

* ``webhook-request --run-id ID --payment-id PAY --status STATUS --event-id EVT --output FILE`` signs
  one provider-shaped event for an existing payment as the simulated provider signs, writes its raw
  body to FILE and prints the two headers and a ``curl`` command for the API's webhook route. It only
  reads the database and sends nothing itself.

``data_dir`` comes from ``settings.load_settings()`` unless ``--data-dir`` is given. A data directory that public mode
marked as its own is refused, because public mode empties it at its next start. Exit codes:
0 success, 2 refused or invalid input, 3 a stale or rejected plan (HTTP 409 in the API).

A command that diagnoses or seeds in mode ``auto`` resolves it once, as it starts (``model_source``), and prints the
source it chose to stderr, as it does for ``hosted``; stdout stays the command's own output. A hosted diagnosis counts
its calls against a daily budget of the command's own, since each command is a process.

A command reads the freshness threshold and the plan and approval lifetimes (``WORKBENCH_STALE_AFTER_SECONDS``,
``WORKBENCH_PLAN_TTL_SECONDS``, ``WORKBENCH_APPROVAL_TTL_SECONDS``) when it runs. The helpers the API shares
(``build_plan``, ``approve_plan``, ``execute_plan`` and ``late_authorisation_facts``) also take them as
``stale_after_ms`` and ``ttl_ms``, and the API passes its server's settings object's values (owner decisions Q31
and Q35).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shlex
import sqlite3
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from recovery_workbench import (
    diagnosis,
    diagnosis_store,
    executor,
    fixes,
    hosted_model,
    metrics,
    model_source,
    plans,
    public_mode,
    refresh,
    report,
    settings,
    storage,
    tools,
)
from recovery_workbench.contracts import (
    ContractError,
    DiagnosisStatus,
    ExclusionReason,
    ModelMode,
    Operation,
    PaymentStatus,
    ScopeError,
    StaleStateError,
    WorkbenchError,
)
from recovery_workbench.simulator import driver, merchant, runtime, scenarios, signing
from recovery_workbench.simulator.provider import SimulatedDeliveryLog, SimulatedProviderAdapter, delivery_log_path
from recovery_workbench.simulator.world import INTEGRATION_ID, generate_world

DATABASE_FILE = "workbench.sqlite"
ORACLE_DIRECTORY = "oracle"
PROVIDER_DIRECTORY = "provider"
RECORDINGS_DIRECTORY = "recordings"
REPORTS_DIRECTORY = "reports"
EXIT_USAGE_ERROR = 2
EXIT_REJECTED = 3
# Operator actions take replay time: each mutating step happens this long after the previous one,
# so nothing it records lands at the instant an earlier packet was cut.
OPERATOR_STEP_MS = 60_000
DEMO_OPERATOR = "op_demo_operator"
OPERATOR_HELP = "an operator session ID, op_..."
API_PORT = 8000  # the API server's default loopback port; api.DEFAULT_PORT reads it from here
# A refresh re-checks at most this many recently failed payments after the (at most 50) open or late-authorised
# ones, so the UI's single refresh request stays within the API's 200 payments (owner decision Q26).
RECENT_FAILURE_LIMIT = 150
MODE_CHOICES = [*(mode.value for mode in ModelMode), model_source.AUTO]
NOTHING_TO_REFRESH = "no open, late-authorised or recently failed payment needs fresh provider state"
NO_CAPTURE_TO_CHECK = "every payment the provider shows captured has a stored payment.captured event"
NO_DELIVERY_LOG_RECORDS = (
    "the run's provider records carry no delivery log, so it was not read; a capture the provider already recorded as "
    "delivered may be a target that its dashboard refuses"
)
NO_PROVIDER_RECORDS = (
    "this server has no provider records for the run, so the provider's delivery log was not read; a capture the "
    "provider already recorded as delivered may be a target that its dashboard refuses"
)
# The receiver refuses any other event ID (ingestion), so the helper refuses it before signing.
WEBHOOK_EVENT_ID = re.compile(r"[A-Za-z0-9_.:\-]{1,128}")
WEBHOOK_STATUSES: tuple[str, ...] = tuple(status.value for status in signing.EVENT_TYPE_BY_STATUS)
WEBHOOK_REQUEST_EPILOG = """\
How to get each outcome from the API's webhook route (start the API, then run the printed curl command):
  accepted     send the file once: 200; the event waits in the inbox as pending (the route runs no worker)
  duplicate    send the same file again: 200; the inbox still holds one row for the event
  401          change one byte of the body file, then send it: the signature no longer matches
  conflicting  run this command again with the same --event-id and another --status, then send the new
               file: 200; the delivery is held in conflicting_deliveries and never processed
The event carries the order's amount and currency and the run's replay time, and is signed as the
simulated provider signs, with the provider's secret. If the run's deployed receiver verifies with
another secret, or changes the body before it checks the signature, even the first send answers 401,
as the provider's own deliveries do. This command only reads the database; it sends nothing."""


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command: Callable[[argparse.Namespace], int] = args.command_handler
    try:
        return command(args)
    except executor.PlanRejectedError as rejected:
        _print_error(rejected, {"result": rejected.result.model_dump(mode="json")})
        return EXIT_REJECTED
    except StaleStateError as stale:
        _print_error(stale)
        return EXIT_REJECTED
    except (WorkbenchError, ValueError, FileNotFoundError, FileExistsError) as problem:
        _print_error(problem)
        return EXIT_USAGE_ERROR


def _print_error(problem: BaseException, extra: dict[str, JsonValue] | None = None) -> None:
    print(json.dumps({"error": type(problem).__name__, "detail": str(problem), **(extra or {})}), file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m recovery_workbench.cli",
                                     description="RayTrace (synthetic sandbox only).")
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str, handler: Callable[[argparse.Namespace], int], *, run: bool = True,
                data_dir: bool = True, epilog: str | None = None) -> argparse.ArgumentParser:
        # The raw formatter keeps an epilog's line breaks; it changes nothing for commands without one.
        sub = commands.add_parser(name, help=help_text, epilog=epilog,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
        if run:
            sub.add_argument("--run-id", required=True)
        if data_dir:
            sub.add_argument("--data-dir", type=Path, default=None, help="defaults to WORKBENCH_DATA_DIR")
        sub.set_defaults(command_handler=handler)
        return sub

    seed = command("seed", "generate and replay a synthetic run", run_seed, run=False)
    seed.add_argument("--scenario", required=True, choices=sorted(scenarios.DEMO_SCENARIOS))
    seed.add_argument("--seed", required=True, type=int)

    status = command("status", "measures, backlog, failures, deployments and incidents of a run", run_status)
    status.add_argument("--as-of-ms", type=int, default=None, help="replay time; defaults to the run clock")

    diagnose = command("diagnose", "build the evidence packet and diagnose the run's incident", run_diagnose)
    diagnose.add_argument("--mode", required=True, choices=MODE_CHOICES)
    diagnose.add_argument("--incident-id", default=None, help="defaults to the latest incident")
    diagnose.add_argument("--recordings-dir", type=Path, default=None, help="defaults to <data-dir>/recordings")

    command("fixes", "list the allowlisted corrected handlers", run_fixes, run=False, data_dir=False)

    validate = command("validate-fix", "run the fixture tests on the deployed and corrected handler", run_validate_fix)
    validate.add_argument("--fix-id", required=True)
    validate.add_argument("--diagnosis-id", required=True)
    validate.add_argument("--operator", required=True, help=OPERATOR_HELP)

    plan = command("plan", "build a reprocess_inbox plan from a passing fix validation", run_plan)
    plan.add_argument("--fix-validation-id", required=True)

    approve = command("approve", "approve the exact plan hash you reviewed", run_approve)
    approve.add_argument("--plan-id", required=True)
    approve.add_argument("--plan-hash", required=True)
    approve.add_argument("--operator", required=True, help=OPERATOR_HELP)

    execute = command("execute", "execute an approved plan exactly once", run_execute)
    execute.add_argument("--plan-id", required=True)

    escalate = command("escalate", "record an exception and the evidence a person needs", run_escalate)
    escalate.add_argument("--diagnosis-id", required=True)
    escalate.add_argument("--operator", required=True, help=OPERATOR_HELP)

    refresh_parser = command("refresh", "fetch read-only provider state for named payments", run_refresh)
    refresh_parser.add_argument("--payment-id", required=True, action="append", dest="payment_ids")

    report_parser = command("report", "export the run's evidence, diagnosis, plan and outcome", run_report)
    report_parser.add_argument("--output", required=True, type=Path)

    receipt = command("receipt", "write one recovery action's receipt as JSON and Markdown", run_receipt)
    receipt.add_argument("--action-id", required=True, help="the action_id that execute printed")
    receipt.add_argument("--output-dir", required=True, type=Path)

    demo = command("demo", "run one scripted end-to-end flow and print each step", run_demo, run=False)
    demo.add_argument("--scenario", required=True, choices=sorted(scenarios.DEMO_SCENARIOS))
    demo.add_argument("--seed", required=True, type=int)
    demo.add_argument("--model-mode", required=True, choices=MODE_CHOICES)
    demo.add_argument("--recordings-dir", type=Path, default=None, help="defaults to <data-dir>/recordings")

    webhook = command("webhook-request", "sign a provider-shaped event for an existing payment and print a curl "
                      "command for the API's webhook route", run_webhook_request, epilog=WEBHOOK_REQUEST_EPILOG)
    webhook.add_argument("--payment-id", required=True, help="an existing payment of the run")
    webhook.add_argument("--status", required=True, choices=WEBHOOK_STATUSES)
    webhook.add_argument("--event-id", required=True, type=_webhook_event_id,
                         help="1-128 letters, digits, '_', '.', ':' or '-'; reuse one to see a duplicate or conflict")
    webhook.add_argument("--output", required=True, type=Path, help="file to write the raw request body to")
    webhook.add_argument("--integration-id", default=INTEGRATION_ID, help=f"default {INTEGRATION_ID}")
    webhook.add_argument("--port", type=int, default=API_PORT, help=f"the API's loopback port (default {API_PORT})")
    return parser


def _webhook_event_id(text: str) -> str:
    if WEBHOOK_EVENT_ID.fullmatch(text) is None:
        raise argparse.ArgumentTypeError(
            f"{text!r} is not an event ID the receiver accepts; use 1-128 letters, digits, '_', '.', ':' or '-'"
        )
    return text


# --------------------------------------------------------------------------- shared helpers


def _data_dir(args: argparse.Namespace) -> Path:
    """The command's data directory; refused when public mode marked it, because public mode empties it at its next
    start. Every command that opens a data directory takes it from here."""
    data_dir = args.data_dir if args.data_dir is not None else settings.load_settings().data_dir
    public_mode.refuse_marked_data_dir(data_dir)
    return data_dir


@contextmanager
def _database(args: argparse.Namespace, *, migrate: bool = True) -> Iterator[sqlite3.Connection]:
    """The data directory's database, with any migration newer than the directory applied first, as the API
    does at start: a directory seeded before migration 3 could not plan, execute or give a receipt without it.
    ``migrate=False`` is for a command that promises to only read the database."""
    database = _data_dir(args) / DATABASE_FILE
    if not database.is_file():
        raise FileNotFoundError(f"no workbench database at {database}; run the seed command first")
    with closing(storage.connect(database)) as conn:
        if migrate:
            storage.migrate(conn)
        yield conn


def command_mode(requested: str) -> ModelMode:
    """The mode a command runs in. auto is resolved here, once, as the command starts; the source it chose, and a hosted
    one, are printed to stderr. Hosted without its settings is refused before anything is done."""
    if requested not in (model_source.AUTO, ModelMode.HOSTED.value):
        return ModelMode(requested)  # local, recorded and rules start as they always have
    configured = settings.load_settings()
    if requested == ModelMode.HOSTED.value and not configured.hosted.configured:
        raise ContractError(f"hosted mode needs {', '.join(configured.hosted.missing())} set; set all three, or choose "
                            "local, recorded, rules or auto")
    source = model_source.resolve(dataclasses.replace(configured, model_mode=requested))
    print(source.start_line(), file=sys.stderr)
    return source.mode


@contextmanager
def _hosted_client(configured: settings.Settings, mode: ModelMode) -> Iterator[hosted_model.HostedClient | None]:
    """The hosted client a hosted diagnosis needs, with this command's own daily budget, closed afterwards."""
    if mode is not ModelMode.HOSTED:
        yield None
        return
    budget = hosted_model.HostedCallBudget(configured.hosted.max_calls_per_day)
    with hosted_model.client_from_settings(configured, budget) as client:
        yield client


def _recordings_dir(args: argparse.Namespace) -> Path:
    return args.recordings_dir if args.recordings_dir is not None else _data_dir(args) / RECORDINGS_DIRECTORY


def _print(document: JsonValue) -> None:
    print(json.dumps(document, indent=2, sort_keys=True))


def next_operator_time(conn: sqlite3.Connection, run_id: str) -> int:
    """Advance the run's replay clock by one operator step and return the new time."""
    with storage.write_transaction(conn):
        run = storage.require_run(conn, run_id)
        return storage.advance_clock(conn, run_id, run.clock_ms + OPERATOR_STEP_MS)


def _deployed(conn: sqlite3.Connection, run_id: str, at_ms: int) -> merchant.DeployedMerchant:
    return runtime.active_merchant(conn, run_id=run_id, at_ms=at_ms)


def _freshness_threshold(stale_after_ms: int | None) -> int:
    """The caller's freshness threshold; without one, the environment's, read now."""
    return settings.load_settings().stale_after_seconds * 1000 if stale_after_ms is None else stale_after_ms


def latest_incident(conn: sqlite3.Connection, run_id: str) -> str:
    known = storage.list_incidents(conn, run_id=run_id)
    if not known:
        raise ValueError(f"run {run_id!r} has no incident to diagnose; check `status` for detector results")
    return known[-1].incident_id


def rupees(paise: int) -> str:
    """Presentation only: money stays integer paise everywhere else."""
    sign = "-" if paise < 0 else ""
    whole, fraction = divmod(abs(paise), 100)
    return f"{sign}INR {whole:,}.{fraction:02d}"


def diagnosis_document(stored: diagnosis_store.StoredDiagnosis) -> dict[str, JsonValue]:
    document: dict[str, JsonValue] = {
        "diagnosis_id": stored.diagnosis_id,
        "incident_id": stored.incident_id,
        "mode": stored.mode.value,
        "mode_label": stored.mode_label,
        "status": stored.status.value,
        "model_id": stored.model_id,
        "model_digest": stored.model_digest,
        "thinking": stored.thinking,
        "prompt_version": stored.prompt_version,
        "validator_version": stored.validator_version,
        "rules_version": stored.rules_version,
        "recorded_from": stored.recorded_from,
        "packet_hash": stored.packet_hash,
        "output": None if stored.output is None else stored.output.model_dump(mode="json"),
        "reasons": list(stored.reasons),
        "unsafe_proposals": [proposal.model_dump(mode="json") for proposal in stored.unsafe_proposals],
        "elapsed_ms": stored.elapsed_ms,
    }
    if stored.mode is ModelMode.HOSTED:  # hosted documents only, so every other one is as it was
        document["provider_host"] = stored.provider_host
    return document


# --------------------------------------------------------------------------- seed and status


def run_seed(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    data_dir.mkdir(parents=True, exist_ok=True)
    world = generate_world(scenarios.demo_scenario(args.scenario), args.seed)
    model_mode = command_mode(settings.load_settings().model_mode)
    database = data_dir / DATABASE_FILE
    with closing(storage.connect(database)) as conn:
        summary = driver.seed_world(conn, world, oracle_dir=data_dir / ORACLE_DIRECTORY, model_mode=model_mode)
    _print({**summary.as_json(), "database": str(database)})
    return 0


def run_status(args: argparse.Namespace) -> int:
    with _database(args) as conn:
        _print(status_report(conn, args.run_id, as_of_ms=args.as_of_ms))
    return 0


def status_report(conn: sqlite3.Connection, run_id: str, *, as_of_ms: int | None = None) -> dict[str, JsonValue]:
    """Observable state of a run as of ``as_of_ms`` (default: its replay clock). Reads no private tables."""
    run = storage.require_run(conn, run_id)
    as_of = run.clock_ms if as_of_ms is None else as_of_ms
    windows = metrics.DEFAULT_WINDOWS
    view = metrics.load_run_view(conn, run_id, as_of, stale_after_ms=windows.stale_after_ms)
    methods: list[str | None] = [*sorted({attempt.method for attempt in view.attempts}), None]
    inbox = storage.list_inbox_rows(conn, run_id=run_id)
    failures: dict[str, int] = {}
    for item in storage.list_worker_attempts(conn, run_id=run_id):
        if item.result.value == "failed" and item.finished_at_ms <= as_of:
            key = f"{item.worker_version} | {item.exception_category or 'no category'}"
            failures[key] = failures.get(key, 0) + 1
    return {
        "run": {"run_id": run.run_id, "scenario": run.scenario, "seed": run.seed, "split": run.split,
                "environment": run.environment, "clock_ms": run.clock_ms, "as_of_ms": as_of},
        "windows": {"horizon_ms": windows.horizon_ms, "window_ms": windows.window_ms,
                    "baseline_ms": windows.baseline_ms, "stale_after_ms": windows.stale_after_ms},
        "provider": {
            "current": [metrics.provider_measures(view, windows.current(as_of), method=m).as_json() for m in methods],
            "baseline": [metrics.provider_measures(view, windows.baseline(as_of), method=m).as_json() for m in methods],
        },
        "merchant": metrics.merchant_measures(view).as_json(),
        "backlog": {
            status: sum(1 for row in inbox if row.processing_status.value == status and row.received_at_ms <= as_of)
            for status in ("pending", "failed", "manual_review", "processed")
        },
        "worker_failures": dict(sorted(failures.items())),
        "deployments": [
            {"deploy_id": d.deploy_id, "service_id": d.service_id, "kind": d.kind, "deployed_at_ms": d.deployed_at_ms,
             "summary": d.summary, "files_changed": list(d.files_changed)}
            for d in storage.list_deployments(conn, run_id=run_id, as_of_ms=as_of)
        ],
        "incidents": [
            {"incident_id": i.incident_id, "trigger": i.trigger, "cohort": i.cohort,
             "evidence_cutoff_ms": i.evidence_cutoff_ms, "state": i.state,
             "fired_detectors": i.measured_impact.get("fired_detectors")}
            for i in storage.list_incidents(conn, run_id=run_id, as_of_ms=as_of)
        ],
        "detectors_now": [result.as_json() for result in metrics.evaluate_detectors(view)],
    }


# --------------------------------------------------------------------------- diagnosis and recovery commands


def diagnose_run(
    conn: sqlite3.Connection, *, run_id: str, mode: ModelMode, recordings_dir: Path, incident_id: str | None = None,
    client: diagnosis_store.ModelClient | None = None,
) -> diagnosis_store.StoredDiagnosis:
    run = storage.require_run(conn, run_id)
    configured = settings.load_settings()
    with _hosted_client(configured, mode) as hosted:
        return diagnosis_store.diagnose_incident(
            conn, run_id=run_id, incident_id=incident_id or latest_incident(conn, run_id), mode=mode,
            settings=configured, deployed=_deployed(conn, run_id, run.clock_ms), client=client,
            recordings_dir=recordings_dir, hosted=hosted,
        )


def run_diagnose(args: argparse.Namespace) -> int:
    mode = command_mode(args.mode)
    with _database(args) as conn:
        stored = diagnose_run(conn, run_id=args.run_id, mode=mode, recordings_dir=_recordings_dir(args),
                              incident_id=args.incident_id)
    _print(diagnosis_document(stored))
    return 0


def run_fixes(args: argparse.Namespace) -> int:
    _print([option.model_dump(mode="json") for option in fixes.available_fixes()])
    return 0


def validate_fix(conn: sqlite3.Connection, *, run_id: str, fix_id: str, diagnosis_id: str, operator: str) -> fixes.FixValidation:
    now_ms = next_operator_time(conn, run_id)
    return fixes.validate_fix(conn, run_id=run_id, deployed=_deployed(conn, run_id, now_ms), fix_id=fix_id,
                              diagnosis_id=diagnosis_id, operator_id=operator, now_ms=now_ms)


def validation_document(validation: fixes.FixValidation) -> dict[str, JsonValue]:
    return {
        "validation_id": validation.validation_id,
        "fix_id": validation.fix_id,
        "deployed_version": validation.deployed_version,
        "reproduced": validation.reproduced,
        "deployed_failures": list(validation.deployed_failures),
        "corrected_failures": list(validation.corrected_failures),
        "passed": validation.passed,
    }


def run_validate_fix(args: argparse.Namespace) -> int:
    with _database(args) as conn:
        validation = validate_fix(conn, run_id=args.run_id, fix_id=args.fix_id, diagnosis_id=args.diagnosis_id,
                                  operator=args.operator)
    _print(validation_document(validation))
    return 0


def build_plan(
    conn: sqlite3.Connection, *, run_id: str, fix_validation_id: str, stale_after_ms: int | None = None,
    ttl_ms: int | None = None,
) -> plans.PlanPreview:
    if stale_after_ms is None or ttl_ms is None:  # a command: the environment, read before the clock moves
        configured = settings.load_settings()
        stale_after_ms = configured.stale_after_seconds * 1000 if stale_after_ms is None else stale_after_ms
        ttl_ms = configured.plan_ttl_seconds * 1000 if ttl_ms is None else ttl_ms
    now_ms = next_operator_time(conn, run_id)
    return plans.build_reprocess_plan(
        conn, run_id=run_id, fix_validation_id=fix_validation_id, deployed=_deployed(conn, run_id, now_ms),
        now_ms=now_ms, ttl_ms=ttl_ms, stale_after_ms=stale_after_ms,
    )


def plan_document(preview: plans.PlanPreview) -> dict[str, JsonValue]:
    return {
        "plan_id": preview.plan.plan_id,
        "plan_hash": preview.plan.plan_hash,
        "operation": preview.plan.operation.value,
        "expires_at_ms": preview.plan.expires_at_ms,
        "target_count": preview.target_count,
        "target_value_paise": preview.target_value_paise,
        "targets": [target.model_dump(mode="json") for target in preview.targets],
        "exclusions_by_reason": {reason: list(orders) for reason, orders in preview.exclusions_by_reason.items()},
        "superseded_inbox_ids": {order: list(ids) for order, ids in preview.superseded_inbox_ids.items()},
        "dry_runs_passed": sum(run.passed for run in preview.dry_runs),
        "preconditions": list(preview.preconditions),
        "deployed_version": preview.deployed_version,
        "corrected_version": preview.corrected_version,
    }


def run_plan(args: argparse.Namespace) -> int:
    with _database(args) as conn:
        preview = build_plan(conn, run_id=args.run_id, fix_validation_id=args.fix_validation_id)
    _print(plan_document(preview))
    return 0


def approve_plan(
    conn: sqlite3.Connection, *, run_id: str, plan_id: str, plan_hash: str, operator: str, ttl_ms: int | None = None
) -> executor.StoredApproval:
    now_ms = next_operator_time(conn, run_id)
    return executor.approve_plan(
        conn, run_id=run_id, plan_id=plan_id, plan_hash=plan_hash, operator_id=operator, now_ms=now_ms,
        ttl_ms=settings.load_settings().approval_ttl_seconds * 1000 if ttl_ms is None else ttl_ms,
    )


def run_approve(args: argparse.Namespace) -> int:
    with _database(args) as conn:
        approval = approve_plan(conn, run_id=args.run_id, plan_id=args.plan_id, plan_hash=args.plan_hash,
                                operator=args.operator)
    _print(approval.model_dump(mode="json"))
    return 0


def execute_plan(
    conn: sqlite3.Connection, *, run_id: str, plan_id: str, stale_after_ms: int | None = None
) -> executor.ExecutionResult:
    now_ms = next_operator_time(conn, run_id)
    return executor.execute_plan(conn, run_id=run_id, plan_id=plan_id, deployed=_deployed(conn, run_id, now_ms),
                                 now_ms=now_ms, stale_after_ms=_freshness_threshold(stale_after_ms))


def run_execute(args: argparse.Namespace) -> int:
    with _database(args) as conn:
        result = execute_plan(conn, run_id=args.run_id, plan_id=args.plan_id)
    _print(result.model_dump(mode="json"))
    return 0


def escalate_diagnosis(
    conn: sqlite3.Connection, *, run_id: str, diagnosis_id: str, operator: str
) -> plans.EscalationRecord:
    stored = diagnosis_store.get_diagnosis(conn, run_id=run_id, diagnosis_id=diagnosis_id)
    if stored is None:
        raise ValueError(f"run {run_id!r} has no diagnosis {diagnosis_id!r}")
    output = stored.output
    if output is None:
        summary = "The diagnosis needs review: " + "; ".join(stored.reasons)
        checks = ()
        subjects: tuple[str, ...] = (stored.incident_id,)
    else:
        summary = f"{output.hypothesis.value}: {output.rationale}"
        checks = tuple(dict.fromkeys((output.next_check, *output.missing_checks)))
        subjects = (output.affected_method,) if output.affected_method not in ("none", "unknown") else (stored.incident_id,)
    now_ms = next_operator_time(conn, run_id)
    return plans.escalate(conn, run_id=run_id, incident_id=stored.incident_id, diagnosis_id=diagnosis_id,
                          operator_id=operator, summary=summary, evidence_needed=checks, subject_ids=subjects, now_ms=now_ms)


def run_escalate(args: argparse.Namespace) -> int:
    with _database(args) as conn:
        record = escalate_diagnosis(conn, run_id=args.run_id, diagnosis_id=args.diagnosis_id, operator=args.operator)
    _print(record.model_dump(mode="json"))
    return 0


def provider_records_path(data_dir: Path, run_id: str) -> Path:
    """Where seeding wrote the simulated provider's records of a run: what a read-only refresh reads."""
    return data_dir / PROVIDER_DIRECTORY / f"{run_id}.json"


def refresh_payments(conn: sqlite3.Connection, data_dir: Path, *, run_id: str, payment_ids: Sequence[str]) -> refresh.RefreshSummary:
    records = provider_records_path(data_dir, run_id)
    if not records.is_file():
        raise FileNotFoundError(f"no provider records for {run_id!r} at {records}; seed the run with this data directory")
    run = storage.require_run(conn, run_id)
    return refresh.refresh_provider_state(
        conn, run_id=run_id, payment_ids=payment_ids, adapter=SimulatedProviderAdapter.from_records(records),
        now_ms=run.clock_ms + OPERATOR_STEP_MS,
    )


@dataclass(frozen=True)
class DeliveryLogRead:
    """The provider's delivery log as read before a replay: what the read recorded, or why there was none."""

    summary: refresh.DeliveryLogSummary | None
    skipped_because: str | None


def read_delivery_log(conn: sqlite3.Connection, data_dir: Path, run_id: str, *, now_ms: int | None = None) -> DeliveryLogRead:
    """Read the provider's delivery log for every capture a replay would consider now (owner decision Q29 (b)).

    Those are the payments the provider shows captured with no stored payment.captured event; only the log tells a
    rejected event, which a resend recovers, from one the receiver answered 2xx and lost, which the provider will
    not resend. ``now_ms`` defaults to one operator step after the clock; the replay paths pass the clock itself when
    their re-check has just taken that step.
    """
    run = storage.require_run(conn, run_id)
    captures = plans.captures_without_stored_event(conn, run_id=run_id, as_of_ms=run.clock_ms)
    if not captures:
        return DeliveryLogRead(None, NO_CAPTURE_TO_CHECK)
    records = provider_records_path(data_dir, run_id)
    if not records.is_file():
        return DeliveryLogRead(None, NO_PROVIDER_RECORDS)
    log = delivery_log_path(records)
    if not log.is_file():  # seeded before the delivery log was written
        return DeliveryLogRead(None, NO_DELIVERY_LOG_RECORDS)
    summary = refresh.record_delivery_log(
        conn, run_id=run_id, payment_ids=captures, delivery_log=SimulatedDeliveryLog.from_file(log),
        now_ms=run.clock_ms + OPERATOR_STEP_MS if now_ms is None else now_ms,
    )
    return DeliveryLogRead(summary, None)


def delivery_log_line(read: DeliveryLogRead) -> str:
    summary = read.summary
    if summary is None:
        return f"delivery log not read: {read.skipped_because}"
    return (f"delivery log read at replay time {summary.now_ms} for {len(summary.payment_ids)} captured payments with "
            f"no stored payment.captured event; {len(summary.acknowledged_captures)} of them were answered 2xx and "
            "never stored, so the replay holds them for escalation")


def run_refresh(args: argparse.Namespace) -> int:
    with _database(args) as conn:
        summary = refresh_payments(conn, _data_dir(args), run_id=args.run_id, payment_ids=args.payment_ids)
    _print({"run_id": summary.run_id, "now_ms": summary.now_ms, "refreshed": list(summary.refreshed),
            "already_recorded": list(summary.already_recorded), "unknown_at_provider": list(summary.unknown_at_provider),
            "statuses": summary.statuses})
    return 0


def run_report(args: argparse.Namespace) -> int:
    with _database(args) as conn:
        path = report.write_report(conn, run_id=args.run_id, path=args.output)
    _print({"report": str(path)})
    return 0


def run_receipt(args: argparse.Namespace) -> int:
    with _database(args) as conn:
        json_path, markdown_path, receipt_hash = report.write_receipt(
            conn, run_id=args.run_id, action_id=args.action_id, directory=args.output_dir
        )
    # The export time is printed, never written into the files: regenerating a receipt gives the same bytes.
    _print({"receipt_hash": receipt_hash, "json": str(json_path), "markdown": str(markdown_path),
            "exported_wall_ms": storage.wall_clock_ms()})
    return 0


# --------------------------------------------------------------------------- webhook request


@dataclass(frozen=True)
class WebhookRequest:
    """One signed, provider-shaped delivery for an existing payment, ready to send to the API."""

    run_id: str
    integration_id: str
    payment_id: str
    order_id: str
    status: PaymentStatus
    amount_paise: int
    currency: str
    headers: tuple[tuple[str, str], ...]  # the event-ID and signature headers, in that order
    raw_body: bytes


def build_webhook_request(
    conn: sqlite3.Connection, *, run_id: str, integration_id: str, payment_id: str, status: PaymentStatus,
    event_id: str,
) -> WebhookRequest:
    """Sign one event for an existing payment; the database is only read.

    Order, amount and currency come from the payment's order, and the event time is the run's replay
    clock. The signature uses ``merchant.PROVIDER_SIGNING_REF``, as ``driver.deliver`` does, whatever
    secret the run's receiver verifies with now: a request the provider could not have sent would add
    an accepted delivery to a run whose receiver refuses the provider's, and change its evidence.
    """
    with storage.read_transaction(conn):
        run = storage.require_run(conn, run_id)
        attempt = _payment_attempt(conn, run_id=run_id, payment_id=payment_id)
        order = storage.require_order(conn, run_id=run_id, order_id=attempt.order_id)
        _integration(conn, run_id=run_id, integration_id=integration_id)  # refuses one the run does not have
    envelope = signing.build_payment_event(
        payment_id=attempt.payment_id, order_id=order.order_id, amount_paise=order.amount_paise, status=status,
        occurred_at_ms=run.clock_ms, payment_created_at_ms=attempt.created_at_ms, currency=order.currency,
        method=attempt.method,
        amount_refunded_paise=order.amount_paise if status is PaymentStatus.REFUNDED else 0,
    )
    headers, raw_body = signing.signed_delivery(envelope, event_id=event_id, secret_ref=merchant.PROVIDER_SIGNING_REF)
    return WebhookRequest(
        run_id=run_id, integration_id=integration_id, payment_id=attempt.payment_id, order_id=order.order_id,
        status=status, amount_paise=order.amount_paise, currency=order.currency, headers=tuple(headers.items()),
        raw_body=raw_body,
    )


def _payment_attempt(conn: sqlite3.Connection, *, run_id: str, payment_id: str) -> storage.PaymentAttemptRecord:
    found = storage.get_payment_attempts(conn, run_id=run_id, payment_ids=[payment_id], as_of_ms=None)
    if found:
        return found[0]
    examples = [item.payment_id for item in storage.list_run_payment_attempts(conn, run_id=run_id, as_of_ms=None)[:3]]
    raise ScopeError(f"payment {payment_id!r} is not a payment of run {run_id!r}; choose one of its payments "
                     f"({', '.join(examples) if examples else 'it has none yet'})")


def _integration(conn: sqlite3.Connection, *, run_id: str, integration_id: str) -> storage.IntegrationRecord:
    integration = storage.get_integration(conn, run_id=run_id, integration_id=integration_id)
    if integration is None:
        configured = [item.integration_id for item in storage.list_integrations(conn, run_id=run_id)]
        raise ScopeError(f"integration {integration_id!r} is not configured for run {run_id!r}; "
                         f"it has {', '.join(configured) if configured else 'none'}")
    return integration


def curl_command(*, url: str, headers: Sequence[tuple[str, str]], body_path: Path) -> str:
    """A ready-to-run curl command. ``--data-binary`` sends the file's bytes unchanged, so the signature holds."""
    header_flags = [word for name, value in headers for word in ("--header", f"{name}: {value}")]
    return shlex.join([
        "curl", "--silent", "--show-error", "--data-binary", f"@{body_path}",
        "--header", "Content-Type: application/json", *header_flags,
        "--write-out", r"\nHTTP %{http_code}\n", url,
    ])


def run_webhook_request(args: argparse.Namespace) -> int:
    with _database(args, migrate=False) as conn:  # documented as read-only: it never writes, not even a migration
        request = build_webhook_request(conn, run_id=args.run_id, integration_id=args.integration_id,
                                        payment_id=args.payment_id, status=PaymentStatus(args.status),
                                        event_id=args.event_id)
    body_path = args.output.resolve()
    body_path.write_bytes(request.raw_body)
    url = f"http://127.0.0.1:{args.port}/api/runs/{request.run_id}/webhooks/{request.integration_id}"
    print(f"# {request.status.value} event for payment {request.payment_id} of order {request.order_id}: "
          f"{request.amount_paise} paise {request.currency}; body {body_path} ({len(request.raw_body)} bytes)")
    for name, value in request.headers:
        print(f"{name}: {value}")
    print(curl_command(url=url, headers=request.headers, body_path=body_path))
    return 0


# --------------------------------------------------------------------------- demo


class _Narrator:
    """Prints numbered demo steps plainly, one line per fact."""

    def __init__(self, write: Callable[[str], None]) -> None:
        self._write = write
        self._step = 0

    def step(self, title: str, *lines: str) -> None:
        self._step += 1
        self._write(f"[{self._step}] {title}")
        for line in lines:
            self._write(f"    {line}")


def run_demo(args: argparse.Namespace) -> int:
    data_dir = _data_dir(args)
    mode = command_mode(args.model_mode)
    data_dir.mkdir(parents=True, exist_ok=True)
    return demo(scenario=args.scenario, seed=args.seed, mode=mode, data_dir=data_dir,
                recordings_dir=_recordings_dir(args), write=print)


def demo(
    *,
    scenario: str,
    seed: int,
    mode: ModelMode,
    data_dir: Path,
    recordings_dir: Path,
    write: Callable[[str], None],
    client: diagnosis_store.ModelClient | None = None,
) -> int:
    """Seed a fresh run and walk it from symptom to outcome, printing every step.

    After the diagnosis it does what an operator would do with the proposed operation; it never
    reads the scenario's hidden labels. Returns 0 when the flow completed.
    """
    narrator = _Narrator(write)
    write(f"== RayTrace demo: scenario {scenario}, seed {seed}, model mode {mode.value} "
          "(synthetic sandbox, no real money) ==")
    world = generate_world(scenarios.demo_scenario(scenario), seed)
    database = data_dir / DATABASE_FILE
    with closing(storage.connect(database)) as conn:
        summary = driver.seed_world(conn, world, oracle_dir=data_dir / ORACLE_DIRECTORY, model_mode=mode)
        run_id = summary.run_id
        narrator.step("seed", f"run {run_id}: {summary.counts['orders']} orders, {summary.counts['webhook_inbox']} "
                      f"inbox events; replay cutoff {summary.cutoff_ms}; incidents {list(summary.incident_ids)}")
        _demo_status(narrator, conn, run_id)
        stored = diagnose_run(conn, run_id=run_id, mode=mode, recordings_dir=recordings_dir, client=client)
        _demo_diagnosis(narrator, stored)
        proposed = stored.output.proposed_operation if stored.output is not None else None
        if stored.status is DiagnosisStatus.NEEDS_REVIEW or proposed is Operation.ESCALATE:
            _demo_late_authorisations(narrator, conn, run_id)
            _demo_escalate(narrator, conn, run_id, stored)
        elif proposed is Operation.REPROCESS_INBOX:
            _demo_reprocess(narrator, conn, data_dir, run_id, stored)
        elif proposed is Operation.REFRESH_PROVIDER_STATE:
            _demo_refresh(narrator, conn, data_dir, run_id)
            _demo_late_authorisations(narrator, conn, run_id)
            _demo_escalate(narrator, conn, run_id, stored)
        elif proposed is Operation.PREPARE_PROVIDER_REPLAY:
            # Re-check first: a capture whose webhooks were all lost looks failed until the provider is asked (Q26),
            # and only the provider's delivery log shows a capture it will not send again (Q29).
            _demo_refresh(narrator, conn, data_dir, run_id, with_delivery_log=True)
            _demo_replay(narrator, conn, run_id, stored)
        else:
            narrator.step("no action", "the diagnosis found no incident; nothing is changed")
        path = report.write_report(conn, run_id=run_id, path=data_dir / REPORTS_DIRECTORY / f"{run_id}.json")
        narrator.step("report", f"written to {path}")
    return 0


def _demo_status(narrator: _Narrator, conn: sqlite3.Connection, run_id: str) -> None:
    run = storage.require_run(conn, run_id)
    windows = metrics.DEFAULT_WINDOWS
    view = metrics.load_run_view(conn, run_id, run.clock_ms, stale_after_ms=windows.stale_after_ms)
    authorization = metrics.provider_measures(view, windows.current(run.clock_ms)).authorization
    grace_ms = metrics.DEFAULT_INCIDENT_DETECTORS.processing_grace_ms
    measures = metrics.merchant_measures(view, grace_ms=grace_ms)
    backlog = Counter(row.processing_status.value for row in view.inbox)
    failures = Counter(
        f"{item.worker_version} | {item.exception_category or 'no category'}"
        for item in view.worker_attempts if item.result.value == "failed"
    )
    narrator.step(
        "status",
        f"provider authorisation, current mature cohort, all methods: {authorization.as_json()['value']} "
        f"({authorization.numerator} of {authorization.denominator} attempts)",
        f"merchant processing of captured orders: {measures.completion.as_json()['value']} "
        f"({measures.completion.numerator} of {measures.completion.denominator} processed)",
        f"pending captured value (metric): {rupees(measures.pending_captured_value_paise)} over "
        f"{measures.pending_captured_orders} orders. Definition: orders with one captured payment known for at least "
        f"{grace_ms // 60_000} min (the grace period), that the worker could process, not yet fulfilled",
        f"inbox rows by status: {dict(sorted(backlog.items()))}",
        f"worker failures by version and category: {dict(failures.most_common(3)) or 'none'}",
        f"deployments: {[item.deploy_id for item in storage.list_deployments(conn, run_id=run_id, as_of_ms=run.clock_ms)]}",
    )


def _demo_diagnosis(narrator: _Narrator, stored: diagnosis_store.StoredDiagnosis) -> None:
    # The rationale, and some reasons, hold text from whatever answered on the model endpoint (a reason can quote 300
    # characters of an error reply), so a terminal must not act on it: each control or format character prints escaped
    # (Q43). Line breaks are escaped, not collapsed, so each fact keeps its one line and every other character prints
    # as stored.
    lines = [f"mode: {stored.mode_label}", f"status: {stored.status.value}; packet {stored.packet_hash[:16]}"]
    if stored.output is not None:
        output = stored.output
        lines += [
            f"hypothesis: {output.hypothesis.value}; faulting change: {output.faulting_change_id}; "
            f"affected method: {output.affected_method}",
            f"evidence level: {output.evidence_level.value} (a label, not a probability); "
            f"next check: {output.next_check.value}; proposed operation: {output.proposed_operation.value}",
            f"rationale: {report.visible(output.rationale)}",
        ]
    prefix = "needs review because" if stored.status is DiagnosisStatus.NEEDS_REVIEW else "rejected, then repaired"
    lines += [f"{prefix}: {report.visible(reason)}" for reason in stored.reasons]
    lines += [f"unsafe proposal recorded: {item.rule} {item.hypothesis.value} -> {item.proposed_operation.value}"
              for item in stored.unsafe_proposals]
    narrator.step("diagnose", *lines)


def _candidate_fixes(conn: sqlite3.Connection, run_id: str, faulting_change_id: str) -> list[fixes.FixOption]:
    """Allowlisted fixes for the files the named deployment changed; every fix when none is named."""
    options = list(fixes.available_fixes())
    deployments = {item.deploy_id: item for item in storage.list_deployments(conn, run_id=run_id, as_of_ms=2**62)}
    named = deployments.get(faulting_change_id)
    if named is None:
        return options
    touched = set(named.files_changed)
    matching = [option for option in options if touched & set(option.files)]
    return matching or options


def _demo_reprocess(
    narrator: _Narrator, conn: sqlite3.Connection, data_dir: Path, run_id: str, stored: diagnosis_store.StoredDiagnosis
) -> None:
    """Flow A: validate a fix, plan, refresh stale provider state and plan again, then approve and execute."""
    output = stored.output
    if output is None:
        raise ContractError(f"diagnosis {stored.diagnosis_id!r} has no validated output to act on")
    narrator.step("fixes", *(f"{option.fix_id}: {option.label}" for option in fixes.available_fixes()))
    passing = _demo_validate_fixes(narrator, conn, run_id, stored, output.faulting_change_id)
    if passing is None:
        narrator.step("stop", "no allowlisted corrected handler passed the fixture tests; nothing is reprocessed")
        return
    preview = build_plan(conn, run_id=run_id, fix_validation_id=passing.validation_id)
    _demo_plan(narrator, "plan", preview)
    stale_only = stale_only_orders(preview)
    if stale_only:
        _demo_refresh_orders(narrator, conn, data_dir, run_id, stale_only)
        draft = preview
        preview = build_plan(conn, run_id=run_id, fix_validation_id=passing.validation_id)
        newly_eligible = sorted(set(preview.plan.target_ids) - set(draft.plan.target_ids))
        _demo_plan(narrator, "plan again", preview,
                   f"newly eligible after the refresh: {len(newly_eligible)} of the {len(stale_only)} stale-only orders "
                   f"({', '.join(newly_eligible) or 'none'})",
                   f"replaces draft {draft.plan.plan_id}, which was never approved")
    _demo_late_authorised_in_plan(narrator, conn, run_id, preview)
    if not preview.targets:
        narrator.step("stop", "the plan has no eligible targets; nothing is reprocessed")
        return
    _demo_execute(narrator, conn, run_id, preview)


def _demo_validate_fixes(
    narrator: _Narrator, conn: sqlite3.Connection, run_id: str, stored: diagnosis_store.StoredDiagnosis,
    faulting_change_id: str,
) -> fixes.FixValidation | None:
    """Try the allowlisted fixes for the named change in order; the first that passes is used."""
    for option in _candidate_fixes(conn, run_id, faulting_change_id):
        validation = validate_fix(conn, run_id=run_id, fix_id=option.fix_id, diagnosis_id=stored.diagnosis_id,
                                  operator=DEMO_OPERATOR)
        narrator.step(
            f"validate-fix {option.fix_id}",
            f"reproduced on the deployed handler: {'yes' if validation.reproduced else 'no'} "
            f"(failed {list(validation.deployed_failures)})",
            f"corrected handler failed {list(validation.corrected_failures)}; passed: {validation.passed}",
        )
        if validation.passed:
            return validation
    return None


def _demo_plan(narrator: _Narrator, title: str, preview: plans.PlanPreview, *notes: str) -> None:
    excluded = {reason: len(orders) for reason, orders in preview.exclusions_by_reason.items()}
    targets = [target.id for target in preview.targets]
    narrator.step(
        f"{title} {preview.plan.plan_id} (hash {preview.plan.plan_hash[:16]})",
        f"{preview.target_count} target orders, value {rupees(preview.target_value_paise)}",
        "plan value definition: the sum of exactly these target rows, i.e. captured orders with an unprocessed event "
        "and no exclusion. Unlike the pending-captured metric it has no grace period and applies every exclusion, "
        "so the two numbers differ by design",
        f"targets: {', '.join(targets[:6])}{' ...' if len(targets) > 6 else ''}",
        *notes,
        f"excluded by reason: {excluded}",
        f"dry runs passed: {sum(run.passed for run in preview.dry_runs)} of {len(preview.dry_runs)}",
        f"rechecked inside the execution transaction: {len(preview.preconditions)} preconditions",
    )


def stale_only_orders(preview: plans.PlanPreview) -> tuple[str, ...]:
    """Orders the plan excluded for stale provider state and nothing else: a refresh may make them eligible."""
    stale = (ExclusionReason.STALE_SNAPSHOT.value,)
    return tuple(sorted(item.order_id for item in preview.excluded if tuple(item.reasons) == stale))


def _demo_refresh_orders(
    narrator: _Narrator, conn: sqlite3.Connection, data_dir: Path, run_id: str, order_ids: Sequence[str]
) -> None:
    run = storage.require_run(conn, run_id)
    wanted = set(order_ids)
    payment_ids = [
        attempt.payment_id for attempt in storage.list_run_payment_attempts(conn, run_id=run_id, as_of_ms=run.clock_ms)
        if attempt.order_id in wanted
    ]
    summary = refresh_payments(conn, data_dir, run_id=run_id, payment_ids=payment_ids)
    narrator.step(
        "refresh (read-only provider snapshot)",
        f"{len(order_ids)} orders were excluded only because their provider state was stale: {', '.join(order_ids)}",
        f"{len(summary.refreshed)} of their {len(payment_ids)} payments refreshed at replay time {summary.now_ms}; "
        f"statuses {dict(sorted(Counter(summary.statuses.values()).items()))}; "
        f"unknown at provider {len(summary.unknown_at_provider)}",
    )


def _demo_execute(narrator: _Narrator, conn: sqlite3.Connection, run_id: str, preview: plans.PlanPreview) -> None:
    approval = approve_plan(conn, run_id=run_id, plan_id=preview.plan.plan_id, plan_hash=preview.plan.plan_hash,
                            operator=DEMO_OPERATOR)
    narrator.step("approve", f"{approval.operator_id} approved plan {preview.plan.plan_id} with hash "
                  f"{approval.plan_hash[:16]} (expires at replay time {approval.expires_at_ms})")
    first = execute_plan(conn, run_id=run_id, plan_id=preview.plan.plan_id)
    fulfilled_after_first = len(storage.list_fulfilments(conn, run_id=run_id))
    narrator.step("execute", f"action {first.action_id}: {first.status.value}; fulfilled {len(first.fulfilled_order_ids)} "
                  "orders exactly once")
    again = execute_plan(conn, run_id=run_id, plan_id=preview.plan.plan_id)
    narrator.step("execute again", f"replayed: {again.replayed}; same action {again.action_id}; fulfilments still "
                  f"{len(storage.list_fulfilments(conn, run_id=run_id))} (were {fulfilled_after_first}); no new effects")
    narrator.step("outcome",
                  f"restored processing value: {rupees(first.restored_processing_value_paise)} (money already captured "
                  "before the intervention, not new revenue)",
                  f"new capture: {rupees(first.additional_captured_paise)}")


def _late_authorised(
    conn: sqlite3.Connection, run_id: str, *, stale_after_ms: int | None = None
) -> tuple[plans.LateAuthorisedOrder, ...]:
    run = storage.require_run(conn, run_id)
    return plans.late_authorised_orders(conn, run_id=run_id, now_ms=run.clock_ms,
                                        stale_after_ms=_freshness_threshold(stale_after_ms))


def _demo_late_authorised_in_plan(
    narrator: _Narrator, conn: sqlite3.Connection, run_id: str, preview: plans.PlanPreview
) -> None:
    """Flow A: the unfulfilled late-authorised orders the plan fulfils, and any kept out for another reason."""
    late = [order for order in _late_authorised(conn, run_id) if not order.fulfilled]
    if not late:
        narrator.step("late authorisation", "no unfulfilled order has a payment that failed and then authorised")
        return
    targets = set(preview.plan.target_ids)
    planned = [order for order in late if order.order_id in targets]
    excluded = {item.order_id: item.reasons for item in preview.excluded}
    lines = [
        "each of these payments failed first and was captured later; the capture is real money already collected, "
        "so fulfilling the order restores processing and charges nobody again (new capture stays 0)"
    ] if planned else []
    lines += [f"{order.order_id}: late-authorised payments {list(order.payment_ids)}; in the plan" for order in planned]
    for order in late:
        if order.order_id in targets:
            continue
        reasons = excluded.get(order.order_id) or order.exclusions
        why = (f"excluded for {', '.join(reasons)}" if reasons
               else "not a candidate: no unprocessed captured event in the inbox")
        lines.append(f"{order.order_id}: late-authorised payments {list(order.payment_ids)}; {why}")
    narrator.step(f"late authorisation: {len(planned)} late-authorised orders are in the plan", *lines)


@dataclass(frozen=True)
class LateAuthorisationFacts:
    """What the run's late authorisations mean, counted from the workbench's own view (read-only)."""

    payments: int  # payments that failed first and authorised later
    captured: int  # of those, captured now
    orders: int
    fulfilled_orders: int
    would_collect_twice: int  # captured late payments whose order has one capture: asking again would double it
    collected_twice: tuple[str, ...]  # orders that already hold more than one capture
    unfulfilled: tuple[plans.LateAuthorisedOrder, ...]  # each with its order's exclusions; none means it may be planned

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "payments": self.payments, "captured": self.captured, "orders": self.orders,
            "fulfilled_orders": self.fulfilled_orders, "would_collect_twice": self.would_collect_twice,
            "collected_twice": list(self.collected_twice),
            "unfulfilled": [order.model_dump(mode="json") for order in self.unfulfilled],
        }


def late_authorisation_facts(
    conn: sqlite3.Connection, run_id: str, *, stale_after_ms: int | None = None
) -> LateAuthorisationFacts:
    orders = _late_authorised(conn, run_id, stale_after_ms=stale_after_ms)
    run = storage.require_run(conn, run_id)
    view = metrics.load_run_view(conn, run_id, run.clock_ms, stale_after_ms=_freshness_threshold(stale_after_ms))
    payment_ids = sorted({payment_id for order in orders for payment_id in order.payment_ids})
    captured = {payment_id for payment_id in payment_ids if view.payments[payment_id].status is PaymentStatus.CAPTURED}
    twice = ExclusionReason.MULTIPLE_CAPTURES.value
    unfulfilled = tuple(order for order in orders if not order.fulfilled)
    return LateAuthorisationFacts(
        payments=len(payment_ids),
        captured=len(captured),
        orders=len(orders),
        fulfilled_orders=len(orders) - len(unfulfilled),
        # An order that already has two captures was collected twice in fact, not hypothetically.
        would_collect_twice=sum(payment_id in captured for order in orders if twice not in order.exclusions
                                for payment_id in order.payment_ids),
        collected_twice=tuple(order.order_id for order in orders if twice in order.exclusions),
        unfulfilled=unfulfilled,
    )


def _demo_late_authorisations(narrator: _Narrator, conn: sqlite3.Connection, run_id: str) -> None:
    """Flows B and C: what the late authorisations in this run mean, as counted facts."""
    facts = late_authorisation_facts(conn, run_id)
    if not facts.payments:
        narrator.step("late authorisations", "no payment failed and then authorised later")
        return
    lines = [f"{facts.payments} payments first failed and later authorised ({facts.captured} now captured); "
             f"{facts.fulfilled_orders} of their {facts.orders} orders were already fulfilled"]
    if facts.would_collect_twice:
        lines.append(f"a 'payment failed' recovery (asking the customer to pay again) would have collected twice "
                     f"for {facts.would_collect_twice} of the captured payments")
    if facts.collected_twice:
        lines.append(f"{len(facts.collected_twice)} orders already have more than one capture; a person must refund "
                     f"the extra one: {', '.join(facts.collected_twice[:8])}")
    for order in facts.unfulfilled[:8]:
        status = (f"its order is excluded for {', '.join(order.exclusions)}" if order.exclusions
                  else "nothing on its order excludes it from a reprocessing plan")
        lines.append(f"not fulfilled yet: {order.order_id}, payments {list(order.payment_ids)}; {status}")
    if len(facts.unfulfilled) > 8:
        lines.append(f"... and {len(facts.unfulfilled) - 8} more unfulfilled orders")
    narrator.step("late authorisations", *lines)


def refresh_candidates(conn: sqlite3.Connection, run_id: str, *, limit: int = 50) -> tuple[str, ...]:
    """Payments a read-only refresh can settle: open or late-authorised ones, then recently failed ones.

    The first part is unchanged: sorted by payment ID, at most ``limit``. Then come the payments projected failed whose
    first failure lies within the packet's look-back (``tools.LOOKBACK_MS``) before the replay clock, newest first,
    at most ``RECENT_FAILURE_LIMIT``. A late authorisation whose webhooks were all lost still projects as a failure;
    only the provider can tell (owner decision Q26).
    """
    run = storage.require_run(conn, run_id)
    view = metrics.load_run_view(conn, run_id, run.clock_ms, stale_after_ms=metrics.DEFAULT_WINDOWS.stale_after_ms)
    open_statuses = (PaymentStatus.UNKNOWN, PaymentStatus.CREATED, PaymentStatus.AUTHORIZED)
    open_or_late = sorted(
        payment_id for payment_id, payment in view.payments.items()
        if payment.late_authorization or payment.status in open_statuses
    )[:limit]
    failed_at = {
        payment_id: min(item.occurred_at_ms for item in payment.history if item.status is PaymentStatus.FAILED)
        for payment_id, payment in view.payments.items() if payment.status is PaymentStatus.FAILED
    }
    window_start_ms = run.clock_ms - tools.LOOKBACK_MS
    recent = sorted((payment_id for payment_id, at_ms in failed_at.items() if at_ms >= window_start_ms),
                    key=lambda payment_id: (-failed_at[payment_id], payment_id))
    return (*open_or_late, *recent[:RECENT_FAILURE_LIMIT])


def _demo_refresh(
    narrator: _Narrator, conn: sqlite3.Connection, data_dir: Path, run_id: str, *, with_delivery_log: bool = False
) -> None:
    candidates = refresh_candidates(conn, run_id)
    if not candidates:
        title, lines = "refresh", [NOTHING_TO_REFRESH]
    else:
        summary = refresh_payments(conn, data_dir, run_id=run_id, payment_ids=candidates)
        title, lines = "refresh (read-only provider snapshot)", [
            f"{len(summary.refreshed)} payments refreshed at replay time {summary.now_ms}; "
            f"statuses {dict(sorted(Counter(summary.statuses.values()).items()))}; "
            f"unknown at provider {len(summary.unknown_at_provider)}"]
    if with_delivery_log:  # in the refresh's own operator step, when it took one
        clock_ms = storage.require_run(conn, run_id).clock_ms
        lines.append(delivery_log_line(read_delivery_log(conn, data_dir, run_id,
                                                         now_ms=clock_ms if candidates else None)))
    narrator.step(title, *lines)


def _demo_escalate(narrator: _Narrator, conn: sqlite3.Connection, run_id: str, stored: diagnosis_store.StoredDiagnosis) -> None:
    record = escalate_diagnosis(conn, run_id=run_id, diagnosis_id=stored.diagnosis_id, operator=DEMO_OPERATOR)
    failed = sum(row.processing_status.value == "failed" for row in storage.list_inbox_rows(conn, run_id=run_id))
    narrator.step("escalate", f"exception recorded (action {record.action_id}) for {list(record.subject_ids)}",
                  f"evidence needed: {[check.value for check in record.evidence_needed]}",
                  f"why not reprocess: {_why_no_reprocessing(stored)}",
                  f"no reprocessing: failed inbox rows {failed}, fulfilments unchanged")


def _why_no_reprocessing(stored: diagnosis_store.StoredDiagnosis) -> str:
    output = stored.output
    if output is None:
        return "the diagnosis needs review, so it proposes no operation and the demo escalates"
    allowed = sorted(operation.value for operation in diagnosis.OPERATION_POLICY[output.hypothesis])
    listed = allowed[0] if len(allowed) == 1 else f"{', '.join(allowed[:-1])} and {allowed[-1]}"
    return (f"the diagnosis is {output.hypothesis.value}, and the operation policy permits only {listed} for it; "
            "reprocessing is not among them")


def _demo_replay(narrator: _Narrator, conn: sqlite3.Connection, run_id: str, stored: diagnosis_store.StoredDiagnosis) -> None:
    instructions = plans.prepare_provider_replay(
        conn, run_id=run_id, incident_id=stored.incident_id, diagnosis_id=stored.diagnosis_id,
        now_ms=next_operator_time(conn, run_id), ttl_ms=settings.load_settings().plan_ttl_seconds * 1000,
        stale_after_ms=settings.load_settings().stale_after_seconds * 1000,
    )
    narrator.step("prepare provider replay (instructions only, no provider call)", *instructions.instructions[:10])


if __name__ == "__main__":
    raise SystemExit(main())
