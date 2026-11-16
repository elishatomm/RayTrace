"""Public mode: the server behind one private demo link (docs/DEMO_HOSTING.md, section 3.3, scope A).

Off unless ``settings.load_public_settings`` finds ``WORKBENCH_PUBLIC_ORIGIN``; ``api`` wires each piece below in only
then, so with public mode off none of it runs. In public mode:

* viewers share one demo token, one set of runs and one server process, which starts from an empty data directory;
* a session lives in a ``__Host-`` cookie, ends after an idle timeout or an absolute lifetime, and only so many live
  at once: a login ends the session its browser presented, and with every slot live it ends the least recently used
  session instead of refusing, so no one holder of the token can keep the others out;
* seeding takes only the published seeds, a few runs per session and per process, and stops at a data-size limit;
* the expensive routes take a few requests per session per minute and, only behind proxies a setting names, every
  route takes a few hundred per client address per minute;
* the server answers a capped number of requests at once, and closes a connection whose request arrives too slowly
  or whose answer its client stops reading;
* one log line records each login result, run creation, quota refusal and 429, never the token or a cookie value.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import stat
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from uvicorn.protocols.http.h11_impl import H11Protocol

from recovery_workbench import settings

SESSION_COOKIE = "__Host-workbench_session"  # the prefix makes a browser insist on Secure, Path=/ and no Domain
SESSION_IDLE_SECONDS = 30 * 60
SESSION_LIFETIME_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 20
WINDOW_SECONDS = 60.0
EXPENSIVE_PER_SESSION_PER_MINUTE = 10  # seeding, diagnosis, fix validation, planning and execution together
REQUESTS_PER_ADDRESS_PER_MINUTE = 300  # every route, only behind WORKBENCH_PUBLIC_TRUSTED_PROXIES
MAX_TRACKED_ADDRESSES = 10_000
LIMIT_CONCURRENCY = 64  # uvicorn answers 503 at once while this many connections or requests are open
REQUEST_DEADLINE_SECONDS = 15.0
WRITE_STALL_SECONDS = 30.0
DATA_MARKER = ".workbench-public-mode"
DATA_MARKER_TEXT = "Recovery Workbench public mode made this directory its own and empties it at every start.\n"
EVENT_LOG = logging.getLogger("recovery_workbench.public")
_PLAIN_VALUE = re.compile(r"[A-Za-z0-9_.:/@+\-]{1,128}")  # written as is in a log line; anything else is quoted


# --------------------------------------------------------------------------- sessions and request windows


@dataclass
class SessionUse:
    """What public mode tracks for one session, under the Workbench's lock. Times are ``time.monotonic()`` seconds."""

    label: str  # names the session in log lines; random, and unrelated to the cookie
    started: float
    last_seen: float
    runs_created: int = 0
    expensive: deque[float] = field(default_factory=deque)

    def ends_at(self) -> float:
        """When the session ends unless a request comes first: the idle timeout or the absolute lifetime."""
        return min(self.last_seen + SESSION_IDLE_SECONDS, self.started + SESSION_LIFETIME_SECONDS)

    def admit_expensive(self, now: float) -> int | None:
        """Count one expensive request; when the session's minute is full, the seconds until it has room again."""
        while self.expensive and now - self.expensive[0] > WINDOW_SECONDS:
            self.expensive.popleft()
        if len(self.expensive) >= EXPENSIVE_PER_SESSION_PER_MINUTE:
            return max(1, math.ceil(WINDOW_SECONDS - (now - self.expensive[0])))
        self.expensive.append(now)
        return None


class AddressWindow:
    """Requests per client address in the current minute, kept only behind trusted proxies.

    A fixed window keeps memory bounded: it holds at most ``MAX_TRACKED_ADDRESSES`` addresses a minute, and every
    address past that shares one count, which fills fast. A flood from many addresses then slows newcomers instead of
    growing the table.
    """

    OVERFLOW = "<more addresses>"

    def __init__(self) -> None:
        self._started: float | None = None
        self._counts: dict[str, int] = {}

    def admit(self, address: str, now: float) -> int | None:
        """Count one request from ``address``; past the limit, the seconds until the window starts again."""
        if self._started is None or now - self._started >= WINDOW_SECONDS:
            self._started, self._counts = now, {}
        known = address in self._counts or len(self._counts) < MAX_TRACKED_ADDRESSES
        key = address if known else self.OVERFLOW
        self._counts[key] = self._counts.get(key, 0) + 1
        if self._counts[key] > REQUESTS_PER_ADDRESS_PER_MINUTE:
            return max(1, math.ceil(WINDOW_SECONDS - (now - self._started)))
        return None


# --------------------------------------------------------------------------- the data directory


def claim_data_dir(data_dir: Path) -> None:
    """Make ``data_dir`` public mode's own, empty, or refuse to start.

    Every public-mode process starts from an empty data directory, so this deletes what an earlier one left there. It
    deletes only inside a directory that carries its marker, and writes the marker only into a directory that is
    empty or that it has just made, so it never deletes what public mode did not make. It never deletes the directory
    itself, and never follows a symbolic link out of it.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    entries = sorted(data_dir.iterdir())
    marker = data_dir / DATA_MARKER
    if entries and not _is_marker(marker):
        raise ValueError(
            f"public mode needs an empty data directory, or one it marked as its own, and {data_dir} holds "
            f"{len(entries)} entries without its {DATA_MARKER} file; point WORKBENCH_DATA_DIR or --data-dir at a "
            "new empty directory. Nothing was deleted"
        )
    for entry in entries:
        if entry == marker:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)  # removes symbolic links inside it without following them
        else:
            entry.unlink()  # a file, or a symbolic link, never its target
    if not entries:
        _create_marker(marker)


def _create_marker(marker: Path) -> None:
    """Write the marker as a new file. O_EXCL and O_NOFOLLOW refuse anything put in its place since the directory was
    listed empty, a symbolic link included, so the marker text is never written through a link out of the directory."""
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
    except FileExistsError as raced:
        raise ValueError(
            f"public mode found {marker.parent} empty, but {DATA_MARKER} appeared in it before the marker was written, "
            "so something else writes there; point WORKBENCH_DATA_DIR or --data-dir at a new empty directory that only "
            "this server uses. Nothing was deleted"
        ) from raced
    with open(descriptor, "wb") as written:
        written.write(DATA_MARKER_TEXT.encode("utf-8"))


def refuse_marked_data_dir(data_dir: Path) -> None:
    """Outside public mode: raise ValueError when public mode marked ``data_dir`` as its own, before anything uses it.

    Public mode empties a directory it marked at its next start, so anything the loopback server or a CLI command wrote
    there would be lost without warning. A directory without the marker, or with one public mode would not accept, is
    never emptied, so it is used as before.
    """
    if _is_marker(data_dir / DATA_MARKER):
        raise ValueError(
            f"{data_dir} is public mode's data directory: it holds the {DATA_MARKER} file, and public mode empties the "
            "directory at every start, so anything written there would be lost at its next start. Point "
            "WORKBENCH_DATA_DIR or --data-dir at another directory, or delete the marker if public mode no longer uses "
            "this one. Nothing was written"
        )


def _is_marker(path: Path) -> bool:
    """Whether ``path`` is public mode's marker: a regular file, not a link, holding exactly the marker text."""
    expected = DATA_MARKER_TEXT.encode("utf-8")
    try:
        status = path.lstat()
    except (FileNotFoundError, NotADirectoryError):  # no such file, or the data directory is a file: no marker
        return False
    return stat.S_ISREG(status.st_mode) and status.st_size == len(expected) and path.read_bytes() == expected


def directory_bytes(root: Path) -> int:
    """The bytes held by the regular files under ``root``; symbolic links are neither followed nor counted."""
    total = 0
    for directory, _, names in os.walk(root):
        for name in names:
            try:
                status = os.lstat(os.path.join(directory, name))
            except FileNotFoundError:
                continue  # removed after the listing, as SQLite removes a -journal file: it holds no bytes now
            if stat.S_ISREG(status.st_mode):
                total += status.st_size
    return total


# --------------------------------------------------------------------------- connection deadlines


class DeadlineProtocol(H11Protocol):
    """uvicorn's HTTP/1.1 protocol with the deadlines it lacks (security audit finding 7).

    uvicorn 0.54 times only the wait between an answer and the next request (``timeout_keep_alive``), and the first
    byte of that request stops even this timer. No setting covers a connection that sends nothing, sends its request
    head or body slowly, or stops reading its answer: each holds a file handle as long as its client likes, and about
    250 of them used up the server's. Here a request must arrive whole within ``REQUEST_DEADLINE_SECONDS`` of the
    connection opening or of the previous answer ending, or the connection is closed; and a connection whose client
    has stopped reading for ``WRITE_STALL_SECONDS`` is aborted. That includes one uvicorn closes after its answer
    (``Connection: close``, or HTTP/1.0): such a close waits until the client has taken what is still buffered, and an
    answer under asyncio's 64 KiB high-water mark never calls ``pause_writing`` (security audit run 3). Each deadline
    is read when it is armed.
    """

    def connection_made(self, transport: asyncio.Transport) -> None:  # type: ignore[override]
        self._request_deadline: asyncio.TimerHandle | None = None
        self._write_deadline: asyncio.TimerHandle | None = None
        self._closing_deadline: asyncio.TimerHandle | None = None
        self._awaited_after: object | None = None  # the request before the one the deadline waits for
        super().connection_made(transport)
        self._await_request()

    def data_received(self, data: bytes) -> None:
        super().data_received(data)
        self._note_request_arrival()

    def on_response_complete(self) -> None:
        if not self.transport.is_closing():
            self._await_request()
        elif self._closing_deadline is None:
            # uvicorn closed the transport after this answer. Its tail may wait in the buffer for a client that reads
            # nothing, and below the high-water mark no other deadline is armed. connection_lost cancels this one.
            self._closing_deadline = self.loop.call_later(WRITE_STALL_SECONDS, self.transport.abort)
        super().on_response_complete()  # this may take a pipelined request that has already arrived whole
        self._note_request_arrival()

    def pause_writing(self) -> None:
        super().pause_writing()
        if self._write_deadline is None:
            self._write_deadline = self.loop.call_later(WRITE_STALL_SECONDS, self._abort_stalled_write)

    def resume_writing(self) -> None:
        super().resume_writing()
        _cancel(self._write_deadline)
        self._write_deadline = None

    def connection_lost(self, exc: Exception | None) -> None:
        for deadline in (self._request_deadline, self._write_deadline, self._closing_deadline):
            _cancel(deadline)
        self._request_deadline = self._write_deadline = self._closing_deadline = None
        super().connection_lost(exc)

    def _await_request(self) -> None:
        _cancel(self._request_deadline)
        self._awaited_after = self.cycle
        self._request_deadline = self.loop.call_later(REQUEST_DEADLINE_SECONDS, self._close_slow_request)

    def _note_request_arrival(self) -> None:
        """Stop the request deadline once a request newer than the awaited one's predecessor has arrived whole."""
        cycle = self.cycle
        if (self._request_deadline is not None and cycle is not None and cycle is not self._awaited_after
                and not cycle.more_body):
            self._request_deadline.cancel()
            self._request_deadline = None

    def _close_slow_request(self) -> None:
        self._request_deadline = None
        self.transport.close()  # sends whatever was already written first...
        # ...which a client that reads nothing would never take, so abort if it is still open after the write deadline.
        self._closing_deadline = self.loop.call_later(WRITE_STALL_SECONDS, self.transport.abort)

    def _abort_stalled_write(self) -> None:
        self._write_deadline = None
        self.transport.abort()


def _cancel(deadline: asyncio.TimerHandle | None) -> None:
    if deadline is not None:
        deadline.cancel()


def server_options(public: settings.PublicSettings) -> dict[str, object]:
    """uvicorn's options for a public-mode server, besides its host, port and log level.

    Forwarded headers are believed only from the proxies the operator named; without them uvicorn takes none, as the
    loopback server does, so no client can choose its own address or scheme.
    """
    options: dict[str, object] = {"limit_concurrency": LIMIT_CONCURRENCY, "http": DeadlineProtocol}
    if public.trusted_proxies:
        return {**options, "proxy_headers": True, "forwarded_allow_ips": list(public.trusted_proxies)}
    return {**options, "proxy_headers": False}


# --------------------------------------------------------------------------- event log


class _CurrentStderr(logging.Handler):
    """Writes each record to whatever ``sys.stderr`` is when it arrives, as Python's last-resort handler does."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
            sys.stderr.flush()
        except Exception:  # logging's contract: report a failed write on stderr, never raise it into a request
            self.handleError(record)


def show_events() -> None:
    """Print public mode's event lines on the console, where a host's log viewer reads them. Once per process."""
    if not any(isinstance(handler, _CurrentStderr) for handler in EVENT_LOG.handlers):
        handler = _CurrentStderr()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(message)s"))  # uvicorn's own line shape
        EVENT_LOG.addHandler(handler)
    EVENT_LOG.setLevel(logging.INFO)


def log_event(level: int, event: str, **fields: object) -> None:
    """One line, ``public-mode event=<event> name=value ...``. Callers pass no secret: never the token or a cookie."""
    written = " ".join(f"{name}={_log_value(value)}" for name, value in fields.items())
    EVENT_LOG.log(level, "public-mode event=%s %s", event, written)


def _log_value(value: object) -> str:
    text = str(value)
    return text if _PLAIN_VALUE.fullmatch(text) else repr(text)  # quoted, so no value can forge a line
