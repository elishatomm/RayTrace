"""Diagnose an incident end to end, and persist and read diagnoses (the ``diagnoses`` table).

Four modes, never confused with each other:

* ``local``: fresh inference against local Ollama;
* ``recorded``: a stored local output replayed later, only for a byte-identical packet. It names
  what it replays (``recorded_from``) with the original model, digest and time, and is always
  labelled recorded, never presented as live;
* ``rules``: the deterministic rules baseline, with no model fields at all. Since Q28 it is
  rules-v3 (``selective.rules_v3``): rules-v2's hypothesis and operation with attribution-v1's
  faulting deployment. A stored diagnosis keeps the rules version it was made with;
* ``hosted`` (owner answer to Q46): fresh inference by a hosted OpenAI-compatible API, with the same
  prompt, validator and policies as local. A provider reports no model digest, so it records the
  provider host instead, and it is labelled HOSTED and outside the M4 evaluation, which measured
  qwen3:8b on the development laptop. Recorded mode never replays it.

The table has no columns for the validator version, the policy setting, the blocked (unsafe)
proposals, a recorded replay's source or a hosted diagnosis's provider host. They travel in a
versioned JSON envelope in the ``output`` column, next to the validated ``DiagnosisOutput``. Only a
hosted row's envelope has ``provider_host`` and ``token_usage`` (each call's token counts, as the
provider reported them, for tracking a free tier), so every other row's envelope is as it always was.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from types import ModuleType
from typing import Protocol

from pydantic import JsonValue, model_validator

from recovery_workbench import diagnosis, packet, storage
from recovery_workbench.contracts import (
    ContractError,
    DiagnosisOutput,
    DiagnosisStatus,
    EvidencePacket,
    ModelMode,
    ScopeError,
    StrictModel,
)
from recovery_workbench.diagnosis import VALIDATOR_VERSION, BlockedProposal, ChatReply, ChatRequest, DiagnosisRun
from recovery_workbench.settings import MODEL_NAME, Settings
from recovery_workbench.simulator import merchant

ENVELOPE_VERSION = 2
RECORDING_VERSION = 1


class CallTokens(StrictModel):
    """One hosted call's token counts from the provider's ``usage``; None where it reported none or never answered."""

    call_index: int  # 0 is the first call, 1 the repair
    prompt_tokens: int | None
    completion_tokens: int | None


class StoredDiagnosis(StrictModel):
    run_id: str
    diagnosis_id: str
    incident_id: str
    mode: ModelMode
    model_id: str | None
    model_digest: str | None  # None for a local run that never reached the model, and for every hosted run
    thinking: bool | None
    prompt_version: str | None
    validator_version: str
    policies_enforced: bool
    rules_version: str | None
    recorded_from: str | None  # what a recorded diagnosis replays: an earlier diagnosis or a recording file
    recorded_original_wall_ms: int | None  # when the replayed output was originally produced
    provider_host: str | None = None  # the host a hosted diagnosis asked; hosted diagnoses only, which have no digest
    token_usage: tuple[CallTokens, ...] = ()  # hosted diagnoses only: one entry per call made
    packet_hash: str
    status: DiagnosisStatus
    output: DiagnosisOutput | None
    reasons: tuple[str, ...]
    unsafe_proposals: tuple[BlockedProposal, ...]
    elapsed_ms: int | None
    created_wall_ms: int

    @model_validator(mode="after")
    def _mode_fields_are_consistent(self) -> StoredDiagnosis:
        mode_problem = self._rules_mode_problem() if self.mode is ModelMode.RULES else self._model_mode_problem()
        problem = mode_problem or self._status_problem()
        if problem is not None:
            raise ValueError(problem)
        return self

    def _rules_mode_problem(self) -> str | None:
        model_fields = (self.model_id, self.thinking, self.prompt_version, self.model_digest, self.recorded_from,
                        self.provider_host)
        if any(field is not None for field in model_fields) or self.token_usage:
            return "a rules diagnosis has no model, digest, thinking, prompt, recorded source, provider host or tokens"
        if not self.rules_version:
            return "a rules diagnosis must name its rules version"
        return None

    def _model_mode_problem(self) -> str | None:
        """Local, recorded and hosted diagnoses: the model fields, a replay source exactly for recorded ones and a
        provider host exactly for hosted ones."""
        if any(field is None for field in (self.model_id, self.thinking, self.prompt_version)):
            return f"a {self.mode.value} diagnosis must record model, thinking and prompt version"
        if self.mode is ModelMode.HOSTED:
            return self._hosted_problem()
        if self.provider_host is not None or self.token_usage:
            return "provider_host and token_usage belong to hosted diagnoses only"
        if self.model_digest is None and self.status is DiagnosisStatus.VALIDATED:
            return "a validated model diagnosis must record the model digest"
        recorded = (self.recorded_from, self.recorded_original_wall_ms)
        if self.mode is ModelMode.RECORDED and any(field is None for field in recorded):
            return "a recorded diagnosis must name recorded_from and when the output was first produced"
        if self.mode is ModelMode.LOCAL and any(field is not None for field in recorded):
            return "recorded_from belongs to recorded diagnoses only"
        return None

    def _hosted_problem(self) -> str | None:
        if not self.provider_host:
            return "a hosted diagnosis must name the provider host it came from"
        if self.model_digest is not None:
            return "a hosted diagnosis has no model digest: a hosted provider reports none"
        if self.thinking:
            return "a hosted diagnosis records thinking off: Ollama's think switch does not apply to a hosted model"
        if self.recorded_from is not None or self.recorded_original_wall_ms is not None:
            return "recorded_from belongs to recorded diagnoses only"
        return None

    def _status_problem(self) -> str | None:
        if (self.status is DiagnosisStatus.VALIDATED) != (self.output is not None):
            return "an output is stored exactly when the diagnosis is validated"
        if self.status is DiagnosisStatus.NEEDS_REVIEW and not self.reasons:
            return "a needs_review diagnosis must say why"
        return None

    @property
    def mode_label(self) -> str:
        """How every display must label this diagnosis."""
        if self.mode is ModelMode.RECORDED:
            return f"RECORDED replay of {self.model_id} output from {self.recorded_from} (not live inference)"
        if self.mode is ModelMode.RULES:
            return f"rules baseline {self.rules_version} (no model)"
        if self.mode is ModelMode.HOSTED:
            return f"HOSTED: {self.model_id} via {self.provider_host}, not part of the M4 evaluation"
        return f"live local model {self.model_id}"


def new_diagnosis_id() -> str:
    return f"diag_{uuid.uuid4().hex[:16]}"


def record_model_diagnosis(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    incident_id: str,
    run: DiagnosisRun,
    model_id: str,
    model_digest: str | None,
    mode: ModelMode = ModelMode.LOCAL,
    recorded_from: str | None = None,
    recorded_original_wall_ms: int | None = None,
    provider_host: str | None = None,
    token_usage: tuple[CallTokens, ...] = (),
    diagnosis_id: str | None = None,
) -> StoredDiagnosis:
    """Store a model diagnosis, fresh (``local``, ``hosted``) or replayed (``recorded``), validated or needs_review."""
    if mode is ModelMode.RULES:
        raise ContractError("use record_rules_diagnosis for the rules baseline; it has no model fields")
    stored = StoredDiagnosis(
        run_id=run_id,
        diagnosis_id=diagnosis_id or new_diagnosis_id(),
        incident_id=incident_id,
        mode=mode,
        model_id=model_id,
        model_digest=model_digest,
        thinking=run.think,
        prompt_version=run.prompt_version,
        validator_version=run.validator_version,
        policies_enforced=run.policies_enforced,
        rules_version=None,
        recorded_from=recorded_from,
        recorded_original_wall_ms=recorded_original_wall_ms,
        provider_host=provider_host,
        token_usage=token_usage,
        packet_hash=run.packet_hash,
        status=run.status,
        output=run.output,
        reasons=run.reasons,
        unsafe_proposals=run.unsafe_proposals,
        elapsed_ms=run.elapsed_ms,
        created_wall_ms=storage.wall_clock_ms(),
    )
    _insert(conn, stored)
    return stored


def record_rules_diagnosis(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    incident_id: str,
    packet_hash: str,
    output: DiagnosisOutput,
    rules_version: str,
    elapsed_ms: int | None = None,
    diagnosis_id: str | None = None,
) -> StoredDiagnosis:
    """Store a rules-baseline diagnosis; the caller validates it against the packet first."""
    stored = StoredDiagnosis(
        run_id=run_id,
        diagnosis_id=diagnosis_id or new_diagnosis_id(),
        incident_id=incident_id,
        mode=ModelMode.RULES,
        model_id=None,
        model_digest=None,
        thinking=None,
        prompt_version=None,
        validator_version=VALIDATOR_VERSION,
        policies_enforced=True,
        rules_version=rules_version,
        recorded_from=None,
        recorded_original_wall_ms=None,
        packet_hash=packet_hash,
        status=DiagnosisStatus.VALIDATED,
        output=output,
        reasons=(),
        unsafe_proposals=(),
        elapsed_ms=elapsed_ms,
        created_wall_ms=storage.wall_clock_ms(),
    )
    _insert(conn, stored)
    return stored


def _envelope(stored: StoredDiagnosis) -> str:
    envelope: dict[str, JsonValue] = {
        "envelope_version": ENVELOPE_VERSION,
        "diagnosis": None if stored.output is None else stored.output.model_dump(mode="json"),
        "validator_version": stored.validator_version,
        "policies_enforced": stored.policies_enforced,
        "rules_version": stored.rules_version,
        "recorded_from": stored.recorded_from,
        "recorded_original_wall_ms": stored.recorded_original_wall_ms,
        "reasons": list(stored.reasons),
        "unsafe_proposals": [proposal.model_dump(mode="json") for proposal in stored.unsafe_proposals],
    }
    if stored.mode is ModelMode.HOSTED:  # hosted rows only, so every other row's envelope stays byte for byte as it was
        envelope["provider_host"] = stored.provider_host
        envelope["token_usage"] = [item.model_dump(mode="json") for item in stored.token_usage]
    return json.dumps(envelope, sort_keys=True)


def _insert(conn: sqlite3.Connection, stored: StoredDiagnosis) -> None:
    with storage.write_transaction(conn):
        storage.require_run(conn, stored.run_id)
        incident = conn.execute(
            "SELECT 1 FROM incidents WHERE run_id = ? AND incident_id = ?", (stored.run_id, stored.incident_id)
        ).fetchone()
        if incident is None:
            raise ScopeError(
                f"incident {stored.incident_id!r} is not part of run {stored.run_id!r}; a diagnosis needs its incident"
            )
        conn.execute(
            "INSERT INTO diagnoses (run_id, diagnosis_id, incident_id, mode, model_id, model_digest, thinking,"
            " prompt_version, packet_hash, status, output, error, elapsed_ms, created_wall_ms)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                stored.run_id, stored.diagnosis_id, stored.incident_id, stored.mode.value, stored.model_id,
                stored.model_digest, None if stored.thinking is None else int(stored.thinking), stored.prompt_version,
                stored.packet_hash, stored.status.value, _envelope(stored),
                "; ".join(stored.reasons) if stored.reasons else None, stored.elapsed_ms, stored.created_wall_ms,
            ),
        )


def _from_row(row: sqlite3.Row) -> StoredDiagnosis:
    envelope = json.loads(str(row["output"]))
    if envelope.get("envelope_version") != ENVELOPE_VERSION:
        raise ContractError(
            f"diagnosis {row['diagnosis_id']!r} uses envelope version {envelope.get('envelope_version')!r}; "
            f"this code reads version {ENVELOPE_VERSION}"
        )
    return StoredDiagnosis(
        run_id=row["run_id"],
        diagnosis_id=row["diagnosis_id"],
        incident_id=row["incident_id"],
        mode=ModelMode(row["mode"]),
        model_id=row["model_id"],
        model_digest=row["model_digest"],
        thinking=None if row["thinking"] is None else bool(row["thinking"]),
        prompt_version=row["prompt_version"],
        validator_version=envelope["validator_version"],
        policies_enforced=envelope["policies_enforced"],
        rules_version=envelope["rules_version"],
        recorded_from=envelope["recorded_from"],
        recorded_original_wall_ms=envelope["recorded_original_wall_ms"],
        provider_host=envelope.get("provider_host"),
        token_usage=tuple(CallTokens.model_validate(item) for item in envelope.get("token_usage", [])),
        packet_hash=row["packet_hash"],
        status=DiagnosisStatus(row["status"]),
        output=None if envelope["diagnosis"] is None else DiagnosisOutput.model_validate(envelope["diagnosis"]),
        reasons=tuple(envelope["reasons"]),
        unsafe_proposals=tuple(BlockedProposal.model_validate(item) for item in envelope["unsafe_proposals"]),
        elapsed_ms=row["elapsed_ms"],
        created_wall_ms=row["created_wall_ms"],
    )


def get_diagnosis(conn: sqlite3.Connection, *, run_id: str, diagnosis_id: str) -> StoredDiagnosis | None:
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        row = conn.execute(
            "SELECT * FROM diagnoses WHERE run_id = ? AND diagnosis_id = ?", (run_id, diagnosis_id)
        ).fetchone()
    return None if row is None else _from_row(row)


def list_diagnoses(
    conn: sqlite3.Connection, *, run_id: str, incident_id: str | None = None
) -> tuple[StoredDiagnosis, ...]:
    with storage.read_transaction(conn):
        storage.require_run(conn, run_id)
        rows = conn.execute(
            "SELECT * FROM diagnoses WHERE run_id = ? AND (? IS NULL OR incident_id = ?)"
            " ORDER BY created_wall_ms, diagnosis_id",
            (run_id, incident_id, incident_id),
        ).fetchall()
    return tuple(_from_row(row) for row in rows)


# --------------------------------------------------------------------------- recordings


class Recording(StrictModel):
    """A validated local output saved for replay, keyed by the exact packet it answered."""

    recording_version: int = RECORDING_VERSION
    packet_hash: str
    run_id: str
    incident_id: str
    source_diagnosis_id: str
    model_id: str
    model_digest: str
    thinking: bool
    prompt_version: str
    validator_version: str
    output: DiagnosisOutput
    unsafe_proposals: tuple[BlockedProposal, ...]
    elapsed_ms: int | None
    recorded_wall_ms: int

    @model_validator(mode="after")
    def _model_metadata_is_well_formed(self) -> Recording:
        # A recording file is read back from disk, and a replay stores its model and digest as the diagnosis's own:
        # they reach the report and the receipt, so they must be what the live client accepts from Ollama.
        if not MODEL_NAME.fullmatch(self.model_id):
            raise ValueError(f"model_id {self.model_id[:80]!r} is not an Ollama model name such as qwen3:8b")
        if not diagnosis.MODEL_DIGEST.fullmatch(self.model_digest):
            raise ValueError(f"model_digest {self.model_digest[:80]!r} is not a SHA-256 digest as Ollama reports one")
        return self


def _recording_from(stored: StoredDiagnosis) -> Recording:
    if stored.mode is not ModelMode.LOCAL or stored.output is None or stored.model_digest is None:
        raise ContractError(f"diagnosis {stored.diagnosis_id!r} is not a validated live local output; it cannot be recorded")
    return Recording(
        packet_hash=stored.packet_hash, run_id=stored.run_id, incident_id=stored.incident_id,
        source_diagnosis_id=stored.diagnosis_id, model_id=str(stored.model_id), model_digest=stored.model_digest,
        thinking=bool(stored.thinking), prompt_version=str(stored.prompt_version),
        validator_version=stored.validator_version, output=stored.output, unsafe_proposals=stored.unsafe_proposals,
        elapsed_ms=stored.elapsed_ms, recorded_wall_ms=stored.created_wall_ms,
    )


def write_recording(directory: Path, stored: StoredDiagnosis) -> Path:
    """Save a validated local output as ``<packet hash>.json`` so a fresh run with the same packet can replay it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stored.packet_hash}.json"
    path.write_text(_recording_from(stored).model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def find_recording(directory: Path, packet_hash: str) -> Recording | None:
    path = directory / f"{packet_hash}.json"
    if not path.is_file():
        return None
    recording = Recording.model_validate_json(path.read_text(encoding="utf-8"))
    if recording.packet_hash != packet_hash:
        raise ContractError(f"{path} claims packet {recording.packet_hash}, not {packet_hash}; the file was renamed or edited")
    return recording


# --------------------------------------------------------------------------- end-to-end diagnosis


class ModelClient(Protocol):
    """A chat client that also names its model and reports the model's digest (OllamaClient does)."""

    model: str

    def chat(self, request: ChatRequest, *, timeout_s: float) -> ChatReply: ...

    def model_digest(self) -> str: ...


class HostedModelClient(Protocol):
    """A chat client for a hosted model that names its model and the provider host it calls (hosted_model.HostedClient).

    It has no ``model_digest``: no OpenAI-compatible provider reports one, and a made-up digest would be false
    provenance, so a hosted diagnosis records the provider host instead. ``diagnosis.diagnose`` needs only ``chat``.
    """

    model: str
    provider_host: str

    def chat(self, request: ChatRequest, *, timeout_s: float) -> ChatReply: ...


def rules_baseline() -> ModuleType:
    """The product's rules baseline, rules-v3 (``selective.rules_v3``); refuses clearly when it is missing.

    Phase 11b's decision 2 adopted it over rules-v2 (Q28). rules-v2 stays for reproducing M4: the evaluation
    calls ``rules.diagnose`` and records ``rules.RULES_VERSION`` itself and never comes through here.
    """
    try:
        return importlib.import_module("recovery_workbench.selective")
    except ModuleNotFoundError as missing:
        if missing.name != "recovery_workbench.selective":
            raise
        raise ContractError(
            "the rules baseline rules-v3 (recovery_workbench/selective.py) is not available in this checkout; "
            "use model mode local or recorded"
        ) from missing


def diagnose_incident(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    incident_id: str,
    mode: ModelMode,
    settings: Settings,
    deployed: merchant.DeployedMerchant,
    client: ModelClient | None = None,
    cutoff_ms: int | None = None,
    recordings_dir: Path | None = None,
    hosted: HostedModelClient | None = None,
) -> StoredDiagnosis:
    """Build the incident's packet, diagnose it in ``mode`` and store the result.

    The packet (and its evidence rows) is committed before any model call, and no transaction is
    open while the model runs. A model failure is stored as needs_review with its reasons; the
    deterministic evidence stays intact. The cutoff defaults to the run's replay clock. Hosted mode
    needs ``hosted``: its client counts every call against its process's daily budget, so the caller
    that keeps the budget builds it.
    """
    if conn.in_transaction:
        raise ContractError("diagnose_incident must start outside any transaction; it never holds one during a model call")
    if mode is ModelMode.HOSTED and hosted is None:
        raise ContractError("hosted mode needs a hosted model client; the API and the CLI build one from the hosted "
                            "settings")
    with storage.read_transaction(conn):
        run = storage.require_run(conn, run_id)
    built = packet.build_packet(
        conn, run_id=run_id, incident_id=incident_id, cutoff_ms=run.clock_ms if cutoff_ms is None else cutoff_ms,
        deployed=deployed,
    )
    if mode is ModelMode.RULES:
        return _diagnose_with_rules(conn, run_id=run_id, incident_id=incident_id, built=built)
    if mode is ModelMode.RECORDED:
        return _replay_recorded(conn, run_id=run_id, incident_id=incident_id, built=built, recordings_dir=recordings_dir)
    if hosted is not None and mode is ModelMode.HOSTED:
        return _diagnose_with_hosted(conn, run_id=run_id, incident_id=incident_id, built=built, settings=settings,
                                     client=hosted)
    return _diagnose_with_model(
        conn, run_id=run_id, incident_id=incident_id, built=built, settings=settings,
        client=client or diagnosis.OllamaClient(base_url=settings.ollama_url, model=settings.model),
        recordings_dir=recordings_dir,
    )


def _diagnose_with_rules(conn: sqlite3.Connection, *, run_id: str, incident_id: str, built: EvidencePacket) -> StoredDiagnosis:
    baseline = rules_baseline()
    started = time.monotonic()
    produced = baseline.rules_v3(built)
    elapsed_ms = round((time.monotonic() - started) * 1000)
    output = produced if isinstance(produced, DiagnosisOutput) else DiagnosisOutput.model_validate(produced)
    try:
        diagnosis.validate_diagnosis(output.model_dump(mode="json"), built)
    except diagnosis.DiagnosisContractError as invalid:
        raise ContractError(f"the rules baseline broke the packet contract or a policy: {invalid}") from invalid
    return record_rules_diagnosis(
        conn, run_id=run_id, incident_id=incident_id, packet_hash=built.packet_hash(), output=output,
        rules_version=str(baseline.RULES_V3_VERSION), elapsed_ms=elapsed_ms,
    )


def _diagnose_with_model(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    incident_id: str,
    built: EvidencePacket,
    settings: Settings,
    client: ModelClient,
    recordings_dir: Path | None,
) -> StoredDiagnosis:
    try:
        digest: str | None = client.model_digest()
    except diagnosis.ModelCallError as unavailable:
        # Recorded as needs_review rather than raised: the evidence is built and the operator must see why.
        run = DiagnosisRun(
            status=DiagnosisStatus.NEEDS_REVIEW, output=None, reasons=(f"the model is unavailable: {unavailable}",),
            calls=(), repairs=0, elapsed_ms=0, think=settings.model_think, deadline_s=float(settings.run_timeout_seconds),
            prompt_version=diagnosis.PROMPT_VERSION, packet_hash=built.packet_hash(),
        )
        return record_model_diagnosis(conn, run_id=run_id, incident_id=incident_id, run=run, model_id=client.model,
                                      model_digest=None)
    run = diagnosis.diagnose(
        built, client, think=settings.model_think, deadline_s=float(settings.run_timeout_seconds), enforce_policies=True
    )
    stored = record_model_diagnosis(
        conn, run_id=run_id, incident_id=incident_id, run=run, model_id=client.model, model_digest=digest
    )
    if recordings_dir is not None and stored.status is DiagnosisStatus.VALIDATED:
        write_recording(recordings_dir, stored)
    return stored


def _diagnose_with_hosted(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    incident_id: str,
    built: EvidencePacket,
    settings: Settings,
    client: HostedModelClient,
) -> StoredDiagnosis:
    # No availability probe first, unlike local: a provider has no digest to ask for, and every call to it spends the
    # daily budget. A provider that cannot be reached fails the first call, and the diagnosis needs review with why.
    run = diagnosis.diagnose(built, client, think=False, deadline_s=float(settings.run_timeout_seconds),
                             enforce_policies=True)
    usage = tuple(
        CallTokens(call_index=call.call_index,
                   prompt_tokens=None if call.reply is None else call.reply.prompt_eval_count,
                   completion_tokens=None if call.reply is None else call.reply.eval_count)
        for call in run.calls
    )
    # Never written as a recording: recorded mode replays local outputs only.
    return record_model_diagnosis(conn, run_id=run_id, incident_id=incident_id, run=run, model_id=client.model,
                                  model_digest=None, mode=ModelMode.HOSTED, provider_host=client.provider_host,
                                  token_usage=usage)


def _replay_recorded(
    conn: sqlite3.Connection, *, run_id: str, incident_id: str, built: EvidencePacket, recordings_dir: Path | None
) -> StoredDiagnosis:
    # The model's input is the packet rendered by one prompt version, so both must match for a replay.
    packet_hash = built.packet_hash()
    source = next(
        (
            stored for stored in reversed(list_diagnoses(conn, run_id=run_id, incident_id=incident_id))
            if stored.mode is ModelMode.LOCAL and stored.status is DiagnosisStatus.VALIDATED
            and stored.packet_hash == packet_hash and stored.prompt_version == diagnosis.PROMPT_VERSION
        ),
        None,
    )
    if source is not None:
        recording, recorded_from = _recording_from(source), f"diagnosis:{source.diagnosis_id}"
    else:
        found = None if recordings_dir is None else find_recording(recordings_dir, packet_hash)
        if found is None:
            raise ContractError(
                f"no recorded local output exists for packet {packet_hash[:16]}; recorded mode replays only an "
                "identical packet, so run local mode on this packet first"
            )
        if found.prompt_version != diagnosis.PROMPT_VERSION:
            raise ContractError(
                f"the recording for packet {packet_hash[:16]} was made with prompt {found.prompt_version}, but the "
                f"current prompt is {diagnosis.PROMPT_VERSION}, so the model saw different input; record it again "
                "in local mode"
            )
        recording, recorded_from = found, f"recording:{packet_hash}.json"
    try:  # a recording made under older rules must still pass today's validation and policies
        diagnosis.validate_diagnosis(recording.output.model_dump(mode="json"), built)
    except diagnosis.DiagnosisContractError as invalid:
        raise ContractError(f"the recorded output no longer passes validation: {invalid}") from invalid
    stored = StoredDiagnosis(
        run_id=run_id,
        diagnosis_id=new_diagnosis_id(),
        incident_id=incident_id,
        mode=ModelMode.RECORDED,
        model_id=recording.model_id,
        model_digest=recording.model_digest,
        thinking=recording.thinking,
        prompt_version=recording.prompt_version,
        validator_version=VALIDATOR_VERSION,
        policies_enforced=True,
        rules_version=None,
        recorded_from=recorded_from,
        recorded_original_wall_ms=recording.recorded_wall_ms,
        packet_hash=packet_hash,
        status=DiagnosisStatus.VALIDATED,
        output=recording.output,
        reasons=(),
        unsafe_proposals=recording.unsafe_proposals,
        elapsed_ms=recording.elapsed_ms,
        created_wall_ms=storage.wall_clock_ms(),
    )
    _insert(conn, stored)
    return stored
