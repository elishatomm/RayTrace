"""Mechanical, non-LLM checks of every stored diagnosis's citations, and the owner's citation spot-check sheet.

Run inside the test image, whose working directory is the repository root:

    docker run --rm -i --init --network none --read-only --tmpfs /tmp --entrypoint python \\
        rw-agent-c:1 - [--json | --spot-check-sheet] [--source-label TEXT] < citation_mechanical_check.py

Without a flag it prints the Markdown report; ``--json`` prints every per-diagnosis result; ``--spot-check-sheet``
prints the sheet for EVALUATION.md §9's human spot check. It reads only committed files (evaluation/traces/*.jsonl and
the experiments' cases, packets, answers and traces), writes nothing, and uses the standard library only. It imports
nothing from recovery_workbench, so the checks do not reuse the code they check.

Nothing here decides whether evidence supports a claim: EVALUATION.md §9 leaves that to a person and forbids
model-judged evaluation. Hard checks test the stored data, and the IDs a rationale names, against the packet. Soft
checks report where the numbers, quoted terms, clock times and IDs in a rationale occur in the packet, as pointers.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import random
import re
import sys
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

EPOCH_MS_FLOOR = 10**12  # 2001-09-09 in epoch ms; every duration in these packets is far smaller
CHOICES_WITHOUT_TARGET = frozenset({"none", "unknown"})
SPOT_CHECK_SEED = 20261112  # summarize.SPOT_CHECK_SEED, copied so the draw is reproduced independently
SPOT_CHECK_LABEL = "test_frozen"
SPOT_CHECK_COUNT = 10
EXPERIMENT_DIRS = (
    "experiments/injection",
    "experiments/injection/round2",
    "experiments/injection/round3",
    "experiments/selective",
)
EVIDENCE_ITEMS_LINE = "Evidence items:"
PROMPT_END_LINE = "Return the JSON object now."
AS_RECEIVED = "as the model received it"
INCIDENT_CARD = "incident card"
UNCITED = "uncited item"

IDENTIFIER_CHAR = r"A-Za-z0-9_"
EVIDENCE_ID_IN_TEXT = re.compile(rf"(?<![{IDENTIFIER_CHAR}])ev_[a-z0-9_]*[a-z0-9]")
DEPLOYMENT_ID_IN_TEXT = re.compile(rf"(?<![{IDENTIFIER_CHAR}])(?:deploy|dep)_[0-9a-f]{{6,}}(?![{IDENTIFIER_CHAR}])")
ENTITY_ID_IN_TEXT = re.compile(
    rf"(?<![{IDENTIFIER_CHAR}])(?:order|pay|evt|inc|run|refund|merchant|int|acc)_[A-Za-z0-9_]*[0-9][A-Za-z0-9_]*"
)
SINGLE_QUOTED = re.compile(rf"(?<![{IDENTIFIER_CHAR}])'([^'\n]{{1,200}}?)'(?![{IDENTIFIER_CHAR}])")
DOUBLE_QUOTED = re.compile(r'"([^"\n]{1,200}?)"')
BACKTICKED = re.compile(r"`([^`\n]{1,200}?)`")
CLOCK_TOKEN = re.compile(r"(?<![\w:.])(\d{1,2}:\d{2}(?::\d{2})?)(Z?)(?![\w:])")
DATE_TOKEN = re.compile(r"(?<![\w-])(\d{4}-\d{2}-\d{2})(?![\w-])")
NUMBER_TOKEN = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)(%?)(?!\w|\.\d)")
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[]|ev_)")
LOOSE_SEPARATORS = re.compile(r"[\s_-]+")


# --------------------------------------------------------------------------- loading the stored diagnoses


@dataclass(frozen=True)
class StoredDiagnosis:
    """One stored answer with the packet it was made from and, for model answers, the prompt the model received."""

    source: str
    case_id: str
    system: str
    status: str
    output: Mapping[str, object] | None
    packet: Mapping[str, object]
    recorded_hashes: tuple[tuple[str, str], ...]  # (where the hash is recorded, the hash)
    prompt: str | None
    system_prompt: str | None

    @property
    def key(self) -> str:
        return f"{self.source} {self.case_id} {self.system}"


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def prompt_messages(calls: object) -> tuple[str | None, str | None]:
    """The first stored call's system and user messages; (None, None) when no call kept its messages."""
    for call in calls if isinstance(calls, list) else ():
        messages = call.get("messages") if isinstance(call, dict) else None
        if messages:
            system = next((m["content"] for m in messages if m.get("role") == "system"), None)
            user = next((m["content"] for m in messages if m.get("role") == "user"), None)
            return system, user
    return None, None


def evaluation_diagnoses(root: Path) -> Iterator[StoredDiagnosis]:
    for path in sorted((root / "evaluation" / "traces").glob("*.jsonl")):
        label = path.stem
        environment = root / "evaluation" / "runs" / label / "environment.json"
        rules_version = read_json(environment)["versions"]["rules"] if environment.exists() else "rules"
        for record in read_jsonl(path):
            for answer_key, answer in record["answers"].items():
                system_prompt, prompt = prompt_messages(answer.get("calls"))
                yield StoredDiagnosis(
                    source=f"evaluation/traces/{label}", case_id=record["run_id"],
                    system="model" if answer_key == "model" else rules_version, status=answer["status"],
                    output=answer.get("output"), packet=record["packet"],
                    recorded_hashes=(("trace", record["packet_hash"]),), prompt=prompt, system_prompt=system_prompt,
                )


def m0_diagnoses(root: Path) -> Iterator[StoredDiagnosis]:
    base = root / "experiments" / "m0"
    packets = {path.stem: read_json(path) for path in sorted((base / "packets").glob("*.json"))}
    expected = read_json(base / "expected.json")["packets"]
    for path in sorted((base / "traces").glob("*.jsonl")):
        for record in read_jsonl(path):
            packet_id = record["packet_id"]
            system_prompt, prompt = prompt_messages(record.get("calls"))
            yield StoredDiagnosis(
                source=f"experiments/m0/traces/{path.stem}", case_id=packet_id, system="model",
                status=record["status"], output=record.get("output"), packet=packets[packet_id],
                recorded_hashes=(("trace", record["packet_hash"]), ("expected.json", expected[packet_id]["packet_hash"])),
                prompt=prompt, system_prompt=system_prompt,
            )


def experiment_diagnoses(root: Path) -> Iterator[StoredDiagnosis]:
    for relative in EXPERIMENT_DIRS:
        base = root / relative
        cases = {case["case_id"]: case for case in read_json(base / "cases.json")["cases"]}
        for answers_path in sorted(base.glob("answers*.json")):
            arm = answers_path.stem.removeprefix("answers").removeprefix("_") or "live"
            trace_path = base / "traces" / f"{arm}.jsonl"
            traces = {record["case_id"]: record for record in read_jsonl(trace_path)} if trace_path.exists() else {}
            for answer in read_json(answers_path)["answers"]:
                yield from _experiment_answer(f"{relative}/{arm}", base, cases[answer["case_id"]], answer,
                                              traces.get(answer["case_id"], {}))


def _experiment_answer(source: str, base: Path, case: Mapping, answer: Mapping, trace: Mapping) -> Iterator[StoredDiagnosis]:
    """The model's answer and, in the selective arms, the rules-v3 answer stored beside it."""
    packet = read_json(base / case["packet_file"])
    hashes = [("answer", answer["packet_hash"]), ("cases.json", case["packet_hash"])]
    if "packet_hash" in trace:
        hashes.append(("trace", trace["packet_hash"]))
    system_prompt, prompt = prompt_messages(trace.get("calls"))
    yield StoredDiagnosis(source=source, case_id=answer["case_id"], system="model", status=answer["status"],
                          output=answer.get("output"), packet=packet, recorded_hashes=tuple(hashes), prompt=prompt,
                          system_prompt=system_prompt)
    rules = answer.get("rules_v3")
    if isinstance(rules, dict) and isinstance(rules.get("output"), dict):
        yield StoredDiagnosis(source=source, case_id=answer["case_id"], system=str(rules.get("rules_version", "rules-v3")),
                              status="validated", output=rules["output"], packet=packet,
                              recorded_hashes=tuple(hashes[:2]), prompt=None, system_prompt=None)


def all_stored_diagnoses(root: Path) -> list[StoredDiagnosis]:
    return [*evaluation_diagnoses(root), *m0_diagnoses(root), *experiment_diagnoses(root)]


# --------------------------------------------------------------------------- what an item holds, as text and numbers


def canonical_sha256(value: object) -> str:
    """contracts.canonical_json's rules (sorted keys, compact, ASCII, no NaN), re-stated here."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def utc(epoch_ms: int) -> datetime:
    return datetime.fromtimestamp(epoch_ms / 1000, tz=UTC)


def utc_text(epoch_ms: int) -> str:
    return utc(epoch_ms).strftime("%Y-%m-%d %H:%M:%SZ")


def is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def leaves(value: object, path: str) -> Iterator[tuple[str, object]]:
    """Every scalar under ``value`` with its path; map keys come out as string leaves too, marked ``(key)``."""
    if isinstance(value, dict):
        for key, entry in value.items():
            yield f"{path}.{key} (key)", str(key)
            yield from leaves(entry, f"{path}.{key}")
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            yield from leaves(entry, f"{path}[{index}]")
    else:
        yield path, value


def epoch_leaves(value: object, path: str, under_ms_key: bool = False) -> Iterator[tuple[str, int]]:
    """Every timestamp: an integer of at least EPOCH_MS_FLOOR held under a key ending in ``_ms``."""
    if isinstance(value, dict):
        for key, entry in value.items():
            yield from epoch_leaves(entry, f"{path}.{key}", str(key).endswith("_ms"))
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            yield from epoch_leaves(entry, f"{path}[{index}]", under_ms_key)
    elif under_ms_key and is_number(value) and value >= EPOCH_MS_FLOOR:
        yield path, int(value)


def map_sums(value: object, path: str) -> Iterator[tuple[str, Decimal]]:
    """For each map holding two or more numbers directly: the sum of those numbers (``failed_by_reason`` totals)."""
    if isinstance(value, dict):
        direct = [Decimal(str(entry)) for entry in value.values() if is_number(entry)]
        if len(direct) >= 2:
            yield f"sum of {path}", sum(direct, Decimal(0))
        for key, entry in value.items():
            yield from map_sums(entry, f"{path}.{key}")
    elif isinstance(value, list):
        for index, entry in enumerate(value):
            yield from map_sums(entry, f"{path}[{index}]")


def numbers_in_text(text: str) -> Iterator[tuple[Decimal, bool]]:
    """Each number written in ``text``, and whether it was written with a decimal point."""
    for match in NUMBER_TOKEN.finditer(text):
        yield Decimal(match.group(1).replace(",", "")), "." in match.group(1)


def is_ratio(value: object) -> bool:
    """A JSON float from 0 to 1: what a percentage can restate. Integer counts of 0 or 1 are not ratios."""
    return isinstance(value, float) and 0 <= value <= 1


def loose(text: str) -> str:
    return LOOSE_SEPARATORS.sub(" ", text.lower()).strip()


@dataclass(frozen=True)
class Haystack:
    """What one evidence item (or the incident card) holds, in the forms a rationale could repeat it."""

    label: str
    strings: tuple[tuple[str, str], ...]  # (path, text), the raw item first and the block as received last
    numbers: tuple[tuple[str, Decimal], ...]  # facts' numbers, then numbers written inside its texts
    ratios: tuple[tuple[str, Decimal], ...]  # facts' floats from 0 to 1, and decimals from 0 to 1 in its texts
    epochs: tuple[tuple[str, int], ...]
    fact_numbers: tuple[tuple[str, Decimal], ...]  # facts' numbers that are not timestamps: for loose arithmetic
    fact_ratios: tuple[tuple[str, Decimal], ...]  # facts' floats from 0 to 1: for loose differences in points
    sums: tuple[tuple[str, Decimal], ...]


def text_numbers(strings: Sequence[tuple[str, str]]) -> tuple[list[tuple[str, Decimal]], list[tuple[str, Decimal]]]:
    """Numbers written in an item's texts (not in its keys or ID), and those of them that read as ratios."""
    numbers, ratios = [], []
    for path, text in strings:
        if path.endswith("(key)") or path == "evidence ID":
            continue
        for number, has_point in numbers_in_text(text):
            numbers.append((f"{path} (number in the text)", number))
            if has_point and number <= 1:
                ratios.append((f"{path} (number in the text)", number))
    return numbers, ratios


def item_haystack(item: Mapping[str, object], block: str | None) -> Haystack:
    facts = item.get("facts", {})
    strings = [("evidence ID", str(item["evidence_id"])), ("title", str(item["title"]))]
    if item.get("text"):
        strings.append(("text", str(item["text"])))
    fact_leaves = list(leaves(facts, "facts"))
    strings += [(path, str(value)) for path, value in fact_leaves if isinstance(value, str)]
    if block is not None:
        strings.append((AS_RECEIVED, block))
    fact_numbers = [(path, Decimal(str(value))) for path, value in fact_leaves if is_number(value)]
    fact_ratios = [(path, Decimal(str(value))) for path, value in fact_leaves if is_ratio(value)]
    written, written_ratios = text_numbers(strings)
    epochs = [("known_at_ms", int(item["known_at_ms"])), *epoch_leaves(facts, "facts")]
    arithmetic = [(path, number) for path, number in fact_numbers if number < EPOCH_MS_FLOOR]
    return Haystack(str(item["evidence_id"]), tuple(strings), tuple(fact_numbers + written),
                    tuple(fact_ratios + written_ratios), tuple(epochs), tuple(arithmetic), tuple(fact_ratios),
                    tuple(map_sums(facts, "facts")))


def incident_haystack(packet: Mapping[str, object], prompt_header: str | None) -> Haystack:
    card = {**packet["incident"], "candidate_change_ids": packet.get("candidate_change_ids", []),
            "candidate_methods": packet.get("candidate_methods", [])}
    card_leaves = list(leaves(card, "incident"))
    strings = [(path, value) for path, value in card_leaves if isinstance(value, str)]
    numbers = [(path, Decimal(str(value))) for path, value in card_leaves if is_number(value)]
    ratios = [(path, Decimal(str(value))) for path, value in card_leaves if is_ratio(value)]
    if prompt_header is not None:
        strings.append((AS_RECEIVED, prompt_header))
        written, written_ratios = text_numbers([(AS_RECEIVED, prompt_header)])
        numbers += written
        ratios += written_ratios
    return Haystack(INCIDENT_CARD, tuple(strings), tuple(numbers), tuple(ratios),
                    tuple(epoch_leaves(card, "incident")), (), (), ())


def bounded(span: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![{IDENTIFIER_CHAR}]){re.escape(span)}(?![{IDENTIFIER_CHAR}])")


def string_hits(haystack: Haystack, span: str) -> list[str]:
    pattern = bounded(span)
    return [path for path, text in haystack.strings if pattern.search(text)]


# --------------------------------------------------------------------------- tokens a rationale states


@dataclass(frozen=True)
class Token:
    kind: str  # evidence_id, deployment_id, entity_id, quoted, number, percent, clock, date
    text: str
    sentence: int


def split_sentences(rationale: str) -> list[str]:
    return [part.strip() for part in SENTENCE_BREAK.split(rationale.strip()) if part.strip()]


def _blank(pattern: re.Pattern[str], text: str) -> str:
    return pattern.sub(lambda match: " " * len(match.group(0)), text)


def sentence_tokens(sentence: str, number: int) -> list[Token]:
    """IDs and quoted spans first; clock times, dates and numbers are read from what remains, so an ID's digits or a
    time's parts are never read as numbers."""
    found: list[tuple[int, Token]] = []
    rest = sentence
    for pattern, kind in ((EVIDENCE_ID_IN_TEXT, "evidence_id"), (DEPLOYMENT_ID_IN_TEXT, "deployment_id"),
                          (ENTITY_ID_IN_TEXT, "entity_id")):
        found += [(m.start(), Token(kind, m.group(0), number)) for m in pattern.finditer(rest)]
        rest = _blank(pattern, rest)
    ids = {token.text for _, token in found}
    for pattern in (SINGLE_QUOTED, DOUBLE_QUOTED, BACKTICKED):  # a quoted ID is checked once, as an ID
        found += [(m.start(), Token("quoted", m.group(1), number)) for m in pattern.finditer(sentence)
                  if m.group(1) not in ids]
    found += [(m.start(), Token("clock", m.group(1) + m.group(2), number)) for m in CLOCK_TOKEN.finditer(rest)]
    rest = _blank(CLOCK_TOKEN, rest)
    found += [(m.start(), Token("date", m.group(1), number)) for m in DATE_TOKEN.finditer(rest)]
    rest = _blank(DATE_TOKEN, rest)
    found += [(m.start(), Token("percent" if m.group(2) else "number", m.group(1) + m.group(2), number))
              for m in NUMBER_TOKEN.finditer(rest)]
    ordered = [token for _, token in sorted(found, key=lambda pair: pair[0])]
    return list(dict.fromkeys(ordered))  # once per sentence


def rationale_tokens(rationale: str) -> list[Token]:
    return [token for number, sentence in enumerate(split_sentences(rationale), start=1)
            for token in sentence_tokens(sentence, number)]


def decimals_of(text: str) -> int:
    return len(text.split(".", 1)[1]) if "." in text else 0


def half_unit(digits: str) -> Decimal:
    """Half of the last stated digit: 32.4 stands for anything in [32.35, 32.45]."""
    return Decimal(5) / Decimal(10) ** (decimals_of(digits) + 1)


def number_hits(token: Token, haystack: Haystack) -> list[str]:
    digits = token.text.replace(",", "")
    value = Decimal(digits)
    exact = [f"{path} = {number}" for path, number in haystack.numbers if number == value]
    if exact or decimals_of(digits) == 0:
        return exact
    return [f"{path} = {number}, rounded" for path, number in haystack.numbers
            if abs(number - value) <= half_unit(digits)]


def percent_hits(token: Token, haystack: Haystack) -> list[str]:
    digits = token.text.rstrip("%").replace(",", "")
    ratios = [f"{path} = {number}" for path, number in haystack.ratios
              if abs(number * 100 - Decimal(digits)) <= half_unit(digits)]
    return string_hits(haystack, token.text) + ratios


def clock_hits(token: Token, haystack: Haystack) -> list[str]:
    text = token.text.rstrip("Z")
    padded = ":".join(part.zfill(2) for part in text.split(":"))  # 9:05 reads as 09:05
    pattern = "%H:%M:%S" if text.count(":") == 2 else "%H:%M"
    verbatim = string_hits(haystack, token.text) or string_hits(haystack, text)
    return verbatim + [f"{path} = {utc_text(value)}" for path, value in haystack.epochs
                       if utc(value).strftime(pattern) == padded]


def date_hits(token: Token, haystack: Haystack) -> list[str]:
    return string_hits(haystack, token.text) + [f"{path} = {utc_text(value)}" for path, value in haystack.epochs
                                                 if utc(value).strftime("%Y-%m-%d") == token.text]


def direct_hits(token: Token, haystack: Haystack) -> list[str]:
    """Where ``token`` occurs as stated: verbatim for text, by value at its stated precision for numbers."""
    if token.kind in {"quoted", "evidence_id", "deployment_id", "entity_id"}:
        return string_hits(haystack, token.text)
    handlers = {"number": number_hits, "percent": percent_hits, "clock": clock_hits, "date": date_hits}
    return handlers[token.kind](token, haystack)


def loose_hits(token: Token, haystack: Haystack) -> list[str]:
    """A weaker match, tried only when nothing matches directly: a quoted span or ID after lower-casing and treating
    spaces, underscores and hyphens alike; a number as the difference of two figures, or the total of a map, in one
    item (a percentage as the difference of two ratios, in points)."""
    if token.kind in {"quoted", "entity_id", "deployment_id"}:
        pattern = bounded(loose(token.text))
        return [f"{path}, after normalising" for path, text in haystack.strings if pattern.search(loose(text))]
    if token.kind not in {"number", "percent"}:
        return []
    digits = token.text.rstrip("%").replace(",", "")
    value, tolerance, scale = Decimal(digits), half_unit(digits), Decimal(100 if token.kind == "percent" else 1)
    exact_only = token.kind == "number" and decimals_of(digits) == 0
    operands = haystack.fact_ratios if token.kind == "percent" else haystack.fact_numbers
    hits = []
    for (path_a, a), (path_b, b) in itertools.combinations(operands, 2):
        difference = abs(a - b) * scale
        if difference == value or (not exact_only and abs(difference - value) <= tolerance):
            hits.append(f"|{path_a} - {path_b}|{' x 100' if token.kind == 'percent' else ''} = {difference}")
    if token.kind == "number":
        hits += [f"{path} = {total}" for path, total in haystack.sums
                 if total == value or (not exact_only and abs(total - value) <= tolerance)]
    return hits


# --------------------------------------------------------------------------- the prompt, split into item blocks


def prompt_blocks(prompt: str, items: Sequence[Mapping[str, object]]) -> tuple[dict[str, str], str, str | None]:
    """Each item's block exactly as the model received it, the incident-card part, and a problem (None if none).

    Items are found in packet order by their whole header line, so text inside an untrusted fence that imitates a
    header can only end a block early if it reproduces the next item's exact header.
    """
    lines = prompt.split("\n")
    if EVIDENCE_ITEMS_LINE not in lines:
        return {}, prompt, f"the prompt has no {EVIDENCE_ITEMS_LINE!r} line"
    start = lines.index(EVIDENCE_ITEMS_LINE) + 1
    ends = [index for index, line in enumerate(lines) if line == PROMPT_END_LINE]
    end = ends[-1] if ends else len(lines)
    header_text = "\n".join(lines[:start])
    starts: list[int] = []
    cursor = start
    for item in items:
        prefix, suffix = f"[{item['evidence_id']}] {item['kind']}, known ", f": {item['title']}"
        found = next((i for i in range(cursor, end) if lines[i].startswith(prefix) and lines[i].endswith(suffix)), None)
        if found is None:
            return {}, header_text, f"no header line for {item['evidence_id']} after line {cursor}"
        starts.append(found)
        cursor = found + 1
    blocks = {}
    for index, item in enumerate(items):
        block = lines[starts[index]:starts[index + 1] if index + 1 < len(starts) else end]
        while block and block[-1] == "":
            block.pop()
        blocks[str(item["evidence_id"])] = "\n".join(block)
    return blocks, header_text, None


# --------------------------------------------------------------------------- the checks


HARD_CHECKS = {
    "H1": "The stored packet's SHA-256 (canonical JSON) equals every hash recorded beside the diagnosis",
    "H2": "Every cited ID (supporting and contradicting) is an item of that diagnosis's packet",
    "H3": "No ID is repeated within a list, and none is in both lists",
    "H4": "Every cited item's known_at_ms is at or before the packet's evidence cutoff",
    "H5": "Every timestamp inside a cited item's facts is at or before the evidence cutoff",
    "H6": "The stored prompt shows this packet's items in order, with its evidence cutoff (answers with a stored prompt)",
    "H7": "Every evidence ID the rationale names is an item of the packet",
    "H8": "Every deployment ID the rationale names is a candidate change in the packet",
    "H9": "An evidence_level of reproduced_in_fixture cites a fixture_check item as supporting",
}
PACKET_CHECKS = {
    "P1": "Every item of the packet (cited or not) is known at or before the evidence cutoff",
    "P2": "Every timestamp inside any item's facts is at or before the evidence cutoff",
}
SOFT_CHECKS = {
    "S1": "Evidence IDs the rationale names are cited (flag: named but not cited)",
    "S2": "A named faulting change has its deployment item cited as supporting",
    "S3": "A named affected method has its metric item cited as supporting",
    "S4": "Other IDs the rationale names (deployments, orders, payments, events) occur in a cited item",
    "S5": "Spans the rationale quotes ('...', \"...\" or `...`) occur verbatim in a cited item",
    "S6": "Numbers and percentages the rationale states occur in a cited item (a percentage may match a ratio)",
    "S7": "Clock times and dates the rationale states occur in a cited item",
    "S8": "The diagnosis cites at least one supporting item",
    "S9": "reproduced_in_fixture: a cited fixture_check item records a failing test",
}
TOKEN_CHECK = {"evidence_id": "S1", "deployment_id": "S4", "entity_id": "S4", "quoted": "S5", "number": "S6",
               "percent": "S6", "clock": "S7", "date": "S7"}
DIRECT_PLACES = ("cited", UNCITED, INCIDENT_CARD, "instructions")
LOOSE_PLACES = ("cited, loosely", "uncited, loosely")
PLACES = (*DIRECT_PLACES, *LOOSE_PLACES, "nowhere")


@dataclass(frozen=True)
class Hit:
    """One occurrence of a stated token: in which item, at which fact path, and how well that name fits the sentence."""

    label: str  # the item's evidence ID, or the incident card
    detail: str  # the path, and the value found there
    place: str  # cited, uncited item or incident card (with ", loosely" for a loose match)
    relevance: int  # word stems the item's ID and the fact's path share with the sentence

    def __str__(self) -> str:
        return f"{self.label}: {self.detail}"


@dataclass(frozen=True)
class TokenResult:
    token: Token
    place: str  # one of PLACES
    where: tuple[str, ...]  # the hits in that place, best-named first
    ranked: tuple[Hit, ...]  # every direct hit in any item or the card, best-named first
    outranked_by: Hit | None  # a better-named hit in an uncited item, when the token matched cited items only by value


@dataclass(frozen=True)
class DiagnosisResult:
    diagnosis: StoredDiagnosis
    hard: Mapping[str, list[str]]  # check -> failures; an empty list is a pass; absent means not applicable
    soft: Mapping[str, list[str]]  # check -> flags; an empty list means nothing to flag; absent means not applicable
    tokens: tuple[TokenResult, ...]
    blocks: Mapping[str, str]
    incident_card: str | None  # the prompt's text before its evidence items, exactly as the model received it


NAME_WORD = re.compile(r"[a-z]{4,}")
NAME_STOP_WORDS = frozenset({"facts", "text", "number", "model", "received", "title", "evidence", "with", "that",
                             "this", "from", "than", "were", "which", "shows", "show", "indicates", "indicating",
                             "ratio"})  # "ratio" ends most metric names, so it says nothing about which one
NUMERIC_KINDS = frozenset({"number", "percent", "clock", "date"})


def stems(text: str) -> frozenset[str]:
    """Words of four letters or more, cut to six, so that authorised and authorisation, or processed and processing,
    meet; used only to rank where a token was found, never to decide whether it was."""
    words = NAME_WORD.findall(text.lower().replace("_", " "))
    return frozenset(word[:6] for word in words if word not in NAME_STOP_WORDS)


def _hits_in(stacks: Sequence[Haystack], finder, token: Token, place: str, sentence: frozenset[str]) -> list[Hit]:
    """Each stack's hits; an item's block as received is listed only when nothing in the raw item matched."""
    found: list[Hit] = []
    for stack in stacks:
        hits = finder(token, stack)
        raw = [hit for hit in hits if not hit.startswith(AS_RECEIVED)]
        for detail in dict.fromkeys(raw or hits):
            name = detail.split(" = ", 1)[0].replace(AS_RECEIVED, "").replace("(number in the text)", "")
            found.append(Hit(stack.label, detail, place, len(stems(f"{stack.label} {name}") & sentence)))
    return sorted(found, key=lambda hit: -hit.relevance)


def locate(token: Token, sentence: frozenset[str], cited: Sequence[Haystack], uncited: Sequence[Haystack],
           card: Haystack, instructions: str | None) -> TokenResult:
    """The first place ``token`` occurs, in PLACES order: direct matches anywhere before loose ones."""
    direct = {place: _hits_in(stacks, direct_hits, token, place, sentence)
              for place, stacks in (("cited", cited), (UNCITED, uncited), (INCIDENT_CARD, [card]))}
    every = [hit for hits in direct.values() for hit in hits]  # cited first
    by_name = token.kind in NUMERIC_KINDS  # a verbatim ID or quoted span needs no ranking by name
    ranked = tuple(sorted(every, key=lambda hit: -hit.relevance) if by_name else every)
    for place, hits in direct.items():
        if hits:
            by_value_only = place == "cited" and by_name and all(hit.relevance == 0 for hit in direct["cited"])
            better = [hit for hit in direct[UNCITED] if hit.relevance >= 2]  # already best-named first
            outranked = better[0] if by_value_only and better else None
            return TokenResult(token, place, tuple(map(str, hits)), ranked, outranked)
    if instructions is not None and token.kind == "quoted" and bounded(token.text).search(instructions):
        return TokenResult(token, "instructions", ("the system prompt",), (), None)
    for place, stacks in (("cited, loosely", cited), ("uncited, loosely", uncited)):
        hits = _hits_in(stacks, loose_hits, token, place, sentence)
        if hits:
            return TokenResult(token, place, tuple(map(str, hits)), tuple(hits), None)
    return TokenResult(token, "nowhere", (), (), None)


def cited_ids(output: Mapping[str, object]) -> tuple[list[str], list[str]]:
    return ([str(i) for i in output.get("supporting_evidence_ids", [])],
            [str(i) for i in output.get("contradicting_evidence_ids", [])])


def contract_failures(diagnosis: StoredDiagnosis, by_id: Mapping[str, Mapping], cutoff: int) -> dict[str, list[str]]:
    """H1 to H5: the packet's integrity, and the cited IDs against the packet and its cutoff."""
    supporting, contradicting = cited_ids(diagnosis.output or {})
    cited = [*supporting, *contradicting]
    actual = canonical_sha256(diagnosis.packet)
    repeats = [f"{name} repeats {sorted({i for i in ids if ids.count(i) > 1})}"
               for name, ids in (("supporting", supporting), ("contradicting", contradicting)) if len(set(ids)) < len(ids)]
    return {
        "H1": [f"{where} records {recorded[:12]}..., the stored packet hashes to {actual[:12]}..."
               for where, recorded in diagnosis.recorded_hashes if recorded != actual],
        "H2": [f"cites {i}, which is not in the packet" for i in cited if i not in by_id],
        "H3": repeats + [f"{i} is cited as both supporting and contradicting"
                         for i in sorted(set(supporting) & set(contradicting))],
        "H4": [f"{i} known_at_ms {by_id[i]['known_at_ms']} is after the cutoff {cutoff}"
               for i in cited if i in by_id and int(by_id[i]["known_at_ms"]) > cutoff],
        "H5": [f"{i} {path} = {value} is after the cutoff {cutoff}" for i in dict.fromkeys(cited) if i in by_id
               for path, value in epoch_leaves(by_id[i].get("facts", {}), "facts") if value > cutoff],
    }


def prompt_failures(prompt: str, items: Sequence[Mapping], cutoff: int) -> tuple[list[str], dict[str, str], str]:
    """H6, with the item blocks and the incident-card part of the prompt for the soft checks."""
    blocks, header, problem = prompt_blocks(prompt, items)
    failures = [problem] if problem else []
    cutoff_text = f"evidence cutoff {utc_text(cutoff)}"
    if cutoff_text not in header:
        failures.append(f"the prompt's incident card does not state {cutoff_text!r}")
    return failures, blocks, header


def named_id_failures(tokens: Sequence[Token], by_id: Mapping[str, Mapping], candidates: set[str]) -> dict[str, list[str]]:
    """H7 and H8: IDs the rationale names that the packet does not hold."""
    return {
        "H7": [f"sentence {t.sentence} names {t.text}, which is not in the packet" for t in tokens
               if t.kind == "evidence_id" and t.text not in by_id],
        "H8": [f"sentence {t.sentence} names {t.text}, which is not a candidate change" for t in tokens
               if t.kind == "deployment_id" and t.text not in candidates],
    }


def fixture_checks(output: Mapping[str, object], by_id: Mapping[str, Mapping]) -> tuple[dict, dict]:
    """H9 and S9, for a reproduced_in_fixture claim; empty when the claim is not made."""
    if output.get("evidence_level") != "reproduced_in_fixture":
        return {}, {}
    supporting, _ = cited_ids(output)
    fixtures = [i for i in supporting if i in by_id and by_id[i]["kind"] == "fixture_check"]
    failing = [i for i in fixtures if any(isinstance(result, dict) and result.get("passed") is False
                                          for result in by_id[i].get("facts", {}).get("results", []))]
    return ({"H9": [] if fixtures else ["reproduced_in_fixture without a cited fixture_check item"]},
            {"S9": [] if failing else [f"the cited fixture item(s) {fixtures} record no failing test"]})


def token_flag(result: TokenResult, cited: Sequence[str], by_id: Mapping[str, Mapping]) -> str | None:
    token = result.token
    if token.kind == "evidence_id":
        named_not_cited = token.text in by_id and token.text not in cited
        return f"sentence {token.sentence} names {token.text} but does not cite it" if named_not_cited else None
    if result.place == "cited":
        if result.outranked_by is None:
            return None
        return (f"sentence {token.sentence}: {token.kind} {token.text!r} matches cited items by value only "
                f"({result.where[0]}); the best match by name is in an uncited item ({result.outranked_by})")
    shown = "; ".join(result.where[:2]) + (f"; and {len(result.where) - 2} more" if len(result.where) > 2 else "")
    where = "nowhere in the packet" if result.place == "nowhere" else f"in no cited item as stated; {result.place}"
    return f"sentence {token.sentence}: {token.kind} {token.text!r} is {where}" + (f" ({shown})" if shown else "")


def answer_flags(output: Mapping[str, object], by_id: Mapping[str, Mapping], cited_stacks: Sequence[Haystack]) -> dict:
    """S2, S3 and S8: whether the answer's named change and method, and any support at all, are cited."""
    supporting, _ = cited_ids(output)
    flags: dict[str, list[str]] = {"S8": [] if supporting else ["cites no supporting item"]}
    change = str(output.get("faulting_change_id", "none"))
    if change not in CHOICES_WITHOUT_TARGET:
        has_item = any(by_id.get(i, {}).get("kind") == "deployment" and by_id[i].get("facts", {}).get("deploy_id") == change
                       for i in supporting)
        elsewhere = " (the ID does occur in a cited item)" if any(string_hits(s, change) for s in cited_stacks) else ""
        flags["S2"] = [] if has_item else [f"names {change} but cites no deployment item for it{elsewhere}"]
    method = str(output.get("affected_method", "none"))
    if method not in CHOICES_WITHOUT_TARGET:
        has_item = any(by_id.get(i, {}).get("kind") == "metric" and by_id[i].get("facts", {}).get("method") == method
                       for i in supporting)
        flags["S3"] = [] if has_item else [f"names method {method} but cites no metric item for it"]
    return flags


def token_place(token: Token, sentence: frozenset[str], cited: Sequence[str], by_id: Mapping[str, Mapping],
                stacks: tuple, instructions: str | None) -> TokenResult:
    """Evidence IDs are placed by membership (the ID itself is the citation); everything else by where it occurs."""
    if token.kind == "evidence_id":
        if token.text in cited:
            return TokenResult(token, "cited", (token.text,), (), None)
        if token.text in by_id:
            return TokenResult(token, UNCITED, (token.text,), (), None)
        return TokenResult(token, "nowhere", (), (), None)
    cited_stacks, uncited_stacks, card = stacks
    return locate(token, sentence, cited_stacks, uncited_stacks, card, instructions)


def check_diagnosis(diagnosis: StoredDiagnosis) -> DiagnosisResult:
    output = diagnosis.output or {}
    items = list(diagnosis.packet["items"])
    by_id = {str(item["evidence_id"]): item for item in items}
    cutoff = int(diagnosis.packet["incident"]["evidence_cutoff_ms"])
    hard = contract_failures(diagnosis, by_id, cutoff)
    blocks: dict[str, str] = {}
    header: str | None = None
    if diagnosis.prompt is not None:
        hard["H6"], blocks, header = prompt_failures(diagnosis.prompt, items, cutoff)
    rationale = str(output.get("rationale", ""))
    tokens = rationale_tokens(rationale)
    sentence_stems = {n: stems(sentence) for n, sentence in enumerate(split_sentences(rationale), start=1)}
    hard.update(named_id_failures(tokens, by_id, {str(c) for c in diagnosis.packet.get("candidate_change_ids", [])}))
    fixture_hard, soft = fixture_checks(output, by_id)
    hard.update(fixture_hard)

    supporting, contradicting = cited_ids(output)
    cited = list(dict.fromkeys([*supporting, *contradicting]))
    haystacks = {i: item_haystack(item, blocks.get(i)) for i, item in by_id.items()}
    cited_stacks = [haystacks[i] for i in cited if i in haystacks]
    stacks = (cited_stacks, [s for i, s in haystacks.items() if i not in cited], incident_haystack(diagnosis.packet, header))
    results = tuple(token_place(t, sentence_stems[t.sentence], cited, by_id, stacks, diagnosis.system_prompt)
                    for t in tokens)
    for result in results:
        flag = token_flag(result, cited, by_id)
        soft.setdefault(TOKEN_CHECK[result.token.kind], []).extend([flag] if flag else [])
    soft.update(answer_flags(output, by_id, cited_stacks))
    card_text = header.removesuffix(EVIDENCE_ITEMS_LINE).rstrip("\n") if header is not None else None
    return DiagnosisResult(diagnosis, hard, soft, results, blocks, card_text)


def packet_problems(packet: Mapping[str, object]) -> dict[str, list[str]]:
    cutoff = int(packet["incident"]["evidence_cutoff_ms"])
    return {
        "P1": [f"{item['evidence_id']} known_at_ms {item['known_at_ms']} is after the cutoff {cutoff}"
               for item in packet["items"] if int(item["known_at_ms"]) > cutoff],
        "P2": [f"{item['evidence_id']} {path} = {value} is after the cutoff {cutoff}" for item in packet["items"]
               for path, value in epoch_leaves(item.get("facts", {}), "facts") if value > cutoff],
    }


# --------------------------------------------------------------------------- the spot-check sample


def spot_check_sample(root: Path) -> list[dict]:
    """summarize.build_spot_check's draw, re-stated: 10 validated model answers of test_frozen, seed 20261112."""
    traces = read_jsonl(root / "evaluation" / "traces" / f"{SPOT_CHECK_LABEL}.jsonl")
    validated = [trace for trace in traces if trace["answers"].get("model", {}).get("status") == "validated"]
    draw = random.Random(SPOT_CHECK_SEED)
    return sorted(draw.sample(validated, min(SPOT_CHECK_COUNT, len(validated))), key=lambda trace: trace["run_id"])  # NOSONAR(S2245) reproduces a seeded evaluation sample; not a security use


def committed_sheet_runs(root: Path) -> list[str]:
    sheet = root / "evaluation" / "citation_spot_check.md"
    return re.findall(r"^## (?:\d+\. )?(run_[0-9a-f]+)", sheet.read_text(encoding="utf-8"), flags=re.MULTILINE)


# --------------------------------------------------------------------------- the report


def system_group(system: str) -> str:
    return "model" if system == "model" else "rules"


def scope_lines(diagnoses: Sequence[StoredDiagnosis], results: Sequence[DiagnosisResult]) -> list[str]:
    lines = ["## Scope", "", "| Source | System | Stored answers | Checked (validated) | needs_review (nothing to check) |",
             "|---|---|---:|---:|---:|"]
    statuses = Counter((d.source, d.system, d.status) for d in diagnoses)
    for source, system in dict.fromkeys((d.source, d.system) for d in diagnoses):
        stored = sum(n for (s, y, _), n in statuses.items() if (s, y) == (source, system))
        checked = statuses[(source, system, "validated")]
        lines.append(f"| `{source}` | {system} | {stored} | {checked} | {stored - checked} |")
    other = Counter(d.status for d in diagnoses if d.status not in {"validated", "needs_review"})
    packets = {canonical_sha256(d.packet) for d in diagnoses}
    model = sum(1 for r in results if r.diagnosis.system == "model")
    with_prompt = sum(1 for r in results if r.diagnosis.prompt is not None)
    lines += ["", f"Total: {len(diagnoses)} stored answers; {len(results)} validated diagnoses checked ({model} model, "
              f"{len(results) - model} rules); {len(packets)} distinct packets; {with_prompt} diagnoses stored with the "
              "prompt the model received." + (f" Other statuses: {dict(other)}." if other else "")]
    return lines


def hard_lines(diagnoses: Sequence[StoredDiagnosis], results: Sequence[DiagnosisResult]) -> list[str]:
    table = "|---|---|---:|---:|---:|"
    lines = ["## Hard checks", "", "| Check | What it tests | Checked | Pass | Fail |", table]
    for check, description in HARD_CHECKS.items():
        applicable = [r for r in results if check in r.hard]
        failed = sum(1 for r in applicable if r.hard[check])
        lines.append(f"| {check} | {description} | {len(applicable)} | {len(applicable) - failed} | {failed} |")
    packets = {canonical_sha256(d.packet): packet_problems(d.packet) for d in diagnoses}
    lines += ["", "| Check | What it tests | Packets | Pass | Fail |", table]
    for check, description in PACKET_CHECKS.items():
        failed = sum(1 for problems in packets.values() if problems[check])
        lines.append(f"| {check} | {description} | {len(packets)} | {len(packets) - failed} | {failed} |")
    failures = [f"- {check} `{r.diagnosis.key}`: {detail}" for r in results
                for check, details in r.hard.items() for detail in details]
    failures += [f"- {check} packet {digest[:12]}...: {detail}" for digest, problems in packets.items()
                 for check, details in problems.items() for detail in details]
    return [*lines, "", "### Every hard failure", "", *(failures or ["None."])]


def soft_lines(results: Sequence[DiagnosisResult]) -> list[str]:
    lines = ["## Soft checks: pointers for the person judging, not verdicts", "",
             "Diagnoses flagged, per check (once per check, however many flags):", "",
             "| Check | What it looks for | Model: applicable | Model: flagged | Rules: applicable | Rules: flagged |",
             "|---|---|---:|---:|---:|---:|"]
    for check, description in SOFT_CHECKS.items():
        cells = []
        for group in ("model", "rules"):
            applicable = [r for r in results if system_group(r.diagnosis.system) == group and check in r.soft]
            cells += [str(len(applicable)), str(sum(1 for r in applicable if r.soft[check]))]
        lines.append(f"| {check} | {description} | " + " | ".join(cells) + " |")
    lines += ["", "Where each stated token occurs. The first place in this order wins: directly in a cited item, an "
              "uncited item, the incident card or the system prompt; then loosely in a cited or an uncited item; "
              "else nowhere. An evidence ID is placed by membership: cited, in the packet but uncited, or not in it.",
              "", "| Check | System | Tokens | " + " | ".join(PLACES) + " |", "|---|---|---:|" + "---:|" * len(PLACES)]
    for check in ("S1", "S4", "S5", "S6", "S7"):
        for system in dict.fromkeys(r.diagnosis.system for r in results):
            places = Counter(t.place for r in results if r.diagnosis.system == system for t in r.tokens
                             if TOKEN_CHECK[t.token.kind] == check)
            if places:
                lines.append(f"| {check} | {system} | {sum(places.values())} | "
                             + " | ".join(str(places[p]) for p in PLACES) + " |")
    lines += ["", "Matching rules. Direct: IDs and quoted spans verbatim between identifier boundaries, in the item's "
              "evidence ID, title, text, fact keys and string values, and the block the model received when it is "
              "stored. A number equals a fact or a number written in a text; with decimals it also matches a value "
              "that rounds to it at its stated precision; a percentage also matches a ratio from 0 to 1 at its stated "
              "precision. A clock time or date matches verbatim or a timestamp's UTC value. Loose, tried only when "
              "nothing matches directly: a span after lower-casing and treating spaces, underscores and hyphens alike; "
              "a number as the difference of two figures, or the total of one map, inside one item; a percentage as "
              "the difference of two ratios, in points. A match on a small integer, or a loose match, is weak "
              "evidence: 0, 1 and 2 occur in most items, and differences coincide.", ""]
    return lines


def flag_lines(results: Sequence[DiagnosisResult]) -> list[str]:
    lines = ["### Soft flags per source", "", "| Source | System | Checked | " + " | ".join(SOFT_CHECKS) + " |",
             "|---|---|---:|" + "---:|" * len(SOFT_CHECKS)]
    for source, system in dict.fromkeys((r.diagnosis.source, r.diagnosis.system) for r in results):
        group = [r for r in results if (r.diagnosis.source, r.diagnosis.system) == (source, system)]
        counts = [str(sum(1 for r in group if r.soft.get(check))) for check in SOFT_CHECKS]
        lines.append(f"| `{source}` | {system} | {len(group)} | " + " | ".join(counts) + " |")
    lines += ["", "### Every soft flag, per diagnosis", ""]
    flagged = [r for r in results if any(r.soft.values())]
    for r in flagged:
        lines.append(f"- `{r.diagnosis.key}`")
        lines += [f"  - {check}: {flag}" for check, flags in sorted(r.soft.items()) for flag in flags]
    return lines if flagged else [*lines, "None."]


def sample_lines(root: Path) -> list[str]:
    sample = [trace["run_id"] for trace in spot_check_sample(root)]
    committed = committed_sheet_runs(root)
    return ["## The spot-check sample, reproduced", "",
            f"`random.Random({SPOT_CHECK_SEED}).sample(validated, {SPOT_CHECK_COUNT})` over the `{SPOT_CHECK_LABEL}` "
            f"traces in file order, sorted by run ID, gives: {', '.join(sample)}.", "",
            f"The committed sheet lists: {', '.join(committed)}. Same runs, same order: "
            f"**{'yes' if sample == committed else 'no'}**."]


def markdown_report(root: Path, diagnoses: Sequence[StoredDiagnosis], results: Sequence[DiagnosisResult],
                    source_label: str) -> str:
    lines = ["# Citation mechanical check", "",
             f"Generated by `citation_mechanical_check.py` over {source_label}. Standard library only: no model, no "
             "network, and no import of the code it checks. It reads committed files and writes nothing.", "",
             "A hard check fails when the stored data, or an ID the rationale names, contradicts the packet. A soft "
             "check only reports where a rationale's numbers, quoted terms, clock times and IDs occur. Neither decides "
             "whether evidence supports a claim; EVALUATION.md §9 leaves that to a person.", "",
             *scope_lines(diagnoses, results), "", *hard_lines(diagnoses, results), "", *soft_lines(results),
             *flag_lines(results), "", *sample_lines(root)]
    return "\n".join(lines) + "\n"


def json_report(results: Sequence[DiagnosisResult]) -> str:
    rows = [{
        "source": r.diagnosis.source, "case_id": r.diagnosis.case_id, "system": r.diagnosis.system,
        "prompt_stored": r.diagnosis.prompt is not None, "hard": r.hard, "soft": r.soft,
        "tokens": [{"kind": t.token.kind, "text": t.token.text, "sentence": t.token.sentence, "place": t.place,
                    "where": list(t.where[:5]), "more": max(0, len(t.where) - 5)} for t in r.tokens],
    } for r in results]
    return json.dumps(rows, indent=1, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- the spot-check sheet


def fenced(text: str) -> list[str]:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", text, fence]


def tagged(hits: Sequence[Hit], limit: int = 3) -> str:
    """Hits with whether their item is cited, best-named first."""
    names = {"cited": "cited", UNCITED: "not cited", INCIDENT_CARD: "incident card, not an item"}
    shown = [f"{hit} ({names.get(hit.place, hit.place)})" for hit in hits[:limit]]
    return "; ".join(shown) + (f"; and {len(hits) - limit} more" if len(hits) > limit else "")


def pointer(t: TokenResult) -> str:
    if t.token.kind == "evidence_id":
        return {"cited": "cited", UNCITED: "**in the packet but not cited**", "nowhere": "**not in the packet**"}[t.place]
    if t.place == "cited" and t.outranked_by is not None:
        return (f"**in a cited item by value only**; the best match by name is not cited: {t.outranked_by}. "
                f"All matches: {tagged(t.ranked)}")
    if t.place == "cited":
        return f"in a cited item: {tagged(t.ranked)}"
    if t.place in {UNCITED, INCIDENT_CARD}:
        return f"**not in a cited item**: {tagged(t.ranked)}"
    if t.place == "instructions":
        return "**not in the evidence**: a term from the instructions the model received"
    if t.place in LOOSE_PLACES:
        return f"**not in any item as stated**; a loose match ({t.place}): {tagged(t.ranked)}"
    return "**nowhere in the packet**"


def pointer_lines(result: DiagnosisResult) -> list[str]:
    lines = [f"- sentence {t.token.sentence}, {t.token.kind.replace('_', ' ')} `{t.token.text}`: {pointer(t)}"
             for t in result.tokens]
    return lines or ["- (the claim states no number, quoted term, clock time or ID)"]


def evidence_lines(result: DiagnosisResult, supporting: Sequence[str], contradicting: Sequence[str]) -> list[str]:
    lines = []
    for role, ids in (("supporting", supporting), ("contradicting", contradicting)):
        for evidence_id in ids:
            lines += [f"**{evidence_id}**, cited as {role}, exactly as the model received it:", "",
                      *fenced(result.blocks.get(evidence_id, "(not found in the stored prompt)")), ""]
    return lines


def verdict_lines(case_id: str, kinds: Mapping[str, str], supporting: Sequence[str], contradicting: Sequence[str]) -> list[str]:
    lines = [f"**Verdicts for {case_id}:**", "",
             "| Cited as supporting | Kind | Supports the claim? (yes / partly / no / against) | Sentence(s) | Note |",
             "|---|---|---|---|---|", *[f"| {i} | {kinds.get(i, '?')} |  |  |  |" for i in supporting]]
    if contradicting:
        lines += ["", "| Cited as contradicting | Kind | Argues against the hypothesis, or for another? (yes / partly / no) "
                  "| Note |", "|---|---|---|---|", *[f"| {i} | {kinds.get(i, '?')} |  |  |" for i in contradicting]]
    return [*lines, "", "Sentences of the claim that no cited item supports (numbers, or none): ", ""]


def sheet_section(index: int, result: DiagnosisResult) -> list[str]:
    diagnosis, output = result.diagnosis, result.diagnosis.output or {}
    supporting, contradicting = cited_ids(output)
    kinds = {str(item["evidence_id"]): str(item["kind"]) for item in diagnosis.packet["items"]}
    lines = [f"## {index}. {diagnosis.case_id}", "",
             f"**The answer:** hypothesis `{output.get('hypothesis')}`; faulting change "
             f"`{output.get('faulting_change_id')}`; method `{output.get('affected_method')}`; operation "
             f"`{output.get('proposed_operation')}`; evidence level `{output.get('evidence_level')}`. It cites "
             f"{len(supporting)} supporting and {len(contradicting)} contradicting items of the packet's {len(kinds)}.",
             "", "**The claim** (the rationale, verbatim, split at sentence ends):", ""]
    lines += [f"{n}. {sentence}" for n, sentence in enumerate(split_sentences(str(output.get("rationale", ""))), start=1)]
    lines += ["", "**Pointers** (string matching only, not a verdict):", "", *pointer_lines(result), "",
              "**The incident card**, not a citable item, exactly as the model received it:", "",
              *fenced(result.incident_card or "(not found in the stored prompt)"), "",
              *evidence_lines(result, supporting, contradicting),
              *verdict_lines(diagnosis.case_id, kinds, supporting, contradicting), "---", ""]
    return lines


SHEET_INTRO = """# Citation spot check (owner to fill in)

EVALUATION.md §9 asks a person to check 10 diagnoses: does the evidence each one cites support what it claims? §9 \
forbids grading this with another model, so every verdict cell below is empty, for you.

## The sample

The original sample, kept: 10 of the 30 `test_frozen` model diagnoses (qwen3:8b, thinking off, prompt diag-p2, \
validator val-4; all 30 validated), drawn by `random.Random(20261112).sample(...)` over the traces in file order and \
sorted by run ID (`SPOT_CHECK_SEED` in `src/recovery_workbench/evaluation/summarize.py`). The rule was committed with \
the first sheet in the root commit `833264a`, before anyone judged a diagnosis. It was not written down before the \
frozen test ran: that commit holds both, so git cannot show which came first. The mechanical check re-draws the sample \
on its own and gets the same 10 runs in the same order.

## How to judge (rubric)

For each diagnosis, read the claim, then each cited item exactly as the model received it. The incident card the model \
also received is shown too, because claims refer to it, but it is not an item and cannot be cited. (The instructions \
the model received are not repeated.)

For each item **cited as supporting**, write one word:

- **yes**: the item states something a sentence of the claim asserts, or something the hypothesis needs.
- **partly**: the item bears on the claim but backs only part of it, or only through a calculation or inference the \
claim does not show.
- **no**: the item does not bear on anything the claim asserts.
- **against**: the item contradicts what it is cited for.

For each item **cited as contradicting** (the model says it argues against its own hypothesis or for another one), \
write yes, partly or no: does it?

Then name any sentence of the claim that no cited item supports, or write none.

Judge support, not correctness: a wrong diagnosis can cite good support, and a right one can cite irrelevant items. \
Judge only from the text shown, which is what the model had. The answer key is at the end; read it after judging.

**Pointers** under each claim come from string matching. For each number, quoted term, clock time or ID in the claim \
they list where it occurs, best match by name first, and say whether that item is cited. They are not verdicts: a \
match can be a coincidence (0, 1 and 2 occur almost everywhere), a loose match is weaker still, and a derived figure \
may match nothing.

## Tally (fill in last)

| # | Run | Supporting cited | yes | partly | no | against | Contradicting cited | of them argue against | Unsupported sentences |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---|
"""


def spot_check_sheet(root: Path, results_by_key: Mapping[str, DiagnosisResult]) -> str:
    chosen = [results_by_key[f"evaluation/traces/{SPOT_CHECK_LABEL} {trace['run_id']} model"]
              for trace in spot_check_sample(root)]
    lines = [SHEET_INTRO.rstrip("\n")]
    for index, result in enumerate(chosen, start=1):
        supporting, contradicting = cited_ids(result.diagnosis.output or {})
        lines.append(f"| {index} | {result.diagnosis.case_id} | {len(supporting)} |  |  |  |  | {len(contradicting)} |  |  |")
    lines += ["", "---", ""]
    for index, result in enumerate(chosen, start=1):
        lines += sheet_section(index, result)
    cases = {case["run_id"]: case for case in read_json(root / "evaluation" / "results.json")["cases"]}
    lines += ["## Answer key (read after judging)", "",
              "From `evaluation/results.json`: each run's family and variant, the oracle's answer, and whether this "
              "diagnosis was scored outcome-correct.", "",
              "| Run | Family and variant | Expected hypothesis | Expected change | Expected method | Outcome correct |",
              "|---|---|---|---|---|---|"]
    for result in chosen:
        case = cases[result.diagnosis.case_id]
        expected = case["expected"]
        lines.append(f"| {case['run_id']} | {case['family']} {case['variant']} | {expected['hypothesis']} | "
                     f"{expected['faulting_change_id']} | {expected['affected_method']} | "
                     f"{'yes' if case['model']['score']['outcome_correct'] else 'no'} |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- entry point


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="citation_mechanical_check.py")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--json", action="store_true", help="print every per-diagnosis result as JSON")
    mode.add_argument("--spot-check-sheet", action="store_true", help="print the owner's spot-check sheet")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the repository root (default: cwd)")
    parser.add_argument("--source-label", default="the repository copy in this image")
    args = parser.parse_args(argv)
    diagnoses = all_stored_diagnoses(args.root)
    results = [check_diagnosis(d) for d in diagnoses if d.status == "validated" and d.output is not None]
    if args.json:
        sys.stdout.write(json_report(results))
    elif args.spot_check_sheet:
        sys.stdout.write(spot_check_sheet(args.root, {r.diagnosis.key: r for r in results}))
    else:
        sys.stdout.write(markdown_report(args.root, diagnoses, results, args.source_label))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
