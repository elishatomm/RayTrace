"""Shared by the val-7 repair measurement's stages: the samples, the scoring cases and the two validators.

PREREGISTRATION.md (this folder) fixes the measurement. This module only reads committed files:

* **Sample S**: the distinct stored packets behind the stored model answers that val-6 accepts and val-7 refuses.
  They are found here, not typed in: every validated model answer the mechanical citation check reads
  (scripts/citation_mechanical_check.py, its own loader), plus the 18 answers of evaluation/runs/dev_diag_p5_smoke.
  The pre-registration names 43 such answers; any other count stops the stage.
* **Sample C**: the 48 frozen M4 packets, evaluation/traces/test_frozen.jsonl (30) and dev_think_off.jsonl (18).
* **Scoring**: evaluation.run.score against each packet's stored expected answer (M4's results, a round's or the
  selective test's cases.json, or experiments/m0/expected.json), as tests/test_validator_v7.py scores.
* **The validators**: val-7 is the package under test (``recovery_workbench.diagnosis``); val-6 is the previous
  diagnosis.py, loaded from a file whose SHA-256 is fixed below, so its verdicts are val-6's own, not inferred.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from pydantic import JsonValue

from recovery_workbench import diagnosis
from recovery_workbench.contracts import DiagnosisOutput, EvidencePacket, sha256_hex
from recovery_workbench.evaluation import run as evaluation_run

REPOSITORY_DIR = Path(__file__).resolve().parents[2]
PREREGISTRATION_FILE = "PREREGISTRATION.md"
PREREGISTRATION_SHA256 = "9f44b758096f28c14c96d0740859da92f40bedab72ea47d178050a4733e2867b"  # as committed in 4f88dda
VAL6_DIAGNOSIS_SHA256 = "85bbebb58a851b6792f5c0bf098209cab67eb505c28496d2a12bf59aedd2f69b"  # diagnosis.py at 74b8e15
NEWLY_REFUSED_ANSWERS = 43  # the pre-registration's count
M4_TRACES = {"test_frozen": 30, "dev_think_off": 18}
SMOKE_ANSWERS = "evaluation/runs/dev_diag_p5_smoke/answers.json"
MECHANICAL_CHECK = "scripts/citation_mechanical_check.py"
VAL7_RULES = {"a": "rationale names deployment IDs that are not candidate changes of this packet",
              "b": "faulting_change_id names a deployment that no item in supporting_evidence_ids mentions"}


# --------------------------------------------------------------------------- loading modules by path


def load_module(name: str, path: Path) -> ModuleType:
    """A module from a file under its own name; dataclasses in it look their module up in sys.modules."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {path} as a module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def val6_module(path: Path) -> ModuleType:
    """val-6's diagnosis.py, refused unless it is byte for byte the file the measurement names."""
    digest = sha256_hex(path.read_bytes())
    if digest != VAL6_DIAGNOSIS_SHA256:
        raise SystemExit(f"{path} has SHA-256 {digest}, not val-6's {VAL6_DIAGNOSIS_SHA256}; refusing to run")
    module = load_module("val6_diagnosis", path)
    if module.VALIDATOR_VERSION != "val-6" or module.PROMPT_VERSION != diagnosis.PROMPT_VERSION:
        raise SystemExit(f"{path} reports {module.VALIDATOR_VERSION} / {module.PROMPT_VERSION}, not val-6 / "
                         f"{diagnosis.PROMPT_VERSION}")
    return module


# --------------------------------------------------------------------------- verdicts


@dataclass(frozen=True)
class Verdict:
    accepted: bool
    reasons: tuple[str, ...]

    def as_json(self) -> dict[str, JsonValue]:
        return {"accepted": self.accepted, "reasons": list(self.reasons)}


def verdict(validator: ModuleType, content: str | None, built: EvidencePacket) -> Verdict | None:
    """One validator's verdict on a reply's text, as its diagnose loop reaches it; None when there is no reply."""
    if content is None:
        return None
    if not content.strip():
        return Verdict(False, ("the reply has no JSON content",))
    try:
        candidate = json.loads(content)
    except json.JSONDecodeError as exc:
        return Verdict(False, (f"the reply is not valid JSON: {exc.msg} at character {exc.pos}",))
    try:
        validator.validate_diagnosis(candidate, built)
    except validator.DiagnosisContractError as refused:
        return Verdict(False, tuple(refused.reasons))
    return Verdict(True, ())


def val7_rules_in(reasons: Sequence[str]) -> list[str]:
    """Which val-7 rules a list of refusal reasons holds: "a", "b", both or none."""
    return [rule for rule, prefix in VAL7_RULES.items() if any(reason.startswith(prefix) for reason in reasons)]


# --------------------------------------------------------------------------- scoring


def output_of(content: str | None) -> DiagnosisOutput | None:
    """The reply as a DiagnosisOutput when it has that shape (whatever a validator says of it), else None."""
    if content is None:
        return None
    try:
        return DiagnosisOutput.model_validate(json.loads(content))
    except (json.JSONDecodeError, ValueError):
        return None


def scored(case: evaluation_run.Case, output: DiagnosisOutput | None) -> dict[str, JsonValue]:
    """Outcome and change correctness; an answer that is missing or not shaped like one scores as not correct."""
    result = evaluation_run.score(case, output)
    return {"outcome_correct": result["outcome_correct"], "change_correct": result["change_correct"],
            "hypothesis_correct": result["hypothesis_correct"]}


def scoring_case(expected_entry: Mapping[str, JsonValue], built: EvidencePacket) -> evaluation_run.Case:
    return evaluation_run.Case(
        run_id=str(expected_entry.get("run_id") or expected_entry.get("case_id") or expected_entry.get("packet_id")),
        seed=int(expected_entry.get("seed", 0)), family=str(expected_entry["family"]),  # type: ignore[arg-type]
        variant=str(expected_entry["variant"]), params=expected_entry.get("params", {}),  # type: ignore[arg-type]
        cutoff_ms=built.incident.evidence_cutoff_ms, expected=expected_entry["expected"],  # type: ignore[arg-type]
        oracle={}, provider_records=Path("unused"))


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _trace_records(root: Path, label: str) -> Iterator[dict]:
    for line in (root / "evaluation" / "traces" / f"{label}.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def expected_entry(root: Path, source: str, case_id: str) -> dict[str, JsonValue]:
    """The stored expected answer (with family, variant, seed and params) for one stored answer's packet."""
    if source.startswith("evaluation/"):
        label = source.rsplit("/", 1)[1] if source.startswith("evaluation/traces/") else "dev_think_off"
        cases = _read(root / "evaluation" / "runs" / label / "results.json")["cases"]
        return next(case for case in cases if case["run_id"] == case_id)
    if source.startswith("experiments/m0/"):
        entry = _read(root / "experiments" / "m0" / "expected.json")["packets"][case_id]
        return {"packet_id": case_id, "family": entry["family"], "variant": json.dumps(entry["variant"], sort_keys=True),
                "expected": {key: entry[key] for key in ("hypothesis", "faulting_change_id", "affected_method",
                                                         "acceptable_operations")}}
    folder = root / source.rsplit("/", 1)[0]
    return next(case for case in _read(folder / "cases.json")["cases"] if case["case_id"] == case_id)


# --------------------------------------------------------------------------- the samples


@dataclass(frozen=True)
class SampleCase:
    """One packet to diagnose, where it is stored, and how its answers are scored."""

    packet_hash: str
    label: str  # where the packet is stored, and its case or run ID
    packet: EvidencePacket
    expected: dict[str, JsonValue]
    stored_answers: tuple[str, ...] = ()  # sample S: the stored answers behind it

    @property
    def case(self) -> evaluation_run.Case:
        return scoring_case(self.expected, self.packet)


def _smoke_answers(root: Path) -> Iterator[tuple[str, str, Mapping[str, JsonValue], dict]]:
    packets = {record["run_id"]: record["packet"] for record in _trace_records(root, "dev_think_off")}
    for answer in _read(root / SMOKE_ANSWERS)["answers"]:
        if answer["status"] == "validated" and answer["output"] is not None:
            yield "evaluation/runs/dev_diag_p5_smoke", answer["run_id"], answer["output"], packets[answer["run_id"]]


def stored_model_answers(root: Path) -> Iterator[tuple[str, str, Mapping[str, JsonValue], dict]]:
    """(source, case ID, output, packet document) for every validated stored model answer, as the val-7 agent read them."""
    check = load_module("citation_mechanical_check", root / MECHANICAL_CHECK)
    for found in check.all_stored_diagnoses(root):
        if found.system == "model" and found.status == "validated" and found.output is not None:
            yield found.source, found.case_id, found.output, found.packet
    yield from _smoke_answers(root)


def sample_s(root: Path, val6: ModuleType) -> list[SampleCase]:
    """The distinct packets behind the stored answers val-6 accepts and val-7 refuses, in order of first answer."""
    behind: dict[str, list[str]] = {}
    packets: dict[str, EvidencePacket] = {}
    entries: dict[str, dict[str, JsonValue]] = {}
    newly_refused = 0
    for source, case_id, output, document in stored_model_answers(root):
        built = EvidencePacket.model_validate(document)
        content = json.dumps(output)
        old, new = verdict(val6, content, built), verdict(diagnosis, content, built)
        if not (old is not None and new is not None and old.accepted and not new.accepted):
            continue
        newly_refused += 1
        digest = built.packet_hash()
        behind.setdefault(digest, []).append(f"{source} {case_id}")
        packets[digest] = built
        entry = expected_entry(root, source, case_id)
        if digest in entries and entries[digest]["expected"] != entry["expected"]:
            raise SystemExit(f"packet {digest[:16]} has two different expected answers; refusing to score it")
        entries.setdefault(digest, entry)
    if newly_refused != NEWLY_REFUSED_ANSWERS:
        raise SystemExit(f"{newly_refused} stored answers are newly refused, not the pre-registration's "
                         f"{NEWLY_REFUSED_ANSWERS}; the sample is not the pre-registered one, so the stage stops")
    return [SampleCase(digest, behind[digest][0], packets[digest], entries[digest], tuple(behind[digest]))
            for digest in behind]


def sample_c(root: Path) -> list[SampleCase]:
    """The 48 frozen M4 packets, TEST then development, in trace order."""
    found = []
    for label, count in M4_TRACES.items():
        cases = {case["run_id"]: case for case in _read(root / "evaluation" / "runs" / label / "results.json")["cases"]}
        records = list(_trace_records(root, label))
        if len(records) != count:
            raise SystemExit(f"evaluation/traces/{label}.jsonl holds {len(records)} packets, not {count}")
        for record in records:
            built = EvidencePacket.model_validate(record["packet"])
            if built.packet_hash() != record["packet_hash"]:
                raise SystemExit(f"{label} {record['run_id']}: the stored packet no longer hashes to its record")
            found.append(SampleCase(built.packet_hash(), f"evaluation/traces/{label} {record['run_id']}", built,
                                    cases[record["run_id"]]))
    return found


def checked_preregistration(directory: Path) -> dict[str, JsonValue]:
    path = directory / PREREGISTRATION_FILE
    digest = sha256_hex(path.read_bytes())
    if digest != PREREGISTRATION_SHA256:
        raise SystemExit(f"{path} has SHA-256 {digest}, not the committed pre-registration's {PREREGISTRATION_SHA256}")
    return {"file": PREREGISTRATION_FILE, "sha256": digest, "commit": "4f88dda"}
