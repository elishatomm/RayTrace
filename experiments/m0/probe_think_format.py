"""Probe how the local Ollama combines ``think`` with ``format`` (a JSON schema) for the M0 model.

    uv run python experiments/m0/probe_think_format.py

Writes ``think_format_probe.json`` next to this file. Four live calls on a toy question with a
schema that uses ``enum``, ``maxItems`` and ``maxLength``; same options as the M0 run except a
small context. Answers the questions diagnosis.py depends on: does ``think`` default on when
omitted, is thinking returned separately, does the grammar constrain the content after thinking,
and does ``num_predict`` cap thinking and content together.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
from pydantic import JsonValue

from recovery_workbench.diagnosis import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, DEFAULT_OPTIONS

OUTPUT_PATH = Path(__file__).resolve().parent / "think_format_probe.json"
NOTE_MAX_CHARS = 60
SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "colour": {"type": "string", "enum": ["red", "green", "blue", "unknown"]},
        "ids": {"type": "array", "items": {"type": "string", "enum": ["ev_a", "ev_b", "ev_c"]}, "maxItems": 2},
        "note": {"type": "string", "maxLength": NOTE_MAX_CHARS},
    },
    "required": ["colour", "ids", "note"],
    "additionalProperties": False,
}
MESSAGES = [
    {"role": "system", "content": "Answer with one JSON object that matches the schema."},
    {
        "role": "user",
        "content": "Evidence ev_a: the sky is clear. ev_b: wet grass. ev_c: a stop sign at a junction. "
        "What colour is the sign? Cite up to two IDs and explain in a note of at least 200 characters.",
    },
]
CASES = (
    ("think omitted, num_predict 1500", None, 1500),
    ("think false, num_predict 1500", False, 1500),
    ("think true, num_predict 1500", True, 1500),
    ("think true, num_predict 6144", True, 6144),
)


def conforms(content: str) -> tuple[bool, str]:
    try:
        answer = json.loads(content)
    except json.JSONDecodeError as exc:
        return False, f"not JSON: {exc.msg}"
    if set(answer) != {"colour", "ids", "note"}:
        return False, f"keys {sorted(answer)}"
    if answer["colour"] not in SCHEMA["properties"]["colour"]["enum"]:
        return False, f"colour {answer['colour']!r} outside the enum"
    if len(answer["ids"]) > 2 or not set(answer["ids"]) <= {"ev_a", "ev_b", "ev_c"}:
        return False, f"ids {answer['ids']!r} break the array rules"
    if len(answer["note"]) > NOTE_MAX_CHARS:
        return False, f"note has {len(answer['note'])} chars"
    return True, f"note length {len(answer['note'])} (limit {NOTE_MAX_CHARS})"


def main() -> None:
    results = []
    with httpx.Client(base_url=DEFAULT_OLLAMA_URL, timeout=httpx.Timeout(300.0, connect=5.0)) as client:
        version = client.get("/api/version").json()["version"]
        for label, think, num_predict in CASES:
            body: dict[str, JsonValue] = {
                "model": DEFAULT_MODEL,
                "messages": MESSAGES,
                "format": SCHEMA,
                "stream": False,
                "options": {**DEFAULT_OPTIONS, "num_ctx": 8192, "num_predict": num_predict},
            }
            if think is not None:
                body["think"] = think
            started = time.monotonic()
            response = client.post("/api/chat", json=body)
            response.raise_for_status()
            payload = response.json()
            message = payload.get("message", {})
            content = message.get("content", "")
            valid, detail = conforms(content) if content.strip() else (False, "empty content")
            results.append(
                {
                    "case": label,
                    "think_sent": think,
                    "num_predict": num_predict,
                    "wall_s": round(time.monotonic() - started, 1),
                    "done_reason": payload.get("done_reason"),
                    "eval_count": payload.get("eval_count"),
                    "thinking_chars": len(message.get("thinking") or ""),
                    "content_chars": len(content),
                    "content_conforms_to_schema": valid,
                    "detail": detail,
                    "content": content,
                }
            )
            print(json.dumps(results[-1]))
    OUTPUT_PATH.write_text(
        json.dumps({"ollama_version": version, "model": DEFAULT_MODEL, "results": results}, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
