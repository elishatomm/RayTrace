"""Stub HTTP servers for the model-chain tests: an OpenAI-compatible provider and an Ollama that lists one model.

Each binds 127.0.0.1 on a free port inside the test container, which has no network, and records every request it
receives, so a test can show what was sent, and what never was. Nothing here is a real provider or a real key.
"""
from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pydantic import JsonValue

HOSTED_KEY = "hosted-test-key-not-a-secret-7f3a9c1e5b"  # distinctive, so a scan for it means something
# Mixed case, with an uppercase letter in every 12 characters: text that lowercased an echo of it before the key scrub
# looked would still hold all of it, which a scan that ignores case finds.
MIXED_CASE_KEY = "Hosted-TEST-Key-not-A-SECRET-7f3A9c1E5b"
HOSTED_MODEL = "test-model-1"
OLLAMA_DIGEST = "c" * 64
KEY_PART_CHARS = 12  # hosted_model.KEY_ECHO_CHARS: this many consecutive characters of the key are part of it


def key_parts_in(text: str, key: str) -> list[str]:
    """Every run of KEY_PART_CHARS characters of ``key`` that ``text`` holds, in any case."""
    folded = text.lower()
    parts = (key[start:start + KEY_PART_CHARS] for start in range(len(key) - KEY_PART_CHARS + 1))
    return [part for part in parts if part.lower() in folded]


@dataclass
class Answer:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=lambda: {"Content-Type": "application/json"})
    delay_s: float = 0.0
    chunked: bool = False  # no Content-Length: the body goes out in chunks


@dataclass(frozen=True)
class Seen:
    method: str
    path: str
    headers: dict[str, str]  # lower-case names
    body: bytes

    def json(self) -> dict[str, JsonValue]:
        return json.loads(self.body)


class StubServer:
    def __init__(self, respond: Callable[[Seen], Answer]) -> None:
        self.respond = respond
        self.requests: list[Seen] = []
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.stub = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class _Server(ThreadingHTTPServer):
    stub: StubServer

    def handle_error(self, request: object, client_address: object) -> None:
        """A client that gave up (a timeout test) leaves a broken pipe here; that is the point, not a failure."""


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - the name http.server dispatches to
        self._serve()

    def do_POST(self) -> None:  # noqa: N802
        self._serve()

    def _serve(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        seen = Seen(self.command, self.path, {name.lower(): value for name, value in self.headers.items()},
                    self.rfile.read(length) if length else b"")
        self.server.stub.requests.append(seen)
        answer = self.server.stub.respond(seen)
        if answer.delay_s:
            time.sleep(answer.delay_s)
        self.send_response(answer.status)
        for name, value in answer.headers.items():
            self.send_header(name, value)
        if answer.chunked:
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for start in range(0, len(answer.body), 1024):
                piece = answer.body[start:start + 1024]
                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            return
        if "Content-Length" not in answer.headers:
            self.send_header("Content-Length", str(len(answer.body)))
        self.end_headers()
        self.wfile.write(answer.body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - http.server's signature
        """Quiet: the tests read self.server.stub.requests instead."""


@contextmanager
def stub_server(respond: Callable[[Seen], Answer]) -> Iterator[StubServer]:
    stub = StubServer(respond)
    stub.start()
    try:
        yield stub
    finally:
        stub.stop()


def completion(content: str, *, prompt_tokens: int = 1200, completion_tokens: int = 150) -> Answer:
    """A chat-completions reply as an OpenAI-compatible provider sends one, with a reasoning field to be ignored."""
    payload = {
        "id": "chatcmpl-stub", "object": "chat.completion", "model": f"{HOSTED_MODEL}-2026-11-01",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content, "reasoning": "STUB-REASONING-TEXT"}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }
    return Answer(body=json.dumps(payload).encode())


def json_answer(status: int, payload: object, **headers: str) -> Answer:
    return Answer(status=status, body=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **headers})


def ollama_listing(model: str = "qwen3:8b") -> Callable[[Seen], Answer]:
    """An Ollama that answers GET /api/tags with ``model``, as auto's probe asks."""
    def respond(seen: Seen) -> Answer:
        if seen.path == "/api/tags":
            return json_answer(200, {"models": [{"name": model, "model": model, "digest": OLLAMA_DIGEST}]})
        return json_answer(404, {"error": f"no route {seen.path}"})
    return respond


def hosted_env(base_url: str, **extra: str) -> dict[str, str]:
    """The three hosted settings for a stub provider at ``base_url``, plus ``extra``."""
    return {"WORKBENCH_HOSTED_URL": f"{base_url}/v1", "WORKBENCH_HOSTED_MODEL": HOSTED_MODEL,
            "WORKBENCH_HOSTED_API_KEY": HOSTED_KEY, **extra}


def closed_port_url() -> str:
    """A loopback URL nothing listens on: the port of a server that was started and stopped again."""
    stub = StubServer(lambda _: Answer())
    url = stub.url
    stub._server.server_close()
    return url
