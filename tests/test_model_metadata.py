"""A diagnosis's model ID and model digest are checked where they enter (security audit run 2, hardening note 7).

Both reach every stored diagnosis, the report, the UI and the receipt. The ID enters from the operator's settings
(``WORKBENCH_MODEL``) or whoever builds an ``OllamaClient``, the digest from the loopback model endpoint's model list,
and both from a recording file in recorded mode. Each entry refuses a value that is not a model name or a SHA-256
digest, and says so; none is dropped or replaced. Every model name and digest the frozen records hold passes both.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from pydantic import JsonValue, ValidationError
from test_diagnose_incident import FakeModel, answer, diagnose, the_packet
from test_tools import seed

from recovery_workbench import diagnosis, diagnosis_store, settings
from recovery_workbench.contracts import ContractError, DiagnosisStatus, ModelMode
from recovery_workbench.diagnosis import ModelCallError, OllamaClient
from recovery_workbench.simulator import driver

REPO_ROOT = Path(__file__).resolve().parents[1]
FROZEN_DIGEST = "500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41"  # qwen3:8b in the frozen evaluation
NAMES = ("qwen3:8b", "qwen3:4b", "fake-model:1b", "llama3.2", "hf.co/bartowski/Qwen3-8B-GGUF:Q4_K_M", "q" * 128)
NOT_NAMES = (
    "", "qwen3:8b\n", "qwen3:8b\x1b[2J", "qwen3 8b", "`qwen3:8b`", "qwen3|8b", "<b>qwen3</b>", "\u202eqwen3:8b",
    "-qwen3", ":8b", "q" * 129,
)
NOT_DIGESTS = (
    "", "abc123", "A" * 64, "a" * 63, "a" * 65, "g" * 64, f"{FROZEN_DIGEST}\n", f"sha256-{FROZEN_DIGEST}",
    f"SHA256:{FROZEN_DIGEST}", f"sha512:{FROZEN_DIGEST}", f"\x1b]52;c;QQ==\x07{FROZEN_DIGEST}",
)
TERMINAL_CONTROL = "\x1b[2J\x1b]52;c;QQ==\x07\x9b\u202e"


@pytest.fixture
def backlog(tmp_path: Path) -> Iterator[tuple[sqlite3.Connection, driver.SeedSummary]]:
    conn, summary = seed(tmp_path, "worker_backlog")
    yield conn, summary
    conn.close()


def listing(digest: str, seen: list[str] | None = None) -> httpx.MockTransport:
    """A loopback endpoint whose model list gives qwen3:4b (the client's default model) ``digest``."""
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:4b", "model": "qwen3:4b", "digest": digest}]})
        return httpx.Response(500, text="no model behind this endpoint")
    return httpx.MockTransport(handler)


# --------------------------------------------------------------------------- the model ID


@pytest.mark.parametrize("name", NAMES)
def test_a_model_name_passes_the_settings_and_the_client(name: str) -> None:
    assert settings.load_settings({"WORKBENCH_MODEL": name}).model == name
    assert OllamaClient(model=name).model == name


@pytest.mark.parametrize("name", NOT_NAMES)
def test_the_settings_refuse_a_model_id_that_is_not_a_model_name(name: str) -> None:
    with pytest.raises(ValueError, match="WORKBENCH_MODEL must be an Ollama model name") as refused:
        settings.load_settings({"WORKBENCH_MODEL": name})
    assert repr(name) in str(refused.value)  # escaped: no control character reaches the message as is


@pytest.mark.parametrize("name", NOT_NAMES)
def test_the_model_client_refuses_a_model_id_that_is_not_a_model_name(name: str) -> None:
    with pytest.raises(ContractError, match="not an Ollama model name"):
        OllamaClient(model=name)


# --------------------------------------------------------------------------- the digest from the model endpoint


@pytest.mark.parametrize("digest", [FROZEN_DIGEST, f"sha256:{FROZEN_DIGEST}"])
def test_the_model_client_passes_a_sha256_digest_with_or_without_its_prefix(digest: str) -> None:
    assert OllamaClient(transport=listing(digest)).model_digest() == digest


@pytest.mark.parametrize("digest", NOT_DIGESTS)
def test_the_model_client_refuses_a_digest_that_is_not_a_sha256_digest(digest: str) -> None:
    client = OllamaClient(transport=listing(digest))
    with pytest.raises(ModelCallError, match="which is not a SHA-256 digest") as refused:
        client.model_digest()
    assert repr(digest) in str(refused.value)


def test_a_malformed_digest_leaves_a_local_diagnosis_needing_review_before_any_model_call(backlog) -> None:
    conn, summary = backlog
    paths: list[str] = []
    stored = diagnose(conn, summary, ModelMode.LOCAL, client=OllamaClient(transport=listing(TERMINAL_CONTROL, paths)))
    assert (stored.status, stored.model_digest, stored.output) == (DiagnosisStatus.NEEDS_REVIEW, None, None)
    assert "which is not a SHA-256 digest" in stored.reasons[0]
    assert TERMINAL_CONTROL not in stored.reasons[0]
    assert paths == ["/api/tags"]  # refused on the model list: no chat call was made


# --------------------------------------------------------------------------- a recording file


@pytest.mark.parametrize(("field", "value"), [
    ("model_id", f"qwen3:8b{TERMINAL_CONTROL}"),
    ("model_digest", f"{FROZEN_DIGEST}{TERMINAL_CONTROL}"),
    ("model_digest", "abc123"),
])
def test_recorded_mode_refuses_a_recording_whose_model_id_or_digest_is_malformed(
    backlog, tmp_path: Path, field: str, value: str
) -> None:
    conn, summary = backlog
    recordings = tmp_path / "recordings"
    diagnose(conn, summary, ModelMode.LOCAL, client=FakeModel(answer(the_packet(conn, summary))), recordings=recordings)
    [path] = recordings.glob("*.json")
    path.write_text(json.dumps({**json.loads(path.read_text(encoding="utf-8")), field: value}), encoding="utf-8")
    other_dir = tmp_path / "fresh_copy"
    other_dir.mkdir()
    other_conn, other_summary = seed(other_dir, "worker_backlog")
    try:
        with pytest.raises(ValidationError, match=field):
            diagnose(other_conn, other_summary, ModelMode.RECORDED, recordings=recordings)
        assert diagnosis_store.list_diagnoses(other_conn, run_id=other_summary.run_id) == ()
    finally:
        other_conn.close()


# --------------------------------------------------------------------------- what the repository already holds


def _recorded_metadata(value: JsonValue, parent: str, names: set[str], digests: set[str]) -> None:
    """Collect every model name and digest under the keys runs, traces and reports store them in."""
    if isinstance(value, list):
        for item in value:
            _recorded_metadata(item, parent, names, digests)
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if isinstance(item, str) and key in ("model_id", "model") and parent != "hardware":  # hardware.model: a machine
            names.add(item)
        elif isinstance(item, str) and key in ("model_digest", "digest"):
            digests.add(item)
        else:
            _recorded_metadata(item, key, names, digests)


def test_every_model_name_and_digest_in_the_frozen_records_passes_both_checks() -> None:
    names: set[str] = set()
    digests: set[str] = set()
    for folder in ("evaluation", "experiments", "fixtures"):
        for path in sorted((REPO_ROOT / folder).rglob("*.json*")):
            text = path.read_text(encoding="utf-8")
            documents = text.splitlines() if path.suffix == ".jsonl" else [text]
            for document in (json.loads(line) for line in documents if line.strip()):
                _recorded_metadata(document, "", names, digests)
    assert names >= {"qwen3:8b", "qwen3:4b"}  # not vacuous: M0's two models and the frozen evaluation's
    assert digests >= {FROZEN_DIGEST}
    assert [name for name in sorted(names) if not settings.MODEL_NAME.fullmatch(name)] == []
    assert [digest for digest in sorted(digests) if not diagnosis.MODEL_DIGEST.fullmatch(digest)] == []
