"""The model source a process diagnoses with, chosen once when it starts and kept for its whole life (owner answer to
Q46).

``WORKBENCH_MODEL_MODE`` names a source (local, recorded, rules or hosted) or asks for ``auto``, which takes the first
of these that can serve:

* local, when the local Ollama answers on its loopback address within ``AUTO_PROBE_TIMEOUT_S`` and lists the model;
* hosted, when the hosted URL, model and key are all set; the provider is not called to find out;
* rules otherwise.

The API server and each CLI command resolve once, as they start, and print the source they chose; the line never holds
the key. Afterwards nothing switches the source: a diagnosis request may still name another mode, as before, and a
failed model call leaves its diagnosis needing review instead of falling back to the rules.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import JsonValue

from recovery_workbench import diagnosis, report
from recovery_workbench.contracts import ModelMode
from recovery_workbench.settings import Settings

AUTO = "auto"
AUTO_PROBE_TIMEOUT_S = 2.0
PROBE_TEXT_CHARS = 160
# The local probe gives the model's digest or raises diagnosis.ModelCallError; api.ApiConfig.model_probe has its shape.
LocalProbe = Callable[[], str]


@dataclass(frozen=True)
class ModelSource:
    mode: ModelMode  # never auto
    model_id: str | None  # None for the rules baseline
    provider_host: str | None  # the hosted model's host; None for every other source
    chosen_by: str  # the setting, or what auto found, for the start line

    @property
    def label(self) -> str:
        """How the page and the start line name the source."""
        if self.mode is ModelMode.HOSTED:
            return f"hosted model {self.model_id} via {self.provider_host}, not part of the M4 evaluation"
        if self.mode is ModelMode.LOCAL:
            return f"local model {self.model_id}"
        if self.mode is ModelMode.RECORDED:
            return f"recorded replays of local model {self.model_id} output (not live inference)"
        return "rules baseline (no model)"

    def document(self) -> dict[str, JsonValue]:
        """The source as GET /api/scenarios and /health give it; how it was chosen stays on the console."""
        return {"mode": self.mode.value, "model_id": self.model_id, "provider_host": self.provider_host,
                "label": self.label}

    def start_line(self) -> str:
        return f"Model source: {self.label}; {self.chosen_by}"


def resolve(configured: Settings, *, local_probe: LocalProbe | None = None) -> ModelSource:
    """The source ``configured`` names; for auto, the first that can serve (module docstring). Only auto probes."""
    if configured.model_mode != AUTO:
        named = ModelMode(configured.model_mode)
        return _source(configured, named, f"set by WORKBENCH_MODEL_MODE={named.value}")
    probe = local_probe if local_probe is not None else (lambda: _ollama_digest(configured))
    try:
        probe()
    except diagnosis.ModelCallError as unavailable:
        # The probe's message can quote whatever answered on the model port, so it is cut short and escaped.
        shown = " ".join(str(unavailable).split())[:PROBE_TEXT_CHARS]
        no_local = f"auto: no local model at {configured.ollama_url} ({report.visible(shown)})"
    else:
        return _source(configured, ModelMode.LOCAL, f"auto: the local model answered at {configured.ollama_url}")
    if configured.hosted.configured:
        return _source(configured, ModelMode.HOSTED, f"{no_local}; the hosted settings are complete")
    missing = ", ".join(configured.hosted.missing())
    return _source(configured, ModelMode.RULES, f"{no_local}; hosted settings incomplete ({missing} not set)")


def settled(configured: Settings, source: ModelSource) -> Settings:
    """``configured`` with auto replaced by the source it resolved to, so every later reader sees a stored mode."""
    return dataclasses.replace(configured, model_mode=source.mode.value)


def _source(configured: Settings, mode: ModelMode, chosen_by: str) -> ModelSource:
    if mode is ModelMode.HOSTED:
        return ModelSource(mode, configured.hosted.model, configured.hosted.host, chosen_by)
    if mode is ModelMode.RULES:
        return ModelSource(mode, None, None, chosen_by)
    return ModelSource(mode, configured.model, None, chosen_by)


def _ollama_digest(configured: Settings) -> str:
    client = diagnosis.OllamaClient(base_url=configured.ollama_url, model=configured.model,
                                    timeout_s=AUTO_PROBE_TIMEOUT_S)
    try:
        return client.model_digest()
    finally:
        client.close()
