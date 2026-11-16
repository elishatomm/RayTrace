"""What an evaluation ran on: hardware, OS, Python, packages, Ollama, model and every version string."""
from __future__ import annotations

import platform
import subprocess
import sys
from collections.abc import Mapping
from importlib import metadata

from pydantic import JsonValue

from recovery_workbench import __version__, diagnosis, packet, rules, settings
from recovery_workbench.simulator import families
from recovery_workbench.simulator.world import GENERATOR_VERSION

_PACKAGES = ("fastapi", "uvicorn", "pydantic", "httpx", "pytest")


def _sysctl(name: str) -> str | None:
    """A read-only macOS hardware query; None where sysctl does not exist."""
    try:
        completed = subprocess.run(["sysctl", "-n", name], capture_output=True, text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def describe_environment(
    client: diagnosis.OllamaClient | None,
    *,
    think: bool | None,
    deadline_s: float | None,
    systems: tuple[str, ...],
) -> dict[str, JsonValue]:
    config = settings.load_settings()
    memory = _sysctl("hw.memsize")
    model: dict[str, JsonValue] | None = None
    if client is not None:
        options: Mapping[str, JsonValue] = client.options
        model = {"model": client.model, "model_digest": client.model_digest(), "ollama_version": client.version(),
                 "think": think, "options": dict(options), "deadline_s": deadline_s, "max_repairs": 1,
                 "prompt_version": diagnosis.PROMPT_VERSION, "keep_alive": client.keep_alive}
    manifest = families.load_manifest()
    return {
        "systems": list(systems),
        "hardware": {"machine": platform.machine(), "model": _sysctl("hw.model"),
                     "cpu": _sysctl("machdep.cpu.brand_string"),
                     "memory_gb": round(int(memory) / 2**30, 1) if memory and memory.isdigit() else None},
        "os": {"platform": platform.platform(), "release": platform.release()},
        "python": sys.version.split()[0],
        "packages": {name: metadata.version(name) for name in _PACKAGES},
        "model": model,
        "versions": {
            "workbench": __version__, "packet": packet.PACKET_VERSION, "validator": diagnosis.VALIDATOR_VERSION,
            "rules": rules.RULES_VERSION, "world_generator": GENERATOR_VERSION,
            "evaluation_manifest": manifest.manifest_version, "family_generator": manifest.generator_version,
        },
        "policy": {"stale_after_seconds": config.stale_after_seconds, "plan_ttl_seconds": config.plan_ttl_seconds,
                   "approval_ttl_seconds": config.approval_ttl_seconds},
    }
