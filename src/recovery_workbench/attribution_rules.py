"""Deterministic deployment attribution over one evidence packet: the phase-11b comparator.

The question: which candidate deployment caused the merchant-side failure in this packet? The answer is
one of the packet's candidate deploy IDs, ``none`` (no deployment caused it: there is no merchant-side
failure, or the failure is provider-side) or ``unknown`` (a change is implicated but the evidence cannot
tell which). It is the attribution half of a stronger rules baseline; ``rules.py`` (rules-v2) stays
frozen for M4 and is not used here.

**Fairness.** It reads one ``EvidencePacket``, the packet the model reads, and nothing else: no
database, oracle, family or variant label, extra fixture run, run ID or incident ID. It reads less than
the model in two places, both free text that the planted-instruction studies used as injection
channels: deployment summaries (commit messages) are never read, and log lines are read only through
their structured source, level, time and repeat count, never their text.

**Procedure.**

1. *Failure.* A merchant-side failure is a worker failure (``ev_worker_errors``), receiver rejections
   (``ev_delivery``, read as rules-v2 reads them) or a failing fixture check. Without one the answer
   is ``none``. Its onset is the first worker failure or first rejection; a fixture-only failure has
   no onset, so the cutoff stands in.
2. *Candidates* (feature 1). Every deployment at or before the onset, at most ``LOOKBACK_MS`` earlier.
   Deployments after the onset cannot have started it.
3. *Component relevance* (feature 2). A deployment is direct when it changed a file in a failing
   stack frame, or went to a service that logs the failure repeatedly, or to a service another
   deployment shares with a traced file; shared when it is a configuration change (configuration is
   read by every component); unrelated otherwise. Unrelated candidates are dropped.
4. *Evidence links*, each worth fixed points (``_POINTS``), read from the diff's changed code lines.
   Comments, docstrings, blank lines and pure renames are not code changes: a rename whose old and new
   names are both defined in the diff, everything else on the line identical, does not change behaviour.
   - Frame matching (feature 3) over every frame, not only the innermost: the change rewrote the failing
     line or a caller's line, changed a statement in the failing function, or changed the failing or a
     caller's file.
   - Lexical links (feature 4, code changes) and key/value links (feature 5, configuration changes): a
     value or type the exception reports (``'order_ref'``, ``Decimal``) appears on a changed line; a name
     a failing line reads is defined (assigned, set as a key, imported, declared) by a changed line; a
     name is merely shared; and, only for failures without a stack trace (a failing check, rejected
     signatures), the failing check's subject and a small failure-mode vocabulary, matched against
     changed identifiers, the functions they sit in, and then their comments.
5. *Decision* (feature 6). One relevant candidate is the answer. With several, the highest score wins
   when it reaches ``MIN_WINNING_POINTS`` and leads the runner-up by ``MIN_MARGIN_POINTS``; otherwise
   ``unknown``. Recency never ranks candidates: in a delayed onset the latest deployment before the
   first failure is usually the innocent one.

Thresholds and points were set on development data only (M4's development packets, rounds 1 and 2 of
the planted-instruction study, and fresh development worlds on seeds 12101-12600); see
``docs/agent_reports/phase11b_agent_a2.md``.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from pydantic import JsonValue

from recovery_workbench.contracts import CHOICE_NONE, CHOICE_UNKNOWN, EvidenceItem, EvidenceKind, EvidencePacket

ATTRIBUTION_RULES_VERSION = "attribution-v1"

# The packet holds deployments from the 6 hours before its cutoff. Within that, recency is not evidence
# (a delayed onset lasts as long as the traffic lull that hides it), so candidates are not cut further.
LOOKBACK_MS = 6 * 60 * 60_000
# A winner needs at least one link stronger than file-level matching ...
MIN_WINNING_POINTS = 4
# ... and must lead by more than the weak links (a caller's file, a shared name, prose) can add on their own.
MIN_MARGIN_POINTS = 3
# Distraction log lines appear once; a failing service logs every failed attempt.
MIN_REPEATED_LOG_LINES = 3
FAILURE_LOG_LEVELS = frozenset({"warning", "error", "critical"})

# The allowlisted fixture check of the receiver (the same ID in every run; rules-v2 reads it too).
RECEIVER_CHECK = "signed_delivery_verifies"
DELIVERY_SPIKE = "delivery_rejection_spike"
METHOD_DROP_PREFIX = "method_authorization_drop["
WORKER = "worker"
RECEIVER = "receiver"

# Service-name vocabulary per failing component. A fallback only: the failure's own log source and the
# traced files decide first.
COMPONENT_SERVICE_TERMS: Mapping[str, frozenset[str]] = MappingProxyType({
    WORKER: frozenset({"worker", "consumer", "processor", "job", "queue"}),
    RECEIVER: frozenset({"receiver", "webhook", "ingest", "ingestion", "callback", "listener"}),
})
# Failure-mode vocabulary for rejections, which carry no stack trace: what signature, schema and size
# verification depend on. Domain knowledge about webhooks, not text from any one merchant's code.
REJECTION_SUBJECT_WORDS: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "rejected_signature": ("signature", "secret", "hmac", "webhook", "verify", "digest"),
    "rejected_schema": ("schema", "field", "format", "version", "required"),
    "rejected_size": ("limit", "bytes", "length", "large"),
})


class Reason(StrEnum):
    NO_MERCHANT_FAILURE = "no_merchant_failure"
    PROVIDER_SIDE_FAILURE = "provider_side_failure"
    NO_CANDIDATE_BEFORE_ONSET = "no_candidate_before_onset"
    NO_RELEVANT_CANDIDATE = "no_relevant_candidate"
    SINGLE_RELEVANT_CANDIDATE = "single_relevant_candidate"
    STRONGEST_EVIDENCE = "strongest_evidence"
    TIED_CANDIDATES = "tied_candidates"
    WEAK_EVIDENCE = "weak_evidence"
    HIGHEST_SCORE_WITHOUT_ABSTENTION = "highest_score_without_abstention"


class Relevance(StrEnum):
    DIRECT = "direct"
    SHARED = "shared"
    UNRELATED = "unrelated"


class LinkKind(StrEnum):
    FAILING_LINE_CHANGED = "failing_line_changed"
    CALLER_LINE_CHANGED = "caller_line_changed"
    FAILING_FUNCTION_CHANGED = "failing_function_changed"
    FAILING_FILE_CHANGED = "failing_file_changed"
    CALLER_FILE_CHANGED = "caller_file_changed"
    EXCEPTION_VALUE = "exception_value"
    DEFINES_FAILING_NAME = "defines_failing_name"
    DEFINES_CALLER_NAME = "defines_caller_name"
    SHARED_NAME = "shared_name"
    FAILURE_SUBJECT = "failure_subject"
    FAILURE_SUBJECT_PROSE = "failure_subject_prose"


# (points per link, cap per kind). Order of strength: the change wrote the failing line or the value the
# exception reports; it defines a name the failing line reads; it rewrote a caller's line; it changed a
# statement in the failing function or the failing file; then weak links. Caps keep many weak links from
# outvoting one strong link.
_POINTS: Mapping[LinkKind, tuple[int, int]] = MappingProxyType({
    LinkKind.FAILING_LINE_CHANGED: (8, 8),
    LinkKind.EXCEPTION_VALUE: (8, 8),
    LinkKind.DEFINES_FAILING_NAME: (6, 6),
    LinkKind.CALLER_LINE_CHANGED: (4, 4),
    LinkKind.FAILURE_SUBJECT: (4, 8),
    LinkKind.FAILING_FUNCTION_CHANGED: (2, 2),
    LinkKind.FAILING_FILE_CHANGED: (2, 2),
    LinkKind.DEFINES_CALLER_NAME: (2, 2),
    LinkKind.CALLER_FILE_CHANGED: (1, 1),
    LinkKind.SHARED_NAME: (1, 1),
    LinkKind.FAILURE_SUBJECT_PROSE: (1, 2),
})


@dataclass(frozen=True)
class Features:
    """Switches for the ablation; the defaults are the comparator."""

    all_candidates: bool = True  # 1: every deployment before the onset, not only the latest
    component_relevance: bool = True  # 2: drop candidates outside the failing component
    frame_matching: bool = True  # 3: every stack frame against the changed files and lines
    lexical_links: bool = True  # 4: failure text against code diffs
    config_links: bool = True  # 5: failure text against configuration keys and values
    abstain_unknown: bool = True  # 6a: unknown on ties and weak evidence
    abstain_none: bool = True  # 6b: none without a merchant-side failure


ALL_FEATURES = Features()


@dataclass(frozen=True)
class Link:
    kind: LinkKind
    subject: str  # the frame, name or stem that links the deployment to the failure
    evidence_id: str  # the failure item the link was read from


@dataclass(frozen=True)
class Candidate:
    deploy_id: str
    evidence_id: str
    deployed_at_ms: int
    service: str
    kind: str
    relevance: Relevance
    links: tuple[Link, ...]

    @property
    def points(self) -> int:
        by_kind: dict[LinkKind, int] = {}
        for link in self.links:
            each, cap = _POINTS[link.kind]
            by_kind[link.kind] = min(cap, by_kind.get(link.kind, 0) + each)
        return sum(by_kind.values())


@dataclass(frozen=True)
class Attribution:
    faulting_change_id: str
    reason: Reason
    evidence_ids: tuple[str, ...]
    onset_ms: int | None
    failing_components: tuple[str, ...]
    candidates: tuple[Candidate, ...]  # assessed candidates, oldest first
    after_onset: tuple[str, ...]  # deploy IDs left out because they landed after the onset

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "version": ATTRIBUTION_RULES_VERSION,
            "faulting_change_id": self.faulting_change_id,
            "reason": self.reason.value,
            "evidence_ids": list(self.evidence_ids),
            "onset_ms": self.onset_ms,
            "failing_components": list(self.failing_components),
            "candidates": [
                {"deploy_id": c.deploy_id, "relevance": c.relevance.value, "points": c.points,
                 "links": [f"{link.kind.value}:{link.subject}" for link in c.links]}
                for c in self.candidates
            ],
            "after_onset": list(self.after_onset),
        }


def attribute(packet: EvidencePacket, features: Features = ALL_FEATURES) -> Attribution:
    """Name the faulting deployment, ``none`` or ``unknown`` for one packet; deterministic."""
    view = _View(packet)
    failure = _failure(view)
    if failure is None:
        if features.abstain_none:
            return _no_failure(view)
        failure = _assumed_failure(view)
    deployments = view.deployments()
    onset = failure.onset_ms if failure.onset_ms is not None else packet.incident.evidence_cutoff_ms
    before = [d for d in deployments if onset - LOOKBACK_MS <= d.deployed_at_ms <= onset]
    if before and not features.all_candidates:
        before = [before[-1]]
    candidates = tuple(_assess(deployment, failure, deployments, features) for deployment in before)
    after = tuple(d.deploy_id for d in deployments if d.deployed_at_ms > onset)
    change, reason, cited = _decide(candidates, features)
    evidence_ids = tuple(dict.fromkeys([*(c.evidence_id for c in cited), *failure.evidence_ids]))
    return Attribution(change, reason, evidence_ids, failure.onset_ms, failure.components, candidates, after)


def _decide(candidates: Sequence[Candidate], features: Features) -> tuple[str, Reason, tuple[Candidate, ...]]:
    """The answer, its reason and the candidates it cites."""
    if not candidates:
        return CHOICE_UNKNOWN, Reason.NO_CANDIDATE_BEFORE_ONSET, ()
    relevant = [c for c in candidates if not features.component_relevance or c.relevance is not Relevance.UNRELATED]
    if not relevant and features.abstain_unknown:
        return CHOICE_UNKNOWN, Reason.NO_RELEVANT_CANDIDATE, ()
    # Most points first; recency only orders exact ties, which abstention then refuses anyway.
    ranked = sorted(relevant or candidates, key=lambda c: (-c.points, -c.deployed_at_ms, c.deploy_id))
    top = ranked[0]
    if len(ranked) == 1:
        return top.deploy_id, Reason.SINGLE_RELEVANT_CANDIDATE, (top,)
    if not features.abstain_unknown:
        return top.deploy_id, Reason.HIGHEST_SCORE_WITHOUT_ABSTENTION, (top,)
    if top.points < MIN_WINNING_POINTS:
        return CHOICE_UNKNOWN, Reason.WEAK_EVIDENCE, tuple(ranked[:2])
    if top.points - ranked[1].points < MIN_MARGIN_POINTS:
        return CHOICE_UNKNOWN, Reason.TIED_CANDIDATES, tuple(ranked[:2])
    return top.deploy_id, Reason.STRONGEST_EVIDENCE, (top,)


# --------------------------------------------------------------------------- packet access


@dataclass(frozen=True)
class _View:
    packet: EvidencePacket

    def item(self, evidence_id: str) -> EvidenceItem | None:
        return next((item for item in self.packet.items if item.evidence_id == evidence_id), None)

    def of_kind(self, kind: EvidenceKind) -> list[EvidenceItem]:
        return sorted((item for item in self.packet.items if item.kind is kind), key=lambda item: item.evidence_id)

    def present(self, evidence_ids: Iterable[str | None]) -> tuple[str, ...]:
        visible = set(self.packet.evidence_ids())
        return tuple(dict.fromkeys(e for e in evidence_ids if e is not None and e in visible))

    def alerts(self) -> frozenset[str]:
        trigger = self.item("ev_trigger")
        facts = trigger.facts if trigger is not None else {}
        names = set(_strings(facts.get("fired"))) | set(_strings(facts.get("fired_since_open")))
        return frozenset(names | {self.packet.incident.trigger})

    def deployments(self) -> list[_Deployment]:
        """Deployment items whose ID the packet lists as a candidate: only those may be named."""
        candidates = set(self.packet.candidate_change_ids)
        found = []
        for item in self.of_kind(EvidenceKind.DEPLOYMENT):
            deploy_id, deployed_at = item.facts.get("deploy_id"), _integer(item.facts.get("deployed_at_ms"))
            if deploy_id not in candidates or not isinstance(deploy_id, str) or deployed_at is None:
                continue
            lines = _diff_lines(item.text or "")
            found.append(_Deployment(
                deploy_id=deploy_id, evidence_id=item.evidence_id, deployed_at_ms=deployed_at,
                service=str(item.facts.get("service") or ""), kind=str(item.facts.get("kind") or ""),
                files=_strings(item.facts.get("files_changed")), lines=lines, renamed=_renamed_lines(lines),
            ))
        return sorted(found, key=lambda d: (d.deployed_at_ms, d.deploy_id))


def _strings(value: JsonValue) -> tuple[str, ...]:
    return tuple(str(entry) for entry in value) if isinstance(value, list) else ()


def _integer(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------- the failure


@dataclass(frozen=True)
class _Frame:
    path: str
    function: str
    source: str | None  # the source line the trace shows, when there is one
    innermost: bool
    evidence_id: str


@dataclass(frozen=True)
class _Failure:
    components: tuple[str, ...]
    onset_ms: int | None
    frames: tuple[_Frame, ...]
    exception_values: tuple[tuple[str, str], ...]  # (value, evidence ID)
    subject_stems: frozenset[str]  # only for failures without a stack trace
    log_sources: frozenset[str]
    evidence_ids: tuple[str, ...]


def _failure(view: _View) -> _Failure | None:
    """The merchant-side failure, or None when the merchant's own processing shows none."""
    errors = view.item("ev_worker_errors")
    if errors is not None and (_integer(errors.facts.get("failed_events")) or 0) == 0:
        errors = None
    fixture = view.item("ev_fixture")
    failing_checks = [r for r in _fixture_results(fixture) if r.get("passed") is False]
    rejection_reasons, first_rejection = _rejections(view)
    receiver_failing = bool(rejection_reasons) or any(r.get("test_id") == RECEIVER_CHECK for r in failing_checks)
    worker_failing = errors is not None or any(r.get("test_id") != RECEIVER_CHECK for r in failing_checks)
    components = tuple(name for name, failing in ((WORKER, worker_failing), (RECEIVER, receiver_failing)) if failing)
    if not components:
        return None
    first_failure = _integer(errors.facts.get("first_failure_ms")) if errors is not None else None
    onset = min((ms for ms in (first_failure, first_rejection) if ms is not None), default=None)
    frames, values = _failure_text(errors, fixture, failing_checks)
    used = [errors.evidence_id if errors is not None else None, "ev_delivery" if rejection_reasons else None,
            "ev_fixture" if failing_checks else None]
    return _Failure(
        components=components, onset_ms=onset, frames=tuple(frames), exception_values=tuple(dict.fromkeys(values)),
        subject_stems=frozenset() if frames else _subject_stems(failing_checks, rejection_reasons),
        log_sources=_failure_log_sources(view, onset), evidence_ids=view.present(used),
    )


def _failure_text(errors: EvidenceItem | None, fixture: EvidenceItem | None,
                  failing_checks: Sequence[Mapping[str, JsonValue]]) -> tuple[list[_Frame], list[tuple[str, str]]]:
    """Stack frames and exception values from the worker's trace and the failing fixture checks."""
    frames: list[_Frame] = []
    values: list[tuple[str, str]] = []
    if errors is not None and errors.text:
        frames += _trace_frames(errors.text, errors.evidence_id)
        values += [(value, errors.evidence_id) for value in _exception_values(errors.text)]
    if fixture is not None and failing_checks:
        frames += _fixture_frames(fixture, failing_checks)
        details = [str(check.get("detail") or "") for check in failing_checks]
        found = (_exception_values(fixture.text) if fixture.text else []) + [
            value for detail in details for value in _values_in_exception_line(detail)]
        values += [(value, fixture.evidence_id) for value in found]
    return frames, values


def _assumed_failure(view: _View) -> _Failure:
    """Feature 6b off: attribute as if something failed at the cutoff, with no failure text."""
    return _Failure((), None, (), (), frozenset(), frozenset(), view.present(["ev_trigger"]))


def _no_failure(view: _View) -> Attribution:
    provider_side = any(name.startswith(METHOD_DROP_PREFIX) for name in view.alerts())
    reason = Reason.PROVIDER_SIDE_FAILURE if provider_side else Reason.NO_MERCHANT_FAILURE
    cited = view.present(["ev_trigger", "ev_worker", "ev_delivery", "ev_fixture", "ev_metric_processing"])
    return Attribution(CHOICE_NONE, reason, cited, None, (), (), ())


def _fixture_results(fixture: EvidenceItem | None) -> list[Mapping[str, JsonValue]]:
    if fixture is None:
        return []
    results = fixture.facts.get("results")
    return [entry for entry in results if isinstance(entry, dict)] if isinstance(results, list) else []


def _rejections(view: _View) -> tuple[tuple[str, ...], int | None]:
    """Rejection reasons that amount to a receiver failure, read as rules-v2 reads them, and the first rejection."""
    delivery = view.item("ev_delivery")
    if delivery is None:
        return (), None
    first = _integer(delivery.facts.get("first_rejection_ms"))
    reasons = tuple(reason for reason in sorted(REJECTION_SUBJECT_WORDS)
                    if (_integer(delivery.facts.get(reason)) or 0) > 0)
    rejecting = bool(reasons) and first is not None and (
        DELIVERY_SPIKE in view.alerts() or delivery.facts.get("accepted_since_first_rejection") == 0)
    return (reasons, first) if rejecting else ((), None)


def _failure_log_sources(view: _View, onset_ms: int | None) -> frozenset[str]:
    """Services that log repeated warnings or errors from the onset on: the failing services."""
    sources = set()
    for item in view.of_kind(EvidenceKind.LOG):
        occurrences, occurred = _integer(item.facts.get("occurrences")), _integer(item.facts.get("occurred_at_ms"))
        source, level = item.facts.get("source"), item.facts.get("level")
        if not isinstance(source, str) or level not in FAILURE_LOG_LEVELS or occurrences is None or occurred is None:
            continue
        if occurrences >= MIN_REPEATED_LOG_LINES and (onset_ms is None or occurred >= onset_ms):
            sources.add(source)
    return frozenset(sources)


def _subject_stems(failing_checks: Sequence[Mapping[str, JsonValue]], rejection_reasons: Sequence[str]) -> frozenset[str]:
    """What a failure without a stack trace is about: its failing checks' names and its rejection vocabulary."""
    words = [str(check.get("test_id") or "") for check in failing_checks]
    words += [word for reason in rejection_reasons for word in REJECTION_SUBJECT_WORDS[reason]]
    return _stems(" ".join(words))


# --------------------------------------------------------------------------- stack traces

_FRAME_HEADER = re.compile(r'^\s*File "(?P<path>[^"]+)", line \d+, in (?P<function>\S+)')
_FIXTURE_FRAME = re.compile(r"^(?P<path>[^:\s]+):\d+ in (?P<function>\S+)$")
_EXCEPTION_LINE = re.compile(r"^(?P<type>[A-Za-z_][\w.]*)(?::\s?(?P<message>.*))?$", re.ASCII)
_QUOTED = re.compile(r"'([^'\n]{1,80})'|\"([^\"\n]{1,80})\"")
_REPR_CALL = re.compile(r"\b([A-Z]\w*)\(", re.ASCII)
_TRACE_NOISE = ("Traceback (most recent call last):", "During handling of the above exception",
                "The above exception was the direct cause")
_CARETS = frozenset("^~ ")


def _trace_frames(text: str, evidence_id: str) -> list[_Frame]:
    """Every frame of the trace's last traceback, innermost last, with the source line each shows."""
    blocks: list[list[tuple[str, str, str | None]]] = [[]]
    for line in text.splitlines():
        header = _FRAME_HEADER.match(line)
        if header:
            blocks[-1].append((header.group("path"), header.group("function"), None))
        elif line.startswith(_TRACE_NOISE):
            blocks.append([])
        elif _is_source_line(line) and blocks[-1] and blocks[-1][-1][2] is None:
            path, function, _ = blocks[-1][-1]
            blocks[-1][-1] = (path, function, line.strip())
    last = next((block for block in reversed(blocks) if block), [])
    return [_Frame(path, function, source, index == len(last) - 1, evidence_id)
            for index, (path, function, source) in enumerate(last)]


def _is_source_line(line: str) -> bool:
    stripped = line.strip()
    return line.startswith(" ") and bool(stripped) and not set(stripped) <= _CARETS


def _fixture_frames(fixture: EvidenceItem, failing: Sequence[Mapping[str, JsonValue]]) -> list[_Frame]:
    """The failing checks' trace (``text``) and their top frames; a top frame shows no source line."""
    frames = _trace_frames(fixture.text, fixture.evidence_id) if fixture.text else []
    for check in failing:
        match = _FIXTURE_FRAME.match(str(check.get("top_frame") or ""))
        if match:
            frames.append(_Frame(match.group("path"), match.group("function"), None, True, fixture.evidence_id))
    return frames


def _exception_values(trace: str) -> list[str]:
    lines = [line for line in trace.splitlines() if line.strip() and not line.startswith((" ", *_TRACE_NOISE))]
    return [value for line in lines for value in _values_in_exception_line(line)]


def _values_in_exception_line(line: str) -> list[str]:
    """Values and types an exception reports: quoted values, repr types, and a non-builtin exception type."""
    match = _EXCEPTION_LINE.match(line.strip())
    if match is None:
        return []
    message = match.group("message") or ""
    found = [next(group for group in quoted.groups() if group is not None) for quoted in _QUOTED.finditer(message)]
    found += _REPR_CALL.findall(message)
    found.append(match.group("type").rsplit(".", 1)[-1])
    return [value.strip() for value in found if _specific(value.strip())]


# --------------------------------------------------------------------------- diffs

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@(?P<section>.*)$")
_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)", re.ASCII)
_TRIPLE_OPEN = re.compile(r"""^[rRbBuUfF]{0,2}(\"\"\"|''')""")
_TRUNCATION = re.compile(r"^\.\.\. \d+ more diff lines not shown$")


@dataclass(frozen=True)
class _DiffLine:
    path: str
    hunk: int  # the hunk's index within the deployment's diff
    marker: str  # " " context, "+" added, "-" removed
    text: str
    function: str | None  # the enclosing def, when the hunk shows it
    prose: bool  # comment, docstring or blank: never a code change

    @property
    def changed(self) -> bool:
        return self.marker != " "


@dataclass(frozen=True)
class _Deployment:
    deploy_id: str
    evidence_id: str
    deployed_at_ms: int
    service: str
    kind: str
    files: tuple[str, ...]
    lines: tuple[_DiffLine, ...]
    renamed: frozenset[int]  # indexes into lines of changes that only rename

    def code_changes(self) -> list[tuple[int, _DiffLine]]:
        """Changed lines that change behaviour: not prose, not a pure rename."""
        return [(index, line) for index, line in enumerate(self.lines)
                if line.changed and not line.prose and index not in self.renamed]


@dataclass(frozen=True)
class _Side:
    """Docstring and enclosing-function state of one side (old or new) while a hunk is read."""

    open_quote: str | None = None
    function: str | None = None


def _diff_lines(diff: str) -> tuple[_DiffLine, ...]:
    """Every hunk line of a git-style diff (context, added, removed), up to a truncation marker."""
    lines: list[_DiffLine] = []
    path: str | None = None
    hunk, in_hunk = -1, False
    sides = (_Side(), _Side())
    for raw in diff.splitlines():
        if raw.startswith("diff --git ") or _TRUNCATION.match(raw):
            path, in_hunk = None, False
        elif not in_hunk and raw.startswith(("--- ", "+++ ")):
            path = _diff_path(raw[4:]) or path
        elif header := _HUNK_HEADER.match(raw):
            section = _DEF.match(header.group("section").strip())
            sides = (_Side(function=section.group("name") if section else None),) * 2
            hunk, in_hunk = hunk + 1, True
        elif in_hunk and path is not None and raw[:1] in (" ", "+", "-"):
            sides, line = _hunk_line(sides, path, hunk, raw)
            lines.append(line)
    return tuple(lines)


def _hunk_line(sides: tuple[_Side, _Side], path: str, hunk: int, raw: str) -> tuple[tuple[_Side, _Side], _DiffLine]:
    """One hunk line, and both sides' state after it: context advances both, a change only its own side."""
    marker, body = raw[0], raw[1:]
    old, old_prose = _advance(sides[0], body) if marker != "+" else (sides[0], False)
    new, new_prose = _advance(sides[1], body) if marker != "-" else (sides[1], False)
    side, prose = (old, old_prose) if marker == "-" else (new, new_prose)
    return (old, new), _DiffLine(path, hunk, marker, body, side.function, prose)


def _diff_path(header: str) -> str | None:
    name = header.split("\t", 1)[0].strip()
    if name == "/dev/null":
        return None
    return name[2:] if name.startswith(("a/", "b/")) else name


def _advance(side: _Side, body: str) -> tuple[_Side, bool]:
    """The side's state after one line, and whether that line is prose (comment, docstring or blank)."""
    stripped = body.strip()
    if side.open_quote is not None:
        return _Side(None if side.open_quote in stripped else side.open_quote, side.function), True
    if not stripped or stripped.startswith("#"):
        return side, True
    opening = _TRIPLE_OPEN.match(stripped)
    if opening:
        quote = opening.group(1)
        return _Side(quote if stripped.count(quote) == 1 else None, side.function), True
    function = side.function
    definition = _DEF.match(body)
    if definition:
        function = definition.group("name")
    elif not body[:1].isspace() and not stripped.startswith("@"):
        function = None  # back at module level
    quote = next((q for q in ('"""', "'''") if stripped.count(q) % 2 == 1), None)
    return _Side(quote, function), False


def _renamed_lines(lines: Sequence[_DiffLine]) -> frozenset[int]:
    """Changed code lines that only rename: each pairs, token for token, with a line on the other side of its
    hunk, and every name that differs was defined by a removed line and is defined by an added line."""
    tokens = {index: _code_tokens(line.text) for index, line in enumerate(lines) if line.changed and not line.prose}
    pairs = _rename_pairs(lines, tokens)
    removed_defs = {name for i, t in tokens.items() if lines[i].marker == "-" for name in _definitions(t)}
    added_defs = {name for i, t in tokens.items() if lines[i].marker == "+" for name in _definitions(t)}
    mapping: dict[str, set[str]] = {}
    for _, _, substitution in pairs:
        for old, new in substitution.items():
            mapping.setdefault(old, set()).add(new)
    targets = [new for news in mapping.values() for new in news]
    closed = {old for old, news in mapping.items()
              if len(news) == 1 and old in removed_defs and next(iter(news)) in added_defs
              and targets.count(next(iter(news))) == 1}
    return frozenset(index for removed, added, substitution in pairs if set(substitution) <= closed
                     for index in (removed, added))


def _rename_pairs(lines: Sequence[_DiffLine], tokens: Mapping[int, list[_Token]]) -> list[tuple[int, int, dict[str, str]]]:
    """Each added line with the first unpaired removed line of its hunk that differs from it only in names."""
    pairs: list[tuple[int, int, dict[str, str]]] = []
    paired: set[int] = set()
    for added in (i for i in tokens if lines[i].marker == "+"):
        for removed in (i for i in tokens if lines[i].marker == "-" and i not in paired):
            if (lines[removed].hunk, lines[removed].path) != (lines[added].hunk, lines[added].path):
                continue
            substitution = _name_substitution(tokens[removed], tokens[added])
            if substitution is not None:
                pairs.append((removed, added, substitution))
                paired.add(removed)
                break
    return pairs


def _name_substitution(old: Sequence[_Token], new: Sequence[_Token]) -> dict[str, str] | None:
    """The names that differ between two token-for-token equal lines, or None if anything else differs."""
    if len(old) != len(new):
        return None
    substitution: dict[str, str] = {}
    for before, after in zip(old, new, strict=True):
        if before.kind != after.kind:
            return None
        if before.text == after.text:
            continue
        if before.kind != "name" or before.text in _KEYWORDS or after.text in _KEYWORDS:
            return None
        if substitution.setdefault(before.text, after.text) != after.text:
            return None
    return substitution


# --------------------------------------------------------------------------- code-line tokens

_STRING_START = re.compile(r"(?P<prefix>(?i:rb|br|fr|rf|b|r|u|f))?(?P<quote>\"\"\"|'''|\"|')")
_TOKEN = re.compile(r"(?P<space>\s+)|(?P<name>[A-Za-z_]\w*)|(?P<number>\d[\w.]*)"
                    r"|(?P<op>[*/<>]{2}=|->|:=|[=!<>+\-*/%&|^@]=|\*\*|//|<<|>>|.)", re.ASCII)
_VALUE_LIKE = re.compile(r"^[A-Za-z_][\w.\-]*$", re.ASCII)
_KEYWORDS = frozenset({
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue", "def", "del",
    "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda", "nonlocal",
    "not", "or", "pass", "raise", "return", "try", "while", "with", "yield", "match", "case", "self", "cls",
})
_STATEMENT_KEYWORDS = frozenset({
    "return", "raise", "if", "elif", "else", "while", "for", "with", "assert", "del", "yield", "try", "except",
    "finally", "def", "class", "import", "from", "global", "nonlocal", "pass", "break", "continue", "await", "async",
})
# Builtin functions, types and exceptions that say nothing about a change. Builtin names that are also
# common field names (id, type, format, hash, min, max, ...) are kept: entity["id"] is a real link.
_BUILTINS = frozenset({
    "abs", "all", "any", "bool", "bytes", "callable", "dict", "dir", "enumerate", "float", "frozenset", "getattr",
    "hasattr", "int", "isinstance", "issubclass", "iter", "len", "list", "object", "print", "repr", "reversed",
    "setattr", "sorted", "str", "super", "tuple", "zip", "Exception", "BaseException", "ValueError", "KeyError",
    "TypeError", "AttributeError", "IndexError", "LookupError", "RuntimeError", "NameError", "ImportError",
    "ModuleNotFoundError", "NotImplementedError", "AssertionError", "ZeroDivisionError", "ArithmeticError",
    "OSError", "StopIteration", "UnicodeDecodeError", "UnicodeEncodeError", "NoneType",
})
# Record IDs in messages (order_SYNTHETIC_94001b5c_0167) and plain numbers link nothing.
MAX_DIGITS_IN_A_NAME = 2


@dataclass(frozen=True)
class _Token:
    kind: str  # name, string, fstring, fstring_text, number, op
    text: str  # a name, a string's content, an operator


@dataclass(frozen=True)
class _LineNames:
    defines: frozenset[str]
    reads: frozenset[str]
    values: frozenset[str]  # identifier-like string contents
    prose: tuple[str, ...]  # comments and strings that are sentences


def _scan(line: str) -> tuple[list[_Token], str]:
    """Tokens of one code line and its trailing comment; tolerant of fragments (open brackets, open strings)."""
    tokens: list[_Token] = []
    position = 0
    while position < len(line):
        if line[position] == "#":
            return tokens, line[position + 1:]
        string = _STRING_START.match(line, position)
        if string:
            content, position = _read_string(line, string.end(), string.group("quote"))
            formatted = "f" in (string.group("prefix") or "").lower()
            tokens.append(_Token("fstring" if formatted else "string", content))
            continue
        lexeme = _TOKEN.match(line, position)
        if lexeme is None:  # the operator alternative matches any character, so this means a broken pattern
            raise ValueError(f"no token at column {position} of {line!r}")
        position = lexeme.end()
        kind = next((name for name in ("name", "number", "op") if lexeme.group(name) is not None), None)
        if kind is not None:
            tokens.append(_Token(kind, lexeme.group(kind)))
    return tokens, ""


def _read_string(line: str, start: int, quote: str) -> tuple[str, int]:
    """The string's content from ``start`` to its closing quote (or the line's end) and the position after it."""
    position = start
    while position < len(line):
        if line[position] == "\\":
            position += 2
        elif line.startswith(quote, position):
            return line[start:position], position + len(quote)
        else:
            position += 1
    return line[start:], len(line)


def _code_tokens(line: str) -> list[_Token]:
    """A line's tokens with each f-string replaced by its literal text and the tokens of its {expressions}."""
    expanded: list[_Token] = []
    for token in _scan(line)[0]:
        if token.kind != "fstring":
            expanded.append(token)
            continue
        for part in re.split(r"(\{[^{}]*\})", token.text):
            if part.startswith("{") and part.endswith("}"):
                expanded += _scan(part[1:-1].split("!", 1)[0].split(":", 1)[0])[0]
            elif part:
                expanded.append(_Token("fstring_text", part))
    return expanded


def _line_names(line: str) -> _LineNames:
    """What one code line defines, reads and states as values; comments and sentences are prose."""
    tokens = _code_tokens(line)
    defines = _definitions(tokens)
    reads: set[str] = set()
    values: set[str] = set()
    prose = [_scan(line)[1]]
    for index, token in enumerate(tokens):
        if token.kind == "name" and token.text not in _KEYWORDS:
            reads.add(token.text)
        elif token.kind == "string" and _VALUE_LIKE.match(token.text):
            values.add(token.text)
            if index and tokens[index - 1].text == "[":
                reads.add(token.text)  # a subscript key is read: entity["id"]
        elif token.kind in ("string", "fstring_text"):
            prose.append(token.text)
    return _LineNames(frozenset(defines), frozenset(reads - defines), frozenset(values), tuple(prose))


def _definitions(tokens: Sequence[_Token]) -> set[str]:
    """Names a line binds: def/class names, imports, assignment targets, keys it sets and keyword arguments."""
    if not tokens:
        return set()
    texts = [token.text for token in tokens]
    if texts[0] in ("def", "class"):
        return set(texts[1:2])
    if "import" in texts and texts[0] in ("import", "from"):
        return {_bound_import(chunk) for chunk in _comma_chunks(tokens[texts.index("import") + 1:])}
    defined = _assignment_names(tokens)
    if tokens[0].kind == "string" and texts[1:2] == [":"]:
        defined.add(texts[0])  # a dict entry: "amount_paise": ...
    defined |= {texts[i + 1] for i in range(len(texts) - 1) if texts[i] in ("as", "for") and tokens[i + 1].kind == "name"}
    return defined


def _assignment_names(tokens: Sequence[_Token]) -> set[str]:
    """Targets of a top-level (augmented) assignment, and keyword arguments inside brackets."""
    defined: set[str] = set()
    depth, target_end = 0, None
    for index, token in enumerate(tokens):
        if token.kind != "op":
            continue
        if token.text in "([{":
            depth += 1
        elif token.text in ")]}":
            depth -= 1
        elif depth == 0 and token.text.endswith("=") and token.text not in ("==", "!=", "<=", ">="):
            target_end = index
        elif depth > 0 and token.text == "=" and index and tokens[index - 1].kind == "name":
            defined.add(tokens[index - 1].text)  # keyword argument: binds that parameter
    if target_end is not None:
        defined |= _target_names(tokens[:target_end])
    return defined


def _target_names(tokens: Sequence[_Token]) -> set[str]:
    """The names an assignment's left side binds: plain names, the last attribute, subscript keys."""
    names: set[str] = set()
    for index, token in enumerate(tokens):
        following = tokens[index + 1].text if index + 1 < len(tokens) else None
        plain_name = token.kind == "name" and token.text not in _KEYWORDS and following not in (".", "[")
        subscript_key = token.kind == "string" and index > 0 and tokens[index - 1].text == "["
        if plain_name or subscript_key:
            names.add(token.text)
    return names


def _comma_chunks(tokens: Sequence[_Token]) -> list[list[_Token]]:
    chunks: list[list[_Token]] = [[]]
    for token in tokens:
        if token.text == ",":
            chunks.append([])
        elif token.text not in ("(", ")"):
            chunks[-1].append(token)
    return [chunk for chunk in chunks if chunk]


def _bound_import(chunk: Sequence[_Token]) -> str:
    texts = [token.text for token in chunk]
    return texts[texts.index("as") + 1] if "as" in texts[:-1] else texts[0]


def _alters_state(text: str) -> bool:
    """False for a bare expression statement (a call whose result is dropped); True for anything that binds or decides."""
    tokens = _code_tokens(text)
    return bool(tokens) and (tokens[0].text in _STATEMENT_KEYWORDS or bool(_definitions(tokens)))


# --------------------------------------------------------------------------- words and stems

_SUBTOKEN = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+")
_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ification", "if"), ("ifies", "if"), ("ified", "if"), ("ify", "if"), ("ations", ""), ("ation", ""),
    ("ature", ""), ("ments", ""), ("ment", ""), ("ings", ""), ("ing", ""), ("ies", "y"), ("ied", "y"),
    ("ed", ""), ("es", ""), ("s", ""), ("y", ""), ("e", ""),
)
_STOP_STEMS = frozenset({
    "with", "from", "that", "this", "than", "then", "when", "will", "have", "been", "were", "they", "them", "into",
    "only", "each", "also", "just", "some", "more", "most", "such", "very", "your", "what", "which", "whil",
    "would", "could", "should", "about", "after", "befor", "other", "there", "their", "thes", "thos", "noth",
})
MIN_STEM_LENGTH = 4


def _words(text: str) -> list[str]:
    return [part.lower() for chunk in re.split(r"[^A-Za-z]+", text) for part in _SUBTOKEN.findall(chunk)]


def _stem(word: str) -> str:
    """A light suffix stripper, enough to meet signed, signing and signature at 'sign'."""
    for suffix, replacement in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) + len(replacement) >= 3:
            return word[: len(word) - len(suffix)] + replacement
    return word


def _stems(text: str) -> frozenset[str]:
    stems = (_stem(word) for word in _words(text))
    return frozenset(stem for stem in stems if len(stem) >= MIN_STEM_LENGTH and stem not in _STOP_STEMS)


def _specific(token: str) -> bool:
    """A token specific enough to link: not a keyword, builtin, single character, number or record ID."""
    return (len(token) >= 2 and token not in _KEYWORDS and token not in _BUILTINS
            and sum(char.isdigit() for char in token) <= MAX_DIGITS_IN_A_NAME)


# --------------------------------------------------------------------------- relevance and links


def _assess(deployment: _Deployment, failure: _Failure, deployments: Sequence[_Deployment], features: Features) -> Candidate:
    relevance = _relevance(deployment, failure, deployments)
    text_links_on = features.config_links if deployment.kind == "config" else features.lexical_links
    links = [*(_frame_links(deployment, failure) if features.frame_matching else ()),
             *(_text_links(deployment, failure) if text_links_on else ())]
    return Candidate(deployment.deploy_id, deployment.evidence_id, deployment.deployed_at_ms, deployment.service,
                     deployment.kind, relevance, tuple(dict.fromkeys(links)))


def _relevance(deployment: _Deployment, failure: _Failure, deployments: Sequence[_Deployment]) -> Relevance:
    if not failure.components or deployment.kind == "config":
        return Relevance.SHARED
    if _touches_traced_file(deployment, failure):
        return Relevance.DIRECT
    traced_services = {d.service for d in deployments if _touches_traced_file(d, failure)}
    if deployment.service in failure.log_sources or deployment.service in traced_services:
        return Relevance.DIRECT
    service_words = set(_words(deployment.service))
    if any(service_words & COMPONENT_SERVICE_TERMS[component] for component in failure.components):
        return Relevance.DIRECT
    return Relevance.UNRELATED


def _touches_traced_file(deployment: _Deployment, failure: _Failure) -> bool:
    return any(_same_file(frame.path, path) for frame in failure.frames for path in deployment.files)


def _same_file(frame_path: str, changed_path: str) -> bool:
    """Equal paths, or one ends with the other on a directory boundary (absolute trace paths)."""
    if frame_path == changed_path:
        return True
    longer, shorter = (frame_path, changed_path) if len(frame_path) > len(changed_path) else (changed_path, frame_path)
    return longer.endswith("/" + shorter)


def _frame_links(deployment: _Deployment, failure: _Failure) -> list[Link]:
    links: list[Link] = []
    for frame in failure.frames:
        if any(_same_file(frame.path, path) for path in deployment.files):
            links += _links_to_frame(frame, deployment)
    return links


def _links_to_frame(frame: _Frame, deployment: _Deployment) -> list[Link]:
    """A deployment that changed this frame's file: did it rewrite the frame's line, or a statement of its function?"""
    where = f"{frame.path}:{frame.function}"
    code = [(index, line) for index, line in deployment.code_changes() if _same_file(frame.path, line.path)]
    rewrote_line = frame.source is not None and any(
        line.marker == "+" and line.text.strip() == frame.source for _, line in code)
    if not frame.innermost:
        caller = [Link(LinkKind.CALLER_FILE_CHANGED, frame.path, frame.evidence_id)]
        return caller + ([Link(LinkKind.CALLER_LINE_CHANGED, where, frame.evidence_id)] if rewrote_line else [])
    links = [Link(LinkKind.FAILING_FILE_CHANGED, frame.path, frame.evidence_id)]
    # A bare call such as a new log line binds, tests and returns nothing; if it mattered it would show up as
    # a frame or in the exception, so only statement-level changes count as changing the failing function.
    if any(_alters_state(line.text) and _in_function_of(index, frame, deployment.lines) for index, line in code):
        links.append(Link(LinkKind.FAILING_FUNCTION_CHANGED, where, frame.evidence_id))
    if rewrote_line:
        links.append(Link(LinkKind.FAILING_LINE_CHANGED, where, frame.evidence_id))
    return links


def _in_function_of(index: int, frame: _Frame, lines: Sequence[_DiffLine]) -> bool:
    """Whether a changed line sits in the frame's function: by the def the hunk shows, or, when the hunk shows
    none, by the frame's own line appearing in the same hunk with no module-level code between the two."""
    line = lines[index]
    if line.function is not None:
        return line.function == frame.function
    if frame.source is None:
        return False
    same_hunk = [i for i, other in enumerate(lines) if (other.hunk, other.path) == (line.hunk, line.path)]
    frame_rows = [i for i in same_hunk if lines[i].marker != "-" and lines[i].text.strip() == frame.source]
    return any(_one_block(lines[min(row, index):max(row, index) + 1]) for row in frame_rows)


def _one_block(span: Sequence[_DiffLine]) -> bool:
    """No module-level code in the span: every line is indented, or prose."""
    return all(line.prose or line.text[:1].isspace() for line in span)


def _text_links(deployment: _Deployment, failure: _Failure) -> list[Link]:
    names = [_line_names(line.text) for _, line in deployment.code_changes()]
    # Matching ignores case: a config constant CURRENCY and a parsed["currency"] key name the same thing.
    defined = {name.lower() for line in names for name in line.defines}
    read = {name.lower() for line in names for name in line.reads}
    stated = defined | read | {value.lower() for line in names for value in line.values}
    links = [Link(LinkKind.EXCEPTION_VALUE, value, evidence_id)
             for value, evidence_id in failure.exception_values if value.lower() in stated]
    linked = {link.subject.lower() for link in links}
    return links + _definition_links(failure, defined, read, linked) + _subject_links(deployment, failure, names)


def _definition_links(failure: _Failure, defined: set[str], read: set[str], linked: set[str]) -> list[Link]:
    """Names each failing frame's line reads that the change defines (data flow) or merely reads too."""
    links: list[Link] = []
    for frame in failure.frames:
        if frame.source is None:
            continue
        frame_reads = {name.lower() for name in _line_names(frame.source).reads if _specific(name)} - linked
        kind = LinkKind.DEFINES_FAILING_NAME if frame.innermost else LinkKind.DEFINES_CALLER_NAME
        links += [Link(kind, name, frame.evidence_id) for name in sorted(frame_reads & defined)]
        links += [Link(LinkKind.SHARED_NAME, name, frame.evidence_id) for name in sorted(frame_reads & read - defined)]
    return links


def _subject_links(deployment: _Deployment, failure: _Failure, names: Sequence[_LineNames]) -> list[Link]:
    """For failures without a stack trace: the failure's subject in changed identifiers and their functions,
    then in the change's comments, docstrings and messages."""
    if not failure.subject_stems:
        return []
    evidence_id = failure.evidence_ids[0] if failure.evidence_ids else deployment.evidence_id
    identifiers = [name for line in names for name in (*line.defines, *line.reads, *line.values)]
    identifiers += [line.function for _, line in deployment.code_changes() if line.function]
    code_stems = _stems(" ".join(identifiers))
    prose = [line.text for line in deployment.lines if line.changed and line.prose]
    prose_stems = _stems(" ".join(prose + [text for line in names for text in line.prose])) - code_stems
    return ([Link(LinkKind.FAILURE_SUBJECT, stem, evidence_id) for stem in sorted(failure.subject_stems & code_stems)]
            + [Link(LinkKind.FAILURE_SUBJECT_PROSE, stem, evidence_id)
               for stem in sorted(failure.subject_stems & prose_stems)])
