"""Public mode over a real uvicorn server on 127.0.0.1: connection deadlines, the concurrency cap and trusted proxies.

Security audit finding 7: idle connections used up the server's file handles. Starlette's TestClient hands the app a
whole request at once, so it can neither hold a connection open, trickle a request, nor stop reading an answer. These
tests start uvicorn with exactly the options ``api.main`` gave ``uvicorn.run`` (test_public_mode.serve), on a port the
kernel picks, and talk to it over raw sockets. The deadlines are shortened to ``DEADLINE`` seconds.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import uvicorn
from starlette.types import Receive, Scope, Send
from test_public_mode import (
    HOST,
    LIMIT_CONCURRENCY,
    ORIGIN,
    REQUESTS_PER_ADDRESS_PER_MINUTE,
    UI_HEADER,
    no_public_variables,  # noqa: F401  (an autouse fixture: no public variable leaks in from the environment)
    serve,
)
from uvicorn.server import ServerState

from recovery_workbench import public_mode

DEADLINE = 1.0
PATIENCE = 8.0  # how long a test waits for the server to close a connection before calling it held
BIG_ANSWER = 64 * 2**20  # more than the loopback socket buffers hold, so a client that stops reading stalls the write
# A client with a small receive window and small segments: the kernels then hold only part of an answer (32,039 bytes
# when measured in the test container), and the rest waits in the server's own buffer.
SMALL_WINDOW = 4096  # SO_RCVBUF, set before connecting; Linux doubles it
SMALL_SEGMENT = 1400  # TCP_MAXSEG
HIGH_WATER = 64 * 1024  # asyncio's write-buffer high-water mark: only a buffer above it calls pause_writing
CLOSING_ANSWER = 63_000  # body bytes: the whole answer stays under HIGH_WATER and over what the kernels hold

ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


@pytest.fixture(autouse=True)
def short_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public mode's deadlines, shortened. api imports public_mode, which reads each deadline when it arms it."""
    module = sys.modules.get("recovery_workbench.public_mode")
    if module is not None:  # before public mode there was nothing to shorten, and no deadline at all
        monkeypatch.setattr(module, "REQUEST_DEADLINE_SECONDS", DEADLINE)
        monkeypatch.setattr(module, "WRITE_STALL_SECONDS", DEADLINE)


@contextmanager
def served_on_loopback(app: ASGIApp, options: dict[str, object]) -> Iterator[int]:
    """Serve ``app`` with ``options`` (main's, but its host and port) from a thread on 127.0.0.1; yields the port."""
    chosen = {name: value for name, value in options.items() if name not in ("host", "port")}
    listener = socket.create_server(("127.0.0.1", 0))
    # The graceful-shutdown bound belongs to the test: a server that held a connection must still let the test end.
    server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_config=None, timeout_graceful_shutdown=5, **chosen))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        listener.close()
    assert not thread.is_alive(), "the loopback server did not stop"


def connect(port: int) -> socket.socket:
    return socket.create_connection(("127.0.0.1", port), timeout=PATIENCE)


def connect_with_small_window(port: int) -> socket.socket:
    """A client of whose answers the kernels take little, as an attacker tunes its own socket."""
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SMALL_WINDOW)
    client.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, SMALL_SEGMENT)
    client.settimeout(PATIENCE)
    client.connect(("127.0.0.1", port))
    return client


@dataclass
class ServerSide:
    """What the server's protocol saw of a connection: the answer's buffered tail as it completed, and when it ended."""

    answered: threading.Event = field(default_factory=threading.Event)
    lost: threading.Event = field(default_factory=threading.Event)
    closing_at_answer: bool = False
    tail_at_answer: int = 0  # answer bytes still in asyncio's buffer when the answer completed
    lost_at: float = 0.0


def watched(options: dict[str, object], side: ServerSide) -> dict[str, object]:
    """main's options, with its protocol class extended only to record ``side``; every deadline is main's own."""
    protocol = options["http"]
    assert isinstance(protocol, type)

    class Watched(protocol):  # type: ignore[misc, valid-type]
        def on_response_complete(self) -> None:
            side.closing_at_answer = self.transport.is_closing()
            side.tail_at_answer = self.transport.get_write_buffer_size()
            super().on_response_complete()
            side.answered.set()

        def connection_lost(self, exc: Exception | None) -> None:
            super().connection_lost(exc)
            side.lost_at = time.monotonic()
            side.lost.set()

    return {**options, "http": Watched}


def received_until_closed(client: socket.socket) -> int:
    """Read everything the server sends until it ends the connection; how many bytes came."""
    received = 0
    try:
        while chunk := client.recv(65_536):
            received += len(chunk)
    except ConnectionResetError:
        pass  # an abort ends the connection as surely as a close
    return received


def request_head(method: str, path: str, headers: dict[str, str] | None = None, length: int | None = None) -> bytes:
    lines = [f"{method} {path} HTTP/1.1", f"Host: {HOST}", *(f"{name}: {value}" for name, value in (headers or {}).items())]
    if length is not None:
        lines.append(f"Content-Length: {length}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


class Answers:
    """Successive answers on one connection, each with a Content-Length. Bytes past one answer are kept for the next,
    so pipelined answers that arrive in one read are not lost."""

    def __init__(self, client: socket.socket) -> None:
        self._client = client
        self._held = b""

    def next(self) -> tuple[int, dict[str, str], bytes]:
        """The next answer's status, headers (lowercase names) and body."""
        while b"\r\n\r\n" not in self._held:
            self._held += self._receive()
        head, _, rest = self._held.partition(b"\r\n\r\n")
        status_line, *lines = head.decode("latin-1").split("\r\n")
        headers = {name.strip().lower(): value.strip() for name, _, value in (line.partition(":") for line in lines)}
        length = int(headers["content-length"])
        while len(rest) < length:
            rest += self._receive()
        self._held = rest[length:]
        return int(status_line.split()[1]), headers, rest[:length]

    def _receive(self) -> bytes:
        chunk = self._client.recv(65_536)
        assert chunk, f"the server closed the connection before it answered; holding {self._held!r}"
        return chunk


def seconds_until_closed(client: socket.socket) -> float | None:
    """How long the server took to close the connection, reading whatever it sent; None if it held it for PATIENCE."""
    started = time.monotonic()
    client.settimeout(PATIENCE)
    try:
        while client.recv(65_536):
            pass
    except TimeoutError:
        return None
    except ConnectionResetError:
        pass  # an abort ends the connection as surely as a close
    return time.monotonic() - started


def trickle_until_closed(client: socket.socket, data: bytes, every: float = 0.25) -> float | None:
    """Send ``data`` one byte per ``every`` seconds until the server closes the connection: how long that took, or None
    if all of it went and the connection is still open."""
    started = time.monotonic()
    client.settimeout(every)
    for byte in data:
        try:
            client.sendall(bytes([byte]))
            if client.recv(65_536) == b"":
                return time.monotonic() - started
        except TimeoutError:
            continue  # still open and silent: the server waits for the rest
        except (BrokenPipeError, ConnectionResetError):
            return time.monotonic() - started
    return None


def assert_closed_by_the_deadline(waited: float | None, what: str) -> None:
    assert waited is not None, f"the server held a connection that {what}"
    assert waited <= DEADLINE + 3, f"the server took {waited:.1f} s to close a connection that {what}"


async def big_answer(scope: Scope, receive: Receive, send: Send) -> None:
    """An answer far larger than the socket buffers, in 64 KiB pieces."""
    piece = b"x" * 65_536
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-length", str(BIG_ANSWER).encode("ascii"))]})
    for _ in range(BIG_ANSWER // len(piece)):
        await send({"type": "http.response.body", "body": piece, "more_body": True})
    await send({"type": "http.response.body", "body": b"", "more_body": False})


async def closing_answer(scope: Scope, receive: Receive, send: Send) -> None:
    """An answer under the high-water mark, in one piece."""
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-length", str(CLOSING_ANSWER).encode("ascii"))]})
    await send({"type": "http.response.body", "body": b"x" * CLOSING_ANSWER, "more_body": False})


async def slow_answer(scope: Scope, receive: Receive, send: Send) -> None:
    """A whole request, then an answer that takes twice the deadline: work, not a slow client."""
    await asyncio.sleep(DEADLINE * 2)
    body = b"late but whole"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-length", str(len(body)).encode("ascii"))]})
    await send({"type": "http.response.body", "body": body, "more_body": False})


# --------------------------------------------------------------------------- item 8: connection deadlines


def test_a_connection_that_never_sends_a_request_is_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    with served_on_loopback(served.app, served.options) as port, closing(connect(port)) as idle:
        waited = seconds_until_closed(idle)
    assert_closed_by_the_deadline(waited, "sent nothing")


def test_a_request_head_that_trickles_in_is_cut_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    slow_head = b"GET /health HTTP/1.1\r\nHost: " + HOST.encode("ascii") + b"\r\nX-Slow: " + b"s" * 40
    with served_on_loopback(served.app, served.options) as port, closing(connect(port)) as client:
        waited = trickle_until_closed(client, slow_head)
    assert_closed_by_the_deadline(waited, "sent its request head a byte at a time")


def test_a_request_body_that_trickles_in_is_cut_off_and_nothing_logs_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    body = b'{"token": "' + b"t" * 60 + b'", "operator_id": "op_slow_viewer"}'
    head = request_head("POST", "/api/session", {**UI_HEADER, "Origin": ORIGIN, "Content-Type": "application/json"},
                        length=len(body))
    with caplog.at_level(logging.WARNING), served_on_loopback(served.app, served.options) as port, \
            closing(connect(port)) as client:
        client.sendall(head)
        waited = trickle_until_closed(client, body)
    assert_closed_by_the_deadline(waited, "sent its request body a byte at a time")
    assert [(record.name, record.getMessage()) for record in caplog.records if record.levelno >= logging.ERROR] == []


def test_a_kept_alive_connection_that_stalls_its_next_request_is_cut_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first byte of a next request stops uvicorn's keep-alive timer; the deadline still runs."""
    served = serve(monkeypatch, tmp_path / "data")
    with served_on_loopback(served.app, served.options) as port, closing(connect(port)) as client:
        client.sendall(request_head("GET", "/health"))
        status, _, body = Answers(client).next()
        assert (status, body) == (200, b'{"status":"ok"}')
        client.sendall(b"GET /health HTTP/1.1\r\nHo")  # and then nothing more
        waited = seconds_until_closed(client)
    assert_closed_by_the_deadline(waited, "began a second request and stopped")


def test_an_answer_the_client_stops_reading_is_abandoned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = serve(monkeypatch, tmp_path / "data")  # main's options, around an app whose answer outgrows the buffers
    with served_on_loopback(big_answer, served.options) as port, closing(connect(port)) as client:
        client.sendall(request_head("GET", "/"))
        time.sleep(DEADLINE + 3)  # reading nothing meanwhile
        received = 0
        client.settimeout(PATIENCE)
        try:
            while chunk := client.recv(1 << 20):
                received += len(chunk)
        except TimeoutError:
            pytest.fail(f"the connection was still open after {received} bytes of a stalled answer")
        except ConnectionResetError:
            pass
    assert received < BIG_ANSWER  # the server gave up on the answer instead of waiting for the client


@pytest.mark.parametrize("request_bytes", [
    pytest.param(request_head("GET", "/", {"Connection": "close"}), id="connection-close"),
    pytest.param(f"GET / HTTP/1.0\r\nHost: {HOST}\r\n\r\n".encode("ascii"), id="http-1.0"),
])
def test_a_closed_connection_whose_client_stops_reading_a_small_answer_is_aborted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request_bytes: bytes
) -> None:
    """Security audit run 3. uvicorn closes the connection after this answer, but the close waits until the client
    has taken what is buffered, and below the high-water mark no deadline was armed: the connection stayed open."""
    served = serve(monkeypatch, tmp_path / "data")  # main's options, around an app whose answer stays under the mark
    side = ServerSide()
    with served_on_loopback(closing_answer, watched(served.options, side)) as port, \
            closing(connect_with_small_window(port)) as client:
        client.sendall(request_bytes)
        assert side.answered.wait(PATIENCE), "the server never answered"
        answered = time.monotonic()
        # The case under test: uvicorn is closing, and part of the answer waits in the server's buffer, under the
        # high-water mark, for a client that reads nothing.
        assert side.closing_at_answer
        assert 0 < side.tail_at_answer < HIGH_WATER, side.tail_at_answer
        held = not side.lost.wait(PATIENCE)
        received = received_until_closed(client)
    assert not held, f"the server held a closed connection {PATIENCE} s for its {side.tail_at_answer}-byte tail"
    assert side.lost_at - answered <= DEADLINE + 3
    assert received < CLOSING_ANSWER  # the tail that waited was dropped, not delivered later


def test_a_closed_connection_whose_client_reads_gets_its_whole_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control: the same answer, window and close, to a client that reads, arrives whole and ends cleanly."""
    served = serve(monkeypatch, tmp_path / "data")
    side = ServerSide()
    with served_on_loopback(closing_answer, watched(served.options, side)) as port, \
            closing(connect_with_small_window(port)) as client:
        client.sendall(request_head("GET", "/", {"Connection": "close"}))
        status, headers, body = Answers(client).next()
        ended = client.recv(1)  # b"" for a close; an abort would raise ConnectionResetError here
        assert side.lost.wait(PATIENCE)
    assert (status, headers["connection"], len(body), ended) == (200, "close", CLOSING_ANSWER, b"")


class StalledTransport(asyncio.Transport):
    """A transport whose client reads nothing, so a close never completes, as asyncio's would not with a tail."""

    def __init__(self) -> None:
        super().__init__()
        self.closing = False
        self.aborted = False

    def get_extra_info(self, name: str, default: object = None) -> object:
        return default

    def is_closing(self) -> bool:
        return self.closing

    def close(self) -> None:
        self.closing = True

    def abort(self) -> None:
        self.closing = self.aborted = True

    def write(self, data: bytes | bytearray | memoryview) -> None:
        """Taken and kept, as by a client that reads nothing."""

    def pause_reading(self) -> None:
        """Nothing arrives, so there is nothing to pause."""

    def resume_reading(self) -> None:
        """Nothing arrives, so there is nothing to resume."""


@pytest.mark.parametrize("drained", [False, True], ids=["client-reads-nothing", "client-drains-first"])
def test_an_answer_completed_on_a_closing_connection_arms_the_abort(
    monkeypatch: pytest.MonkeyPatch, drained: bool
) -> None:
    """The same fix without sockets, so no kernel's buffer sizes decide the case: once uvicorn has closed the transport
    when an answer completes, the protocol aborts it after the write deadline, unless the connection ends first."""
    monkeypatch.setattr(public_mode, "REQUEST_DEADLINE_SECONDS", 60.0)  # no request deadline acts meanwhile
    loop = asyncio.new_event_loop()
    try:
        config = uvicorn.Config(closing_answer, lifespan="off", log_config=None)
        protocol = public_mode.DeadlineProtocol(config=config, server_state=ServerState(), app_state={}, _loop=loop)
        transport = StalledTransport()
        protocol.connection_made(transport)
        transport.close()  # as uvicorn does once an answer to Connection: close or HTTP/1.0 is whole
        protocol.on_response_complete()
        if drained:
            protocol.connection_lost(None)  # asyncio calls this once the client has taken the tail
        loop.run_until_complete(asyncio.sleep(DEADLINE * 1.5))
    finally:
        loop.close()
    assert transport.aborted is not drained


def test_a_whole_request_is_answered_however_long_the_work_takes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A control: the deadline covers the client's sending, not the server's work, and normal requests pass."""
    served = serve(monkeypatch, tmp_path / "data")
    with served_on_loopback(slow_answer, served.options) as port, closing(connect(port)) as client:
        client.sendall(request_head("GET", "/"))
        status, _, body = Answers(client).next()
    assert (status, body) == (200, b"late but whole")
    with served_on_loopback(served.app, served.options) as port, closing(connect(port)) as client:
        client.sendall(request_head("GET", "/health") * 3)  # three pipelined requests, each whole
        answers = Answers(client)
        replies = [answers.next() for _ in range(3)]
    assert [(status, body) for status, _, body in replies] == [(200, b'{"status":"ok"}')] * 3


# --------------------------------------------------------------------------- item 7: concurrency cap, proxies


def test_requests_past_the_concurrency_cap_get_503_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = sys.modules.get("recovery_workbench.public_mode")
    if module is not None:  # the held connections must outlast the test, however slowly the machine runs it
        monkeypatch.setattr(module, "REQUEST_DEADLINE_SECONDS", 60.0)
    served = serve(monkeypatch, tmp_path / "data")
    with served_on_loopback(served.app, served.options) as port:
        with ExitStack() as held:
            for _ in range(LIMIT_CONCURRENCY):
                held.enter_context(closing(connect(port)))
            with closing(connect(port)) as client:
                client.sendall(request_head("GET", "/health"))
                status, headers, _ = Answers(client).next()
            assert status == 503
            assert headers.get("connection") == "close"
        with closing(connect(port)) as client:  # once the others have gone, the server answers again
            client.sendall(request_head("GET", "/health"))
            assert Answers(client).next()[0] == 200


def statuses_from(port: int, forwarded_for: list[str]) -> list[int]:
    """One request per X-Forwarded-For value, pipelined on one connection; their statuses, in order.

    Pipelined, because one request at a time costs about 40 ms a round trip on this loopback with uvicorn's own
    protocol as with public mode's.
    """
    with closing(connect(port)) as client:
        client.sendall(b"".join(request_head("GET", "/api/session", {"X-Forwarded-For": address})
                                for address in forwarded_for))
        answers = Answers(client)
        return [answers.next()[0] for _ in forwarded_for]


def test_forwarded_addresses_are_believed_only_from_the_named_proxies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = REQUESTS_PER_ADDRESS_PER_MINUTE
    trusting = serve(monkeypatch, tmp_path / "trusting", WORKBENCH_PUBLIC_TRUSTED_PROXIES="127.0.0.1")
    with served_on_loopback(trusting.app, trusting.options) as port:
        first = statuses_from(port, ["203.0.113.9"] * (limit + 1) + ["203.0.113.10"])
    assert first == [200] * limit + [429, 200]  # each forwarded address has its own minute
    elsewhere = serve(monkeypatch, tmp_path / "elsewhere", WORKBENCH_PUBLIC_TRUSTED_PROXIES="198.51.100.1")
    with served_on_loopback(elsewhere.app, elsewhere.options) as port:
        second = statuses_from(port, ["203.0.113.9"] * limit + ["203.0.113.10"])
    assert second == [200] * limit + [429]  # a peer that is not a named proxy cannot choose its address
