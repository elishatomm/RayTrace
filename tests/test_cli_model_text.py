"""The CLI demo prints the model endpoint's text inert (owner decision Q43).

Whatever answers on the loopback model endpoint, Ollama or a program listening there while Ollama is stopped, writes
the rationale and reaches the diagnosis's reasons: a refusal quotes the name of a field the answer invented, and an
error reply lands in a reason, up to 300 characters of it. A terminal would act on control characters in that text,
and bidi or zero-width characters would reorder or hide it. The demo prints both with the receipt's escape,
``report.visible``, applied to the text as stored: a line break is escaped rather than collapsed, so each fact stays on
its own line and every other character prints as the endpoint sent it. The endpoint here is httpx's mock transport
behind the real ``OllamaClient``.
"""
from __future__ import annotations

import json
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from test_cli_flows import demo_lines, seeded_run_id
from test_diagnose_incident import answer, the_packet
from test_tools import seed

from recovery_workbench import cli, diagnosis, diagnosis_store, storage
from recovery_workbench.contracts import EvidencePacket
from recovery_workbench.diagnosis import OllamaClient

MODEL = "qwen3:8b"
DIGEST = "f" * 64
OPERATOR = "op_cli_tester"
# A CSI that clears the screen, an OSC 52 clipboard write ended by BEL, the 8-bit CSI U+009B, a right-to-left override
# and an invisible tag character from outside the BMP (the receipt test's set), then each as the demo must show it.
TERMINAL_CONTROL = "\x1b[2J\x1b]52;c;QQ==\x07\x9b\u202e\U000e0041"
SHOWN = r"\u001b[2J\u001b]52;c;QQ==\u0007\u009b\u202e\U000e0041"
# Every printable ASCII character, with runs of spaces: the demo printed it as stored and still does.
PLAIN_ASCII = "  " + "".join(map(chr, range(0x21, 0x7F))) + "  end  "
RATIONALE = "Worker failures follow a deployment. "  # the valid answer's rationale, before the endpoint's text
ERROR_REPLY = "model runner crashed: "  # the error reply's body, before the endpoint's text
WAYS = ("answer", "error")  # the endpoint's text arrives in its answers, or in an error reply


@dataclass(frozen=True)
class DemoRun:
    lines: list[str]  # every line the demo printed
    stored: diagnosis_store.StoredDiagnosis  # the diagnosis as the demo's database holds it


def answering_endpoint(*replies: dict[str, object]) -> httpx.MockTransport:
    """A loopback model endpoint that lists MODEL and answers the chat calls with ``replies``, in order."""
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": MODEL, "model": MODEL, "digest": DIGEST}]})
        if request.url.path == "/api/chat" and queue:
            message = {"role": "assistant", "content": json.dumps(queue.pop(0))}
            return httpx.Response(200, json={"model": MODEL, "message": message, "done_reason": "stop"})
        return httpx.Response(404, text=f"nothing answers {request.url.path} here")

    return httpx.MockTransport(handler)


def erroring_endpoint(body: str) -> httpx.MockTransport:
    """A loopback model endpoint that answers every request with HTTP 500 and ``body``."""
    return httpx.MockTransport(lambda request: httpx.Response(500, text=body))


def endpoint_with(text: str, way: str, built: EvidencePacket) -> httpx.MockTransport:
    if way == "error":
        return erroring_endpoint(ERROR_REPLY + text)
    # The first answer invents a field named ``text``, which the validator refuses by name; the repair is valid.
    return answering_endpoint({**answer(built), text: "an invented field"}, answer(built, rationale=RATIONALE + text))


def reasons_with(text: str, way: str) -> tuple[str, ...]:
    """The diagnosis's reasons when the endpoint sends ``text`` that way."""
    if way == "error":
        return (f"the model is unavailable: Ollama /api/tags returned HTTP 500: {ERROR_REPLY}{text}",)
    return (f"call 1: {text}: Extra inputs are not permitted",)


def lines_with(text: str, way: str) -> list[str]:
    """The demo's lines that carry the endpoint's text, with ``text`` where it appears."""
    if way == "error":
        return [f"    needs review because: {reason}" for reason in reasons_with(text, way)]
    return [f"    rationale: {RATIONALE}{text}",
            *(f"    rejected, then repaired: {reason}" for reason in reasons_with(text, way))]


def demo_against(directory: Path, endpoint: httpx.MockTransport) -> DemoRun:
    client = OllamaClient(model=MODEL, transport=endpoint)
    try:
        lines = demo_lines(directory, "worker_backlog", client=client, mode="local")
    finally:
        client.close()
    with closing(storage.connect(directory / cli.DATABASE_FILE)) as conn:
        [stored] = diagnosis_store.list_diagnoses(conn, run_id=seeded_run_id(lines))
    return DemoRun(lines, stored)


def control_or_format(text: str) -> list[str]:
    """Every Cc or Cf character in ``text`` except a line break: what a terminal would act on or hide."""
    return [char for char in text if unicodedata.category(char) in ("Cc", "Cf") and char != "\n"]


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> EvidencePacket:
    """The packet the demo's diagnosis reads: the demo seeds this same world (worker_backlog, seed 101) afresh."""
    conn, summary = seed(tmp_path_factory.mktemp("same_world"), "worker_backlog")
    try:
        return the_packet(conn, summary)
    finally:
        conn.close()


@pytest.fixture(scope="module", params=WAYS)
def hostile(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, built: EvidencePacket
) -> tuple[str, DemoRun]:
    way = request.param
    return way, demo_against(tmp_path_factory.mktemp(f"hostile_{way}"), endpoint_with(TERMINAL_CONTROL, way, built))


@pytest.fixture(scope="module", params=WAYS)
def plain(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory, built: EvidencePacket
) -> tuple[str, DemoRun]:
    way = request.param
    return way, demo_against(tmp_path_factory.mktemp(f"plain_{way}"), endpoint_with(PLAIN_ASCII, way, built))


def test_the_demo_prints_the_endpoints_text_with_each_control_or_format_character_escaped(
    hostile: tuple[str, DemoRun]
) -> None:
    way, run = hostile
    assert control_or_format("\n".join(run.lines)) == []
    for line in lines_with(SHOWN, way):
        assert line in run.lines


def test_the_demo_prints_plain_ascii_from_the_endpoint_exactly_as_before(plain: tuple[str, DemoRun]) -> None:
    way, run = plain
    for line in lines_with(PLAIN_ASCII, way):
        assert line in run.lines


def test_the_stored_diagnosis_keeps_the_endpoints_text_as_it_was_sent(hostile: tuple[str, DemoRun]) -> None:
    way, run = hostile
    assert run.stored.reasons == reasons_with(TERMINAL_CONTROL, way)
    if way == "answer":
        assert run.stored.output is not None
        assert run.stored.output.rationale == RATIONALE + TERMINAL_CONTROL


def printed(capsys: pytest.CaptureFixture[str], *argv: str) -> str:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    assert code == 0, captured.err
    return captured.out


def test_diagnose_and_escalate_already_print_the_endpoints_text_as_ascii_escapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Outside Q43's change because already inert: these commands print JSON, and json.dumps escapes every character
    outside printable ASCII. The escalation's summary quotes the reasons."""
    endpoint = erroring_endpoint(ERROR_REPLY + TERMINAL_CONTROL)
    monkeypatch.setattr(diagnosis, "OllamaClient", lambda **chosen: OllamaClient(**chosen, transport=endpoint))
    data = ["--data-dir", str(tmp_path)]
    run_id = json.loads(printed(capsys, "seed", "--scenario", "worker_backlog", "--seed", "101", *data))["run_id"]
    diagnosed = printed(capsys, "diagnose", "--run-id", run_id, "--mode", "local", *data)
    escalated = printed(capsys, "escalate", "--run-id", run_id, "--diagnosis-id", json.loads(diagnosed)["diagnosis_id"],
                        "--operator", OPERATOR, *data)
    for output in (diagnosed, escalated):
        assert [char for char in output if not " " <= char <= "~" and char != "\n"] == []
    [reason] = reasons_with(TERMINAL_CONTROL, "error")
    assert json.loads(diagnosed)["reasons"] == [reason]
    assert json.loads(escalated)["summary"] == f"The diagnosis needs review: {reason}"
