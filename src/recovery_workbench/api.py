"""HTTP API for the one-page UI (SPEC §11). Synthetic sandbox only; it listens on 127.0.0.1 only.

Start it with ``uv run python -m recovery_workbench.api --data-dir DIR [--port 8000]``. At start the
server makes a random operator token and prints it once to its own console. The token never appears
in a response or a log line.

Security, even on localhost:

* ``POST /api/session`` exchanges the token and a human operator ID (``op_...``) for an httpOnly,
  SameSite=Strict session cookie. Every other ``/api`` route needs that session, except the webhook
  route below.
* Every request must name an allowed ``Host`` (127.0.0.1 or localhost on the bound port), so a
  DNS-rebinding page can neither read nor write.
* Every request in a method other than GET, HEAD and OPTIONS, so every state change, must carry the
  ``X-Workbench-Request: 1`` header. An HTML form cannot set it, and another origin cannot add it
  without CORS, which is not enabled. When the browser sends ``Origin`` or ``Sec-Fetch-Site``, they
  must name this origin.
* A request that declares a body over ``MAX_REQUEST_BODY_BYTES`` (64 KiB) is refused with 413 before
  any route runs, so before the session check and before login. A body sent without a length is
  refused at the read that takes it past the limit, which a route makes before either; a route that
  takes no body never reads it. That is Starlette's ``Content Too Large``: plain text for a declared
  length, ``{"detail"}`` for a body sent without one. Only the webhook route reads its own body, as
  below.
* ``POST /api/runs/{run_id}/webhooks/{integration_id}`` is called by the simulated provider, not the
  UI. The HMAC signature over the raw body authenticates it, so it takes no session, and it alone is
  exempt from the ``X-Workbench-Request`` header: POST only, on a path of exactly that shape, read
  as the router reads it. The Host, Origin and Sec-Fetch-Site checks still apply; a provider sends
  neither of the last two, and a browser on another site is refused. The route stops reading the
  body once it holds one byte more than the receiver's ceiling and then closes the connection, takes
  a capped number of deliveries per minute per server process, and answers only the outcome and,
  once verified, the event ID. The merchant receiver deployed at receipt reads a body only once it is
  within the integration's size limit and the nesting limit; any other body reaches ingestion as sent.
* The operator behind a fix validation, approval or escalation comes from the session, never from a
  request body. Model output reaches no route that approves or executes anything.

Public mode (``public_mode``, on only when ``WORKBENCH_PUBLIC_ORIGIN`` is set) serves one https origin instead of the
loopback names, takes the operator token from ``WORKBENCH_DEMO_TOKEN`` and never prints it, has no webhook route and
so no exemption for one, puts the session in a Secure ``__Host-`` cookie with an idle timeout and a lifetime, adds
quotas, rate limits and ``Strict-Transport-Security``, and answers ``/health`` with its status only. It starts
from an empty data directory, into which it copies the demo runs the image holds when ``WORKBENCH_PUBLIC_TEMPLATE_DIR``
names them (``public_template``). With it off, everything here holds as written, and the server refuses a data
directory that public mode marked as its own, since public mode empties that directory at its next start.

Replay time: every evidence time the API fixes from a run's replay clock is taken under
``Workbench.clock_lock``. That covers a diagnosis cutoff, an advance, an operator step, a refresh,
a seeding replay and the order queue's time. A webhook delivery holds the same lock while it moves
the clock one millisecond forward and is received at that new time, so no record ever becomes known
at or before an evidence time the API has already used. A separate CLI process working on the same
data directory does not take this lock, so it is not covered while the API receives webhooks.

Recovery timing comes from the server's settings object, loaded once at start or injected, never from the
environment when a request arrives; the CLI reads the environment when a command runs.

The model source (owner answer to Q46: the local model, a hosted one or the rules) is chosen once, when the app is
created (``model_source.resolve``), and kept for the server's life. ``GET /api/scenarios`` gives it to the page;
``/health`` repeats it but never calls the hosted provider. A diagnosis request may name hosted only when the hosted
settings are complete, and every hosted call counts against one daily budget for the process.

* Freshness (owner decision Q31): every staleness judgement takes ``stale_after_seconds`` from it. That covers the
  order queue, the reprocess planner, the executor's recheck, the provider-replay instructions, the
  late-authorisation facts and a seeded run's oracle.
* Expiry (owner decision Q35): a reprocess plan's ``plan_ttl_seconds`` and an approval's ``approval_ttl_seconds``.

Before provider-replay instructions are prepared, the API re-checks the refresh candidates read-only, as the refresh
route does (owner decision Q26): a late authorisation whose webhooks were all lost still looks failed until the
provider is asked again. Without the run's provider records on this server the instructions are prepared without
that re-check, and the answer says so. It then reads the provider's delivery log for every capture the instructions
would cover (owner decision Q29 (b)), in the re-check's operator step, or in one of its own when nothing needed a
refresh: a capture the provider recorded as answered 2xx is held for escalation, because its dashboard will not
resend it. The answer's ``delivery_log`` says what the read found, or ``delivery_log_skipped_because`` why there was
none.

Errors are JSON ``{"error", "detail"}``: 409 for a stale or rejected plan, 422 for a contract
violation or malformed input, 403 for a scope or authority failure (including a missing session),
and 404 for an order that is not in a run's order queue at the requested replay time, or for a diagnosis job this
server issued, finished and no longer keeps: it keeps every live job and its newest ``MAX_FINISHED_JOBS`` finished ones.
Details never carry stack traces or server file paths. The webhook route answers ``{"outcome",
"event_id"}`` with the receiver's status (200, 400, 401, 404 or 413). For a run this server does not
have it answers 404 ``{"error", "detail"}``, not the 403 other routes give, because a sender has no
session. When it cannot take a delivery now it answers ``{"error", "detail"}`` with ``Retry-After``:
429 over the rate cap, 503 for any storage failure on its path.
"""
from __future__ import annotations

import argparse
import hmac
import logging
import math
import re
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing, asynccontextmanager, closing, contextmanager
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

import uvicorn
from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, JsonValue
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.middleware.body_limit import RequestBodyLimitMiddleware
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Receive, Scope, Send

from recovery_workbench import (
    cli,
    diagnosis,
    diagnosis_store,
    executor,
    fixes,
    hosted_model,
    ingestion,
    model_source,
    order_cases,
    packet,
    plans,
    policy,
    public_mode,
    public_template,
    refresh,
    report,
    settings,
    storage,
)
from recovery_workbench.contracts import (
    ActionStatus,
    AmountStatus,
    CaseState,
    ContractError,
    ModelMode,
    NextStep,
    Operation,
    ScopeError,
    StaleStateError,
    StrictModel,
    WorkbenchError,
)
from recovery_workbench.executor import PlanRejectedError
from recovery_workbench.simulator import driver, runtime, scenarios
from recovery_workbench.simulator.world import generate_world, run_id_for

logger = logging.getLogger(__name__)

APP_NAME = "recovery-workbench"
BIND_HOST = "127.0.0.1"
# Hosts route traffic to the container's interface, so public mode may listen on every interface; nothing else may.
ALL_INTERFACES = "0.0.0.0"
DEFAULT_PORT = cli.API_PORT
DEFAULT_UI_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"
SESSION_COOKIE = "workbench_session"
REQUEST_HEADER = "x-workbench-request"
# The only methods the request guard passes on the Host check alone. It names these, not the ones it checks, so a route
# added later with any other method, standard or made up, fails closed.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SAME_ORIGIN_FETCH_SITES = frozenset({"same-origin", "none"})
HEALTH_CACHE_SECONDS = 30.0
MAX_FAILED_LOGINS_PER_MINUTE = 10
RATE_WINDOW_SECONDS = 60.0
DEFAULT_MAX_WEBHOOK_DELIVERIES_PER_MINUTE = 600
NOT_DURABLE_RETRY_AFTER_SECONDS = 30
RECEIPT_CLOCK_STEP_MS = 1  # a webhook delivery is received this long after the replay clock it finds
MAX_SEED = 2**31 - 1
MAX_ADVANCE_MS = 24 * 60 * 60_000
MAX_REFRESH_PAYMENTS = 200
# Request bodies on every route but the webhook route. The largest request model, a RefreshRequest of
# MAX_REFRESH_PAYMENTS IDs of 128 characters, is 26,217 bytes of compact JSON.
MAX_REQUEST_BODY_BYTES = 64 * 1024
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
# Public mode only, which serves https alone: a browser that has opened the demo once refuses plain http to its host for
# a year, so a later visit typed without https:// cannot be served by a stripping proxy that asks for the demo token.
# No includeSubDomains: the server answers one host and has no say over names under it. No preload: only a registrable
# domain can be preloaded, and a platform's name (<name>.<host>) is not one.
PUBLIC_TRANSPORT_SECURITY = "max-age=31536000"
# Finished diagnosis jobs kept in memory, newest first; each holds its packet. A live job is always kept.
MAX_FINISHED_JOBS = 64
# A job ID is job_ and 16 hex digits, as before: 12 random, then 4 that tag them with the server's key, so the server
# tells an ID it issued and has since dropped (404) from one it never issued (403) without remembering every ID.
JOB_NONCE_BYTES = 6
JOB_TAG_BYTES = 2
ISSUED_JOB_ID = re.compile(rf"job_([0-9a-f]{{{2 * JOB_NONCE_BYTES}}})([0-9a-f]{{{2 * JOB_TAG_BYTES}}})")
METRIC_DEFINITIONS: dict[str, str] = {
    "provider_authorisation": (
        "Mature cohort only: attempts created in the window that ends 10 minutes before the replay clock, so "
        "slow authorisations have had time to arrive. Ratio = attempts whose state shows an authorisation "
        "(authorised, captured or refunded) / attempts."
    ),
    "provider_freshness": "Replay time since the newest provider observation of the cohort was known.",
    "merchant_completion": (
        "Orders with one captured payment, known for at least 5 minutes (the grace period), that the worker "
        "could process: fulfilled / all of them."
    ),
    "pending_captured_value": (
        "The value of those orders not yet fulfilled. It counts no capture newer than the grace period and applies "
        "only the exclusions the worker itself applies, so it differs from a plan's value by design."
    ),
    "plan_value": (
        "The sum of exactly the plan's target rows: captured orders with an unprocessed event and no exclusion. "
        "No grace period; every exclusion applies."
    ),
}
QUEUE_PAGE_LIMIT = 1_000  # summary rows per page of the order queue, unless the caller asks for another number
MAX_QUEUE_PAGE_LIMIT = 5_000
QUEUE_DEFINITIONS: dict[str, str] = {
    "age_ms": "Replay time since the order was created: as_of_ms minus its created_at_ms. Null without an order record.",
    "freshness_ms": (
        "Replay time since the newest provider observation of the order's payments became known. Null when none is "
        "visible."
    ),
    "captured_unprocessed_value_paise": (
        "Order value of the captured_processing_incomplete orders: one clean capture, a durable captured event the "
        "worker has not settled, and no blocker. A reprocess plan targets these, unless the corrected handler's dry "
        "run fails on one."
    ),
    "unresolved_value_paise": (
        "Order value of every order that still needs an action or a hold: every state except complete, "
        "payment_failed and insufficient_evidence. Amounts that are unknown, conflicting or in another currency are "
        "counted in unresolved_unasserted_orders, never summed."
    ),
    "totals": (
        "Totals and coverage cover the whole queue at as_of_ms, never the filtered page: a filter cannot change the "
        "money shown."
    ),
    "refund_flag_read": (
        "The orders table's refund flag carries no date, so it is read only when as_of_ms is at or after the run's "
        "replay clock. At an earlier time only a dated refund observation counts."
    ),
}

ModelProbe = Callable[[], str]  # returns the model digest or raises diagnosis.ModelCallError
ModelClientFactory = Callable[[], diagnosis_store.ModelClient]
JobStatus = Literal["queued", "running", "done", "failed"]
_RECORD_ID_CHARACTER = r"[A-Za-z0-9_.:@\-]"
# At least one character that is not a dot. The UI puts these IDs in URL paths, where a browser reads . and .. as
# "this directory" and "its parent", so a record with such an ID could not be opened.
RecordId = Annotated[str, Field(min_length=1, max_length=128,
                                pattern=rf"^{_RECORD_ID_CHARACTER}*[A-Za-z0-9_:@\-]{_RECORD_ID_CHARACTER}*$")]
# A path segment RecordId takes: 1 to 128 of its characters, not all of them dots.
_RECORD_ID_SEGMENT = rf"(?!\.+(?:/|\Z)){_RECORD_ID_CHARACTER}{{1,128}}"
# The one route the request guard lets through without the UI header: its HMAC signature authenticates it. Its
# segments are RecordId's, so the waiver covers no path the route itself would refuse.
WEBHOOK_PATH = re.compile(rf"/api/runs/{_RECORD_ID_SEGMENT}/webhooks/{_RECORD_ID_SEGMENT}")
# The POST routes public mode rate-limits per session: seeding, diagnosis, fix validation, planning and execution.
EXPENSIVE_PATH = re.compile(
    rf"/api/runs(?:/{_RECORD_ID_SEGMENT}/(?:diagnoses|fixes/{_RECORD_ID_SEGMENT}/validate|plans"
    rf"|plans/{_RECORD_ID_SEGMENT}/execute))?"
)


# --------------------------------------------------------------------------- request bodies


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(_Body):
    token: Annotated[str, Field(min_length=1, max_length=256)]
    operator_id: Annotated[str, Field(pattern=f"^{policy.HUMAN_OPERATOR_ID.pattern}$")]


class CreateRunRequest(_Body):
    scenario: str
    seed: Annotated[int, Field(ge=0, le=MAX_SEED)]


class AdvanceRequest(_Body):
    by_ms: Annotated[int, Field(gt=0, le=MAX_ADVANCE_MS)]


class DiagnoseRequest(_Body):
    incident_id: RecordId | None = None
    mode: ModelMode | None = None


class ValidateFixRequest(_Body):
    diagnosis_id: RecordId


class PlanRequest(_Body):
    operation: Literal["reprocess_inbox", "escalate", "prepare_provider_replay"] = "reprocess_inbox"
    fix_validation_id: RecordId | None = None
    diagnosis_id: RecordId | None = None


class ApproveRequest(_Body):
    plan_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class RefreshRequest(_Body):
    payment_ids: Annotated[list[RecordId], Field(min_length=1, max_length=MAX_REFRESH_PAYMENTS)]


# --------------------------------------------------------------------------- server state


@dataclass(frozen=True)
class ApiConfig:
    data_dir: Path
    operator_token: str = field(repr=False)  # a secret, the demo token in public mode: in no repr, as PublicSettings
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    ui_dir: Path | None = DEFAULT_UI_DIR
    model_client_factory: ModelClientFactory | None = None  # tests inject fakes; None means Ollama from settings
    # None means ask the configured Ollama for the model digest: for /health, and for auto's choice at start.
    model_probe: ModelProbe | None = None
    max_webhook_deliveries_per_minute: int = DEFAULT_MAX_WEBHOOK_DELIVERIES_PER_MINUTE  # per server process
    public: settings.PublicSettings | None = None  # public mode; None, the default, keeps it off

    def __post_init__(self) -> None:
        if self.max_webhook_deliveries_per_minute < 1:
            raise ValueError(
                f"max_webhook_deliveries_per_minute must be at least 1, got {self.max_webhook_deliveries_per_minute}"
            )
        if self.public is None:
            return
        # Public mode serves exactly its one origin, with the demo token: never a loopback name beside it.
        if self.allowed_hosts != {self.public.host} or self.allowed_origins != {self.public.origin}:
            raise ValueError(
                f"public mode serves only Host {self.public.host!r} and Origin {self.public.origin!r}; got hosts "
                f"{sorted(self.allowed_hosts)} and origins {sorted(self.allowed_origins)}"
            )
        if not hmac.compare_digest(self.operator_token.encode("utf-8"), self.public.demo_token.encode("utf-8")):
            raise ValueError("public mode's operator token must be the demo token from WORKBENCH_DEMO_TOKEN")


@dataclass(frozen=True)
class OperatorSession:
    operator_id: str
    created_wall_ms: int


@dataclass(frozen=True)
class DiagnosisJob:
    job_id: str
    run_id: str
    incident_id: str
    mode: ModelMode
    cutoff_ms: int
    created_wall_ms: int
    status: JobStatus = "queued"
    diagnosis_id: str | None = None
    error: str | None = None
    packet: dict[str, JsonValue] | None = None
    finished_wall_ms: int | None = None

    def advanced(
        self,
        status: JobStatus,
        *,
        diagnosis_id: str | None,
        error: str | None,
        packet: dict[str, JsonValue] | None,
        finished_wall_ms: int | None,
    ) -> DiagnosisJob:
        """The same job in a later state; the identity fields never change."""
        return DiagnosisJob(
            job_id=self.job_id, run_id=self.run_id, incident_id=self.incident_id, mode=self.mode,
            cutoff_ms=self.cutoff_ms, created_wall_ms=self.created_wall_ms, status=status,
            diagnosis_id=diagnosis_id, error=error, packet=packet, finished_wall_ms=finished_wall_ms,
        )


class RunExistsError(WorkbenchError):
    """The seed would recreate a run that is already in this data directory."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run {run_id!r} already exists in this data directory; open it or choose another seed")


class DiagnosisBusyError(WorkbenchError):
    """One inference at a time: a diagnosis job is already queued or running."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        super().__init__(f"diagnosis job {job_id!r} is still running; poll it and try again when it finishes")


class TooManyLoginsError(WorkbenchError):
    """Too many wrong operator tokens in the last minute."""


class RetryLaterError(WorkbenchError):
    """The request was not taken now; the answer's ``Retry-After`` says when the sender may try again."""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class TooManyDeliveriesError(RetryLaterError):
    """More webhook deliveries in the last minute than this server process takes (HTTP 429)."""


class DeliveryNotStoredError(RetryLaterError):
    """The receipt transaction failed: nothing was stored or acknowledged, so the sender must retry (HTTP 503)."""


class JobNotKeptError(WorkbenchError):
    """A diagnosis job this server issued has finished and been dropped from memory (HTTP 404)."""


class QuotaExceededError(WorkbenchError):
    """Public mode: a new run would pass a run cap or the data limit (HTTP 429). Waiting does not lift it."""


class SeedNotOfferedError(ContractError):
    """Public mode: the seed is not one of the published seeds (HTTP 422)."""


class ReceiverFailedError(WorkbenchError):
    """The deployed merchant receiver raised while preparing a delivery for verification (HTTP 500)."""


class UnknownRunError(ScopeError):
    """A webhook delivery names a run this server does not have (HTTP 404).

    Other routes answer an unknown run with a plain ``ScopeError`` (403) whose wording assumes an
    operator session; a sender has none, so only the webhook route raises this.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"run {run_id!r} does not exist on this server; check the run ID in the delivery URL")


class OrderNotInQueueError(WorkbenchError):
    """The run's order queue at the requested replay time holds no such order (HTTP 404)."""


class Workbench:
    """Everything the routes share: configuration, sessions, diagnosis jobs, the webhook rate window and the health cache."""

    def __init__(self, config: ApiConfig, configured: settings.Settings) -> None:
        self.config = config
        # The model source, chosen here once for the server's whole life: auto probes the local model now, never again.
        self.model_source = model_source.resolve(configured, local_probe=config.model_probe)
        self.settings = model_source.settled(configured, self.model_source)
        self.hosted_budget = hosted_model.HostedCallBudget(self.settings.hosted.max_calls_per_day)  # per process
        self.public = config.public
        self.session_cookie = SESSION_COOKIE if config.public is None else public_mode.SESSION_COOKIE
        self.database = config.data_dir / cli.DATABASE_FILE
        self.recordings_dir = config.data_dir / cli.RECORDINGS_DIRECTORY
        self._lock = threading.Lock()
        self.seed_lock = threading.Lock()  # one seeding at a time, so two requests cannot create the same run
        # Held while an evidence time is fixed from a replay clock, and by a webhook delivery from its clock
        # step through its receipt, so no record becomes known at or before an evidence time already used.
        self.clock_lock = threading.Lock()
        self._sessions: dict[str, OperatorSession] = {}
        self._jobs: dict[str, DiagnosisJob] = {}
        self._finished_jobs: deque[str] = deque()  # the finished jobs in _jobs, oldest first
        self._job_key = secrets.token_bytes(32)  # tags the job IDs this process issues; never leaves it
        self._failed_logins: deque[float] = deque()
        self._webhook_deliveries: deque[float] = deque()
        self._health: tuple[float, dict[str, JsonValue]] | None = None
        self._inference = ThreadPoolExecutor(max_workers=1, thread_name_prefix="diagnosis")
        # Public mode only, all under self._lock: each live session's use, the runs this process created, and the
        # requests per client address (behind trusted proxies only).
        self._session_use: dict[str, public_mode.SessionUse] = {}
        self._public_runs_created = 0
        self._addresses = public_mode.AddressWindow()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with closing(storage.connect(self.database)) as conn:
            yield conn

    def public_detail(self, problem: BaseException) -> str:
        """The error message without server paths."""
        detail = str(problem)
        for path in {str(self.config.data_dir.resolve()), str(self.config.data_dir)}:
            detail = detail.replace(path, "<data_dir>")
        return detail

    # ----------------------------------------------------------------------- sessions

    def login(self, token: str, operator_id: str, presented: str | None = None) -> tuple[str, OperatorSession]:
        """A new session for the right token. In public mode it replaces ``presented``, the session ID the browser sent,
        when that session is live; the loopback server ignores it."""
        now = time.monotonic()
        # The window counts every caller's wrong tokens together, so if a full window refused the right token, any
        # program without it could keep the operator out. Guessing needs no window: main() draws the token, 256 random
        # bits, or in public mode takes WORKBENCH_DEMO_TOKEN only in the shape secrets.token_urlsafe(32) writes, which
        # refuses the shapes a person picks (settings._demo_token_problem). No shape check proves a token random, so
        # the operator is told to generate it. If a shorter or chosen token is ever allowed, revisit this.
        if not hmac.compare_digest(token.encode("utf-8"), self.config.operator_token.encode("utf-8")):
            with self._lock:
                while self._failed_logins and now - self._failed_logins[0] > RATE_WINDOW_SECONDS:
                    self._failed_logins.popleft()
                if len(self._failed_logins) >= MAX_FAILED_LOGINS_PER_MINUTE:
                    self.log_public(logging.WARNING, "login", result="throttled", status=429, operator=operator_id)
                    raise TooManyLoginsError("too many wrong operator tokens in the last minute; wait and try again")
                self._failed_logins.append(now)
            self.log_public(logging.WARNING, "login", result="wrong_token", status=403, operator=operator_id)
            if self.public is not None:  # public mode never prints its token
                raise ScopeError("the demo token is not valid; use the demo token you were given")
            raise ScopeError("the operator token is not valid; use the token the server printed at start")
        session_id = secrets.token_urlsafe(32)
        session = OperatorSession(operator_id=operator_id, created_wall_ms=storage.wall_clock_ms())
        label: str | None = None  # public mode's name for the session in its log lines
        replaced: str | None = None  # and for the browser's earlier session that this one replaced
        with self._lock:
            if self.public is not None:
                label, replaced = self._open_public_session(session_id, now, presented)
            self._sessions[session_id] = session
            live = len(self._sessions)
        ended = {} if replaced is None else {"replaced": replaced}
        self.log_public(logging.INFO, "login", result="ok", status=200, operator=operator_id, session=label,
                         live_sessions=live, **ended)
        return session_id, session

    def session(self, session_id: str | None) -> OperatorSession | None:
        if not session_id:
            return None
        with self._lock:
            found = self._sessions.get(session_id)
            if found is not None and self.public is not None and not self._public_session_alive(session_id):
                return None
            return found

    def logout(self, session_id: str | None) -> None:
        with self._lock:
            self._sessions.pop(session_id or "", None)
            self._session_use.pop(session_id or "", None)

    # ----------------------------------------------------------------------- public mode: sessions, limits, quotas

    def log_public(self, level: int, event: str, **fields: object) -> None:
        if self.public is not None:  # with public mode off the server logs none of these events, as before
            public_mode.log_event(level, event, **fields)

    def _open_public_session(self, session_id: str, now: float, presented: str | None) -> tuple[str, str | None]:
        """Under self._lock: track a new public session; its label, and the label of the browser's session it replaced.

        It ends the session the browser presented, if that is live, and the expired ones. While every slot is still
        live it then ends the one least recently used, so a holder of the token who opens session after session can
        push others out, but never lock them out: anyone with the token gets in.
        """
        replaced = None
        if presented is not None and presented in self._session_use:
            replaced = self._session_use[presented].label
            self._end_public_session(presented)
        for expired in [key for key, use in self._session_use.items() if now >= use.ends_at()]:
            self._end_public_session(expired)
        while len(self._sessions) >= public_mode.MAX_SESSIONS:
            self._evict_least_recently_used(now)
        label = f"s_{secrets.token_hex(4)}"
        self._session_use[session_id] = public_mode.SessionUse(label=label, started=now, last_seen=now)
        return label, replaced

    def _evict_least_recently_used(self, now: float) -> None:
        """Under self._lock: end the live public session that has gone longest without a request, and log it."""
        session_id, use = min(self._session_use.items(), key=lambda item: item[1].last_seen)
        operator_id = self._sessions[session_id].operator_id
        self._end_public_session(session_id)
        self.log_public(logging.WARNING, "session_evicted", session=use.label, operator=operator_id,
                         idle_seconds=round(now - use.last_seen), live_sessions=len(self._sessions))

    def _end_public_session(self, session_id: str) -> None:
        """Under self._lock: forget a public session; its cookie then names no session."""
        del self._sessions[session_id]
        del self._session_use[session_id]

    def _public_session_alive(self, session_id: str) -> bool:
        """Under self._lock: note a request on a live public session, or end it once idle or too old."""
        use = self._session_use[session_id]
        now = time.monotonic()
        if now >= use.ends_at():
            del self._sessions[session_id]
            del self._session_use[session_id]
            return False
        use.last_seen = now
        return True

    def admit_expensive(self, session_id: str | None) -> tuple[int | None, str | None]:
        """Count one expensive request against its session's minute: the seconds to wait when that minute is full,
        and the session's label for the log.

        A request without a live public session is not counted: its route refuses it.
        """
        now = time.monotonic()
        with self._lock:
            use = self._session_use.get(session_id or "")
            return (None, None) if use is None else (use.admit_expensive(now), use.label)

    def admit_address(self, address: str) -> int | None:
        """Count one request from a client address; the seconds to wait when its minute is full."""
        now = time.monotonic()
        with self._lock:
            return self._addresses.admit(address, now)

    def reserve_public_run(self, session_id: str | None, public: settings.PublicSettings) -> None:
        """Under seed_lock: refuse a new run past the session's cap, the process's cap or the data limit."""
        with self._lock:
            use = self._session_use.get(session_id or "")
            created = self._public_runs_created
            label = None if use is None else use.label
            session_full = use is not None and use.runs_created >= public.runs_per_session
        if session_full:
            reason, detail = "session_runs", (f"this session has created {public.runs_per_session} runs, the most "
                                              "public mode allows a session")
        elif created >= public.runs_per_process:
            reason, detail = "process_runs", f"this server has created {created} runs, the most public mode allows"
        elif (held := public_mode.directory_bytes(self.config.data_dir)) >= public.data_limit_bytes:
            reason, detail = "data_limit", (f"the data directory holds {held // 2**20} MB, at public mode's limit of "
                                            f"{public.data_limit_bytes // 2**20} MB")
        else:
            return
        self.log_public(logging.WARNING, "quota_refused", reason=reason, status=429, session=label)
        raise QuotaExceededError(f"{detail}; open one of the runs that already exist")

    def count_public_run(self, session_id: str | None) -> tuple[str | None, int]:
        """Under seed_lock, after a new run was seeded: count it; its session's label and this process's total."""
        with self._lock:
            self._public_runs_created += 1
            use = self._session_use.get(session_id or "")
            if use is not None:
                use.runs_created += 1
            return (None if use is None else use.label), self._public_runs_created

    # ----------------------------------------------------------------------- webhook rate limit

    def admit_webhook_delivery(self) -> None:
        """Count one webhook request against this process's cap, or refuse it when the last minute is full.

        The webhook route has no session, so this cap is its only brake. The route calls this before
        it reads the body or opens the database, so a refused request costs neither.
        """
        now = time.monotonic()
        with self._lock:
            while self._webhook_deliveries and now - self._webhook_deliveries[0] > RATE_WINDOW_SECONDS:
                self._webhook_deliveries.popleft()
            if len(self._webhook_deliveries) >= self.config.max_webhook_deliveries_per_minute:
                oldest_age = now - self._webhook_deliveries[0]
                raise TooManyDeliveriesError(
                    "too many webhook deliveries in the last minute; send again after the Retry-After interval",
                    retry_after_seconds=max(1, math.ceil(RATE_WINDOW_SECONDS - oldest_age)),
                )
            self._webhook_deliveries.append(now)

    # ----------------------------------------------------------------------- health

    def health(self) -> dict[str, JsonValue]:
        if self.public is not None:
            return {"status": "ok"}  # it needs no session, so in public mode it names no version, model or clock
        return {
            "app": APP_NAME,
            "version": metadata.version(APP_NAME),
            "environment": self.settings.environment,
            "execution_mode": self.settings.model_mode,
            "model": self._model_health(),
            "model_source": self.model_source.document(),  # chosen at start; nothing is called to answer this
            "versions": {
                "prompt": diagnosis.PROMPT_VERSION, "validator": diagnosis.VALIDATOR_VERSION,
                "packet": packet.PACKET_VERSION, "policy": policy.POLICY_VERSION, "report": report.REPORT_VERSION,
            },
            "wall_clock_ms": storage.wall_clock_ms(),
        }

    def _model_health(self) -> dict[str, JsonValue]:
        if self.settings.model_mode == ModelMode.HOSTED.value:
            # Never probed: /health needs no session, so a call to the provider would let anyone spend the budget.
            return {"id": self.settings.hosted.model, "thinking": False, "checked": False, "available": None,
                    "digest": None}
        base: dict[str, JsonValue] = {"id": self.settings.model, "thinking": self.settings.model_think}
        if self.settings.model_mode != ModelMode.LOCAL.value:
            return {**base, "checked": False, "available": None, "digest": None}
        with self._lock:
            cached = self._health
        if cached is not None and time.monotonic() - cached[0] < HEALTH_CACHE_SECONDS:
            return cached[1]
        try:
            digest = (self.config.model_probe or self._ollama_digest)()
            result: dict[str, JsonValue] = {**base, "checked": True, "available": True, "digest": digest}
        except diagnosis.ModelCallError as unavailable:
            # /health needs no session, and the message can quote the model endpoint's reply, so it goes to the console
            # instead. A repr, so control characters in the reply cannot forge log lines; a warning, because uvicorn
            # configures only its own loggers, and Python's last-resort handler prints ours from WARNING up.
            logger.warning("the model probe for /health failed: %r", str(unavailable))
            result = {**base, "checked": True, "available": False, "digest": None}
        with self._lock:
            self._health = (time.monotonic(), result)
        return result

    def _ollama_digest(self) -> str:
        client = diagnosis.OllamaClient(base_url=self.settings.ollama_url, model=self.settings.model)
        try:
            return client.model_digest()
        finally:
            client.close()

    # ----------------------------------------------------------------------- diagnosis jobs

    def accepted_modes(self) -> tuple[ModelMode, ...]:
        """The modes a diagnosis request may name: every mode, but hosted only when its settings are complete."""
        return tuple(mode for mode in ModelMode if mode is not ModelMode.HOSTED or self.settings.hosted.configured)

    def start_diagnosis(self, run_id: str, incident_id: str, mode: ModelMode, cutoff_ms: int) -> DiagnosisJob:
        with self._lock:
            busy = next((job for job in self._jobs.values() if job.status in ("queued", "running")), None)
            if busy is not None:
                raise DiagnosisBusyError(busy.job_id)
            job = DiagnosisJob(job_id=self._new_job_id(), run_id=run_id, incident_id=incident_id, mode=mode,
                               cutoff_ms=cutoff_ms, created_wall_ms=storage.wall_clock_ms())
            self._jobs[job.job_id] = job
        self._inference.submit(self._run_diagnosis, job.job_id)
        return job

    def job(self, run_id: str, job_id: str) -> DiagnosisJob:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None and self._issued_here(job_id):
            raise JobNotKeptError(
                f"diagnosis job {job_id!r} has finished and is no longer kept: this server keeps its newest "
                f"{MAX_FINISHED_JOBS} finished jobs; the run's diagnoses list holds its result"
            )
        if job is None or job.run_id != run_id:
            raise ScopeError(f"diagnosis job {job_id!r} is not part of run {run_id!r}")
        return job

    def _new_job_id(self) -> str:
        nonce = secrets.token_bytes(JOB_NONCE_BYTES)
        return f"job_{nonce.hex()}{self._job_tag(nonce).hex()}"

    def _issued_here(self, job_id: str) -> bool:
        """Whether this server issued ``job_id``. A made-up ID passes for one once in 65,536 tries, and then only
        answers 404 instead of 403: no job is reachable through it."""
        found = ISSUED_JOB_ID.fullmatch(job_id)
        if found is None:
            return False
        return hmac.compare_digest(self._job_tag(bytes.fromhex(found[1])), bytes.fromhex(found[2]))

    def _job_tag(self, nonce: bytes) -> bytes:
        return hmac.digest(self._job_key, nonce, "sha256")[:JOB_TAG_BYTES]

    def _keep_newest_finished(self, job_id: str) -> None:
        """Under self._lock, when ``job_id`` has just finished: drop the oldest finished jobs past MAX_FINISHED_JOBS.
        Every finished job's diagnosis is stored; only its job document and packet go. A live job is never dropped."""
        self._finished_jobs.append(job_id)
        while len(self._finished_jobs) > MAX_FINISHED_JOBS:
            del self._jobs[self._finished_jobs.popleft()]

    def _mark_running(self, job_id: str) -> DiagnosisJob:
        with self._lock:
            current = self._jobs[job_id]
            job = current.advanced("running", diagnosis_id=current.diagnosis_id, error=current.error,
                                   packet=current.packet, finished_wall_ms=current.finished_wall_ms)
            self._jobs[job_id] = job
        return job

    def _mark_failed(self, job_id: str, error: str) -> None:
        with self._lock:
            current = self._jobs[job_id]
            self._jobs[job_id] = current.advanced("failed", diagnosis_id=current.diagnosis_id, error=error,
                                                  packet=current.packet, finished_wall_ms=storage.wall_clock_ms())
            self._keep_newest_finished(job_id)

    def _mark_done(self, job_id: str, diagnosis_id: str, packet_document: dict[str, JsonValue]) -> None:
        with self._lock:
            current = self._jobs[job_id]
            self._jobs[job_id] = current.advanced("done", diagnosis_id=diagnosis_id, error=current.error,
                                                  packet=packet_document, finished_wall_ms=storage.wall_clock_ms())
            self._keep_newest_finished(job_id)

    def _run_diagnosis(self, job_id: str) -> None:
        """Worker thread: its own connection, and no transaction held while the model thinks."""
        job = self._mark_running(job_id)
        hosted: hosted_model.HostedClient | None = None
        try:
            client = (self.config.model_client_factory()
                      if self.config.model_client_factory is not None and job.mode is ModelMode.LOCAL else None)
            if job.mode is ModelMode.HOSTED:  # its calls count against this server's one daily budget
                hosted = hosted_model.client_from_settings(self.settings, self.hosted_budget)
            with self.connection() as conn:
                deployed = runtime.active_merchant(conn, run_id=job.run_id, at_ms=job.cutoff_ms)
                stored = diagnosis_store.diagnose_incident(
                    conn, run_id=job.run_id, incident_id=job.incident_id, mode=job.mode, settings=self.settings,
                    deployed=deployed, client=client, cutoff_ms=job.cutoff_ms, recordings_dir=self.recordings_dir,
                    hosted=hosted,
                )
                built = packet.build_packet(conn, run_id=job.run_id, incident_id=job.incident_id, cutoff_ms=job.cutoff_ms,
                                            deployed=deployed, persist=False)
        except WorkbenchError as refused:  # e.g. no recording for this packet, or no rules baseline
            self._mark_failed(job_id, self.public_detail(refused))
            return
        except Exception:  # a bug: keep the job answerable and put the traceback on the server console
            logger.exception("diagnosis job %s failed unexpectedly", job_id)
            self._mark_failed(job_id, "the diagnosis failed unexpectedly; see the server console")
            return
        finally:
            if hosted is not None:
                hosted.close()
        if built.packet_hash() != stored.packet_hash:
            logger.error("diagnosis %s used packet %s but the rebuild gave %s", stored.diagnosis_id, stored.packet_hash,
                         built.packet_hash())
        self._mark_done(job_id, stored.diagnosis_id, built.model_dump(mode="json"))

    def shutdown(self) -> None:
        self._inference.shutdown(wait=False, cancel_futures=True)


# --------------------------------------------------------------------------- application


def current_operator(request: Request) -> OperatorSession:
    """The operator behind this request's session cookie; a missing or unknown session is refused (403)."""
    workbench: Workbench = request.app.state.workbench
    session = workbench.session(request.cookies.get(workbench.session_cookie))
    if session is None and workbench.public is not None:  # public mode never prints its token
        raise ScopeError("an operator session is required; log in with the demo token you were given")
    if session is None:
        raise ScopeError("an operator session is required; log in with the operator token the server printed")
    return session


Operator = Annotated[OperatorSession, Depends(current_operator)]


def create_app(config: ApiConfig, configured: settings.Settings | None = None) -> FastAPI:
    workbench = Workbench(config, configured or settings.load_settings())
    demo_runs: public_template.DemoRuns | None = None
    if config.public is None:
        public_mode.refuse_marked_data_dir(config.data_dir)  # public mode would empty it at its next start
        config.data_dir.mkdir(parents=True, exist_ok=True)
    else:
        # The demo runs the image holds (Q48): the template is checked before the data directory is touched.
        template = (None if config.public.template_dir is None
                    else public_template.plan(config.public.template_dir, config.data_dir))
        public_mode.claim_data_dir(config.data_dir)  # every public-mode process starts from an empty data directory
        if template is not None:
            demo_runs = public_template.install(template, config.data_dir, seeds=config.public.seeds,
                                                model_mode=ModelMode(workbench.settings.model_mode))
    with workbench.connection() as conn:  # the API seeds runs, so it needs the simulator's tables too
        storage.migrate_simulator(conn)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        workbench.shutdown()

    app = FastAPI(title="RayTrace (synthetic sandbox)", version=metadata.version(APP_NAME),
                  docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.workbench = workbench
    app.state.demo_runs = demo_runs
    # Before the guard, so the guard runs first; _BodyLimit says why. Public mode has no webhook route to exempt.
    app.add_middleware(_BodyLimit, webhook_exempt=config.public is None)
    _install_request_guard(app, config, workbench)
    _install_error_handlers(app, workbench)
    _install_session_routes(app, workbench)
    _install_run_routes(app, workbench, config)
    _install_diagnosis_routes(app, workbench)
    _install_recovery_routes(app, workbench, config)
    _install_queue_routes(app, workbench)
    if config.public is None:  # on a public link anyone could post signed deliveries to a computable run ID
        _install_webhook_routes(app, workbench)
    _mount_ui(app, config.ui_dir)
    return app


# --------------------------------------------------------------------------- routes


def _install_session_routes(app: FastAPI, workbench: Workbench) -> None:
    @app.get("/health")
    def health() -> dict[str, JsonValue]:
        return workbench.health()

    @app.get("/api/session")
    def session_state(request: Request) -> dict[str, JsonValue]:
        session = workbench.session(request.cookies.get(workbench.session_cookie))
        return {"operator_id": None if session is None else session.operator_id}

    @app.post("/api/session")
    def login(body: LoginRequest, request: Request, response: Response) -> dict[str, JsonValue]:
        session_id, session = workbench.login(body.token, body.operator_id,
                                              presented=request.cookies.get(workbench.session_cookie))
        if workbench.public is not None:  # served over HTTPS: Secure, and the __Host- name binds it to this one host
            response.set_cookie(workbench.session_cookie, session_id, httponly=True, samesite="strict", path="/",
                                secure=True)
            return {"operator_id": session.operator_id}
        response.set_cookie(SESSION_COOKIE, session_id, httponly=True, samesite="strict", path="/")  # NOSONAR(S2092) plain-HTTP server that main() binds to 127.0.0.1 only; browsers that do not treat http loopback as secure drop Secure cookies, and the cookie is already HttpOnly and SameSite=Strict
        return {"operator_id": session.operator_id}

    @app.delete("/api/session")
    def logout(request: Request, response: Response, _: Operator) -> dict[str, JsonValue]:
        workbench.logout(request.cookies.get(workbench.session_cookie))
        # A browser ignores a __Host- cookie, even one that removes it, unless it is Secure.
        response.delete_cookie(workbench.session_cookie, path="/", httponly=True, samesite="strict",
                               secure=workbench.public is not None)
        return {"operator_id": None}


def _install_run_routes(app: FastAPI, workbench: Workbench, config: ApiConfig) -> None:
    @app.get("/api/scenarios")
    def list_scenarios(_: Operator) -> dict[str, JsonValue]:
        return {"scenarios": sorted(scenarios.DEMO_SCENARIOS), "execution_mode": workbench.settings.model_mode,
                "model_modes": [mode.value for mode in workbench.accepted_modes()],
                "model_source": workbench.model_source.document()}  # the page reads its model source here

    @app.get("/api/fixes")
    def list_fixes(_: Operator) -> list[JsonValue]:
        return [option.model_dump(mode="json") for option in fixes.available_fixes()]

    @app.get("/api/runs")
    def list_runs(_: Operator) -> list[JsonValue]:
        with workbench.connection() as conn:
            rows = conn.execute("SELECT run_id FROM runs ORDER BY created_wall_ms DESC, run_id").fetchall()
            return [_run_document(conn, str(row["run_id"])) for row in rows]

    @app.post("/api/runs", status_code=201)
    def create_run(body: CreateRunRequest, request: Request, response: Response, _: Operator) -> dict[str, JsonValue]:
        if config.public is None:
            return _seed_run(workbench, config, body)
        document, created = _seed_public_run(workbench, config.public, body,
                                             request.cookies.get(workbench.session_cookie))
        if not created:
            response.status_code = 200  # viewers share runs, so a run that exists already is opened, not refused
        return document

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: RecordId, _: Operator) -> dict[str, JsonValue]:
        with workbench.connection() as conn:
            return _run_document(conn, run_id)

    @app.post("/api/runs/{run_id}/advance")
    def advance(run_id: RecordId, body: AdvanceRequest, _: Operator) -> dict[str, JsonValue]:
        with workbench.connection() as conn:
            with workbench.clock_lock, storage.write_transaction(conn):
                run = storage.require_run(conn, run_id)
                storage.advance_clock(conn, run_id, run.clock_ms + body.by_ms)
            return _run_document(conn, run_id)

    @app.get("/api/runs/{run_id}/incidents")
    def incidents(run_id: RecordId, _: Operator) -> dict[str, JsonValue]:
        with workbench.connection() as conn:
            return {**cli.status_report(conn, run_id), "definitions": dict(METRIC_DEFINITIONS)}

    @app.get("/api/runs/{run_id}/payment-state")
    def payment_state(run_id: RecordId, _: Operator) -> dict[str, JsonValue]:
        """Late authorisations as counted facts, and the payments a read-only refresh could settle."""
        with workbench.connection() as conn:
            facts = cli.late_authorisation_facts(conn, run_id,
                                                 stale_after_ms=workbench.settings.stale_after_seconds * 1000)
            return {"late_authorisations": facts.as_json(),
                    "refresh_candidates": list(cli.refresh_candidates(conn, run_id))}


def _install_diagnosis_routes(app: FastAPI, workbench: Workbench) -> None:
    @app.post("/api/runs/{run_id}/diagnoses", status_code=202)
    def start_diagnosis(run_id: RecordId, body: DiagnoseRequest, _: Operator) -> dict[str, JsonValue]:
        return _start_diagnosis_job(workbench, run_id, body)

    @app.get("/api/runs/{run_id}/diagnoses")
    def list_diagnoses(run_id: RecordId, _: Operator) -> list[JsonValue]:
        with workbench.connection() as conn:
            storage.require_run(conn, run_id)
            return [cli.diagnosis_document(stored) for stored in diagnosis_store.list_diagnoses(conn, run_id=run_id)]

    @app.get("/api/runs/{run_id}/diagnoses/{job_id}")
    def get_diagnosis(run_id: RecordId, job_id: RecordId, _: Operator) -> dict[str, JsonValue]:
        return _job_document(workbench, workbench.job(run_id, job_id))


def _install_recovery_routes(app: FastAPI, workbench: Workbench, config: ApiConfig) -> None:
    # Each operator action below steps the replay clock (cli.next_operator_time, or a refresh's own step)
    # and works at that time, so it runs under clock_lock; one operator makes the wait for webhooks short.
    @app.post("/api/runs/{run_id}/fixes/{fix_id}/validate")
    def validate_fix(run_id: RecordId, fix_id: RecordId, body: ValidateFixRequest, session: Operator) -> dict[str, JsonValue]:
        with workbench.connection() as conn, workbench.clock_lock:
            validation = cli.validate_fix(conn, run_id=run_id, fix_id=fix_id, diagnosis_id=body.diagnosis_id,
                                          operator=session.operator_id)
        return {**cli.validation_document(validation), "operator_id": validation.operator_id}

    @app.post("/api/runs/{run_id}/plans")
    def create_plan(run_id: RecordId, body: PlanRequest, session: Operator) -> dict[str, JsonValue]:
        with workbench.connection() as conn, workbench.clock_lock:
            return _plan_document_for(conn, workbench, run_id, body, session)

    @app.post("/api/runs/{run_id}/plans/{plan_id}/approve")
    def approve(run_id: RecordId, plan_id: RecordId, body: ApproveRequest, session: Operator) -> dict[str, JsonValue]:
        with workbench.connection() as conn, workbench.clock_lock:
            approval = cli.approve_plan(conn, run_id=run_id, plan_id=plan_id, plan_hash=body.plan_hash,
                                        operator=session.operator_id,
                                        ttl_ms=workbench.settings.approval_ttl_seconds * 1000)
        return approval.model_dump(mode="json")

    @app.post("/api/runs/{run_id}/plans/{plan_id}/execute")
    def execute(run_id: RecordId, plan_id: RecordId, _: Operator) -> dict[str, JsonValue]:
        with workbench.connection() as conn, workbench.clock_lock:
            result = cli.execute_plan(conn, run_id=run_id, plan_id=plan_id,
                                      stale_after_ms=workbench.settings.stale_after_seconds * 1000)
        return result.model_dump(mode="json")

    @app.post("/api/runs/{run_id}/refresh")
    def refresh_provider(run_id: RecordId, body: RefreshRequest, _: Operator) -> dict[str, JsonValue]:
        with workbench.connection() as conn, workbench.clock_lock:
            summary = cli.refresh_payments(conn, config.data_dir, run_id=run_id, payment_ids=body.payment_ids)
        return _refresh_document(summary)

    @app.get("/api/runs/{run_id}/report")
    def export_report(run_id: RecordId, _: Operator) -> dict[str, JsonValue]:
        with workbench.connection() as conn:
            return report.build_report(conn, run_id=run_id)

    @app.get("/api/runs/{run_id}/actions/{action_id}/receipt")
    def action_receipt(
        run_id: RecordId, action_id: RecordId, _: Operator,
        output: Annotated[Literal["json", "markdown"], Query(alias="format")] = "json",
    ) -> Response:
        """One reprocess action's receipt: the exact bytes the CLI's receipt command writes, JSON or Markdown."""
        with workbench.connection() as conn:
            receipt = report.build_receipt(conn, run_id=run_id, action_id=action_id)
        if output == "markdown":
            return PlainTextResponse(report.render_receipt_markdown(receipt), media_type="text/markdown; charset=utf-8")
        return Response(report.receipt_bytes(receipt), media_type="application/json")


# --------------------------------------------------------------------------- the order recovery queue (read only)

TimelineStage = Literal["order", "provider", "delivery", "inbox", "worker"]
TimelineKind = Literal[
    "order_created", "payment_attempt", "provider_observation", "delivery_attempt", "inbox_event",
    "conflicting_delivery", "worker_attempt", "fulfilment", "order_cancelled",
]
# Each kind's stage. The order of the keys places records the workbench recorded at the same replay time:
# the order and its attempts, then provider, delivery and inbox, worker, and what happened to the order.
TIMELINE_STAGES: dict[TimelineKind, TimelineStage] = {
    "order_created": "order",
    "payment_attempt": "order",
    "provider_observation": "provider",
    "delivery_attempt": "delivery",
    "inbox_event": "inbox",
    "conflicting_delivery": "inbox",
    "worker_attempt": "worker",
    "fulfilment": "order",
    "order_cancelled": "order",
}
_TIMELINE_RANK: dict[str, int] = {kind: rank for rank, kind in enumerate(TIMELINE_STAGES)}


class QueueRow(StrictModel):
    """One order in the queue list; the case detail carries the whole record."""

    order_id: str
    order_known: bool
    amount_paise: int | None
    currency: str | None
    amount_status: AmountStatus
    state: CaseState
    next_step: NextStep
    blockers: tuple[str, ...]
    age_ms: int | None  # QUEUE_DEFINITIONS["age_ms"]
    freshness_ms: int | None  # QUEUE_DEFINITIONS["freshness_ms"]
    newest_observation_at_ms: int | None


class TimelineEntry(StrictModel):
    """One record the case relied on, at the replay time the workbench recorded it."""

    at_ms: int  # when it was created, known, received, finished, fulfilled or cancelled, as its kind has it
    stage: TimelineStage
    kind: TimelineKind
    record_id: str  # the record's own ID in its table; the kind names the table
    payment_id: str | None
    facts: dict[str, JsonValue]  # the record's own fields, without the status columns a later step rewrites
    text: str | None = None  # untrusted free text (a worker stack trace): shown as text, never followed


class CaseReceipt(StrictModel):
    """A reprocess action executed by the queue's time whose plan targeted the order, and where its receipt is."""

    action_id: str
    plan_id: str
    status: ActionStatus
    executed_at_ms: int
    order_result: str | None  # completed, no_op, refused or failed, from the action audit; None without an audit
    order_reasons: tuple[str, ...]
    receipt_path: str  # the receipt route: JSON, or Markdown with ?format=markdown


@dataclass(frozen=True)
class QueueFilter:
    """Which cases a queue page lists. No filter admits every case, and the totals never depend on it."""

    states: frozenset[CaseState]
    next_steps: frozenset[NextStep]
    blocker: str | None

    def __post_init__(self) -> None:
        if self.blocker is not None and self.blocker not in order_cases.BLOCKER_ORDER:
            raise ContractError(f"blocker must be one of {list(order_cases.BLOCKER_ORDER)}, got {self.blocker!r}")

    def admits(self, case: order_cases.OrderCase) -> bool:
        return ((not self.states or case.state in self.states)
                and (not self.next_steps or case.next_step in self.next_steps)
                and (self.blocker is None or self.blocker in case.blockers))

    def document(self) -> dict[str, JsonValue]:
        return {"state": [item.value for item in CaseState if item in self.states],
                "next_step": [item.value for item in NextStep if item in self.next_steps], "blocker": self.blocker}


@dataclass(frozen=True)
class QueueRead:
    """The queue at one replay time, with the run's clock and the orders' creation times from the same snapshot."""

    queue: order_cases.OrderQueue
    clock_ms: int
    created_at_ms: Mapping[str, int]  # order ID -> creation time, for every order of the run

    def row(self, case: order_cases.OrderCase) -> QueueRow:
        created = self.created_at_ms.get(case.order_id) if case.order_known else None
        return QueueRow(
            order_id=case.order_id, order_known=case.order_known, amount_paise=case.amount_paise, currency=case.currency,
            amount_status=case.amount_status, state=case.state, next_step=case.next_step, blockers=case.blockers,
            age_ms=None if created is None else self.queue.as_of_ms - created, freshness_ms=case.freshness.age_ms,
            newest_observation_at_ms=case.freshness.newest_observation_at_ms,
        )

    def header(self) -> dict[str, JsonValue]:
        queue = self.queue
        return {"run_id": queue.run_id, "as_of_ms": queue.as_of_ms, "replay_clock_ms": self.clock_ms,
                "stale_after_ms": queue.stale_after_ms, "classifier_version": queue.classifier_version,
                "refund_flag_read": queue.refund_flag_read}


def _install_queue_routes(app: FastAPI, workbench: Workbench) -> None:
    # Both routes only read, and no model is involved: they work with Ollama stopped.
    @app.get("/api/runs/{run_id}/orders")
    def order_queue(
        run_id: RecordId,
        _: Operator,
        as_of_ms: int | None = None,
        state: Annotated[list[CaseState] | None, Query()] = None,
        next_step: Annotated[list[NextStep] | None, Query()] = None,
        blocker: Annotated[str | None, Query(max_length=64)] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_QUEUE_PAGE_LIMIT)] = QUEUE_PAGE_LIMIT,
        after: Annotated[str | None, Query(min_length=1, max_length=128, pattern=rf"^{_RECORD_ID_CHARACTER}+$")] = None,
    ) -> dict[str, JsonValue]:
        """The queue at a replay time: whole-queue totals and coverage, then one page of rows in order-ID order."""
        chosen = QueueFilter(states=frozenset(state or ()), next_steps=frozenset(next_step or ()), blocker=blocker)
        with workbench.connection() as conn, _queue_snapshot(conn, workbench, run_id, as_of_ms) as read:
            return _queue_document(read, chosen, limit=limit, after=after)

    @app.get("/api/runs/{run_id}/orders/{order_id}")
    def order_case(run_id: RecordId, order_id: RecordId, _: Operator, as_of_ms: int | None = None) -> dict[str, JsonValue]:
        """One case: its record, its provider -> delivery/inbox -> worker -> order timeline, and its receipts."""
        with workbench.connection() as conn, _queue_snapshot(conn, workbench, run_id, as_of_ms) as read:
            case = next((item for item in read.queue.cases if item.order_id == order_id), None)
            if case is None:
                raise OrderNotInQueueError(
                    f"order {order_id!r} is not in run {run_id!r}'s order queue at replay time {read.queue.as_of_ms}: "
                    "the run has no such order, or no evidence of it was known by then; list the queue for order IDs"
                )
            return _case_document(conn, read, case)


@contextmanager
def _queue_snapshot(
    conn: sqlite3.Connection, workbench: Workbench, run_id: str, requested_ms: int | None
) -> Iterator[QueueRead]:
    """The run's queue at a replay time, inside one read snapshot the caller may go on reading.

    The snapshot begins with the clock read, under ``clock_lock``. A webhook delivery holds that lock from its clock
    step to its receipt's commit, so the snapshot holds every record known by that clock and none received after
    it, and the queue's refund-flag rule reads the same clock. Without ``requested_ms`` the queue is at the clock;
    an earlier time is allowed, and a later one is refused, because nothing past the clock has happened yet.
    """
    with storage.read_transaction(conn):
        with workbench.clock_lock:
            clock_ms = storage.require_run(conn, run_id).clock_ms
        as_of_ms = clock_ms if requested_ms is None else requested_ms
        if not 0 <= as_of_ms <= clock_ms:
            raise ContractError(
                f"as_of_ms must be a replay time from 0 to run {run_id!r}'s replay clock {clock_ms}, got {as_of_ms}; "
                "leave it out for the queue at the clock"
            )
        queue = order_cases.classify_orders(conn, run_id=run_id, as_of_ms=as_of_ms,
                                            stale_after_ms=workbench.settings.stale_after_seconds * 1000)
        created = {order.order_id: order.created_at_ms for order in storage.list_orders(conn, run_id=run_id)}
        yield QueueRead(queue=queue, clock_ms=clock_ms, created_at_ms=created)


def _queue_document(read: QueueRead, chosen: QueueFilter, *, limit: int, after: str | None) -> dict[str, JsonValue]:
    queue = read.queue
    matching = [case for case in queue.cases if chosen.admits(case)]
    remaining = [case for case in matching if after is None or case.order_id > after]  # cases are in order-ID order
    page = remaining[:limit]
    return {
        **read.header(),
        "filters": chosen.document(),
        "totals": queue.totals.model_dump(mode="json"),
        "coverage": queue.coverage.model_dump(mode="json"),
        "states": [
            {"state": rule.state.value, "next_step": order_cases.NEXT_STEP[rule.state].value,
             "condition": rule.condition, "unresolved": rule.state in order_cases.UNRESOLVED_STATES}
            for rule in order_cases.STATE_PRECEDENCE
        ],
        "matching_orders": len(matching),
        "orders": [read.row(case).model_dump(mode="json") for case in page],
        "next_after": page[-1].order_id if len(remaining) > limit else None,
        "definitions": dict(QUEUE_DEFINITIONS),
    }


def _case_document(conn: sqlite3.Connection, read: QueueRead, case: order_cases.OrderCase) -> dict[str, JsonValue]:
    run_id, as_of_ms = read.queue.run_id, read.queue.as_of_ms
    condition = next(rule.condition for rule in order_cases.STATE_PRECEDENCE if rule.state is case.state)
    return {
        **read.header(),
        "summary": read.row(case).model_dump(mode="json"),
        "case": case.model_dump(mode="json"),
        "state_condition": condition,
        "timeline": [entry.model_dump(mode="json") for entry in _case_timeline(conn, run_id, case, as_of_ms)],
        "receipts": [item.model_dump(mode="json") for item in _case_receipts(conn, run_id, case.order_id, as_of_ms)],
        "definitions": dict(QUEUE_DEFINITIONS),
    }


def _timeline_entry(
    kind: TimelineKind, at_ms: int, record_id: str | int, payment_id: str | None, facts: dict[str, JsonValue],
    text: str | None = None,
) -> TimelineEntry:
    return TimelineEntry(at_ms=at_ms, stage=TIMELINE_STAGES[kind], kind=kind, record_id=str(record_id),
                         payment_id=payment_id, facts=facts, text=text)


def _case_timeline(
    conn: sqlite3.Connection, run_id: str, case: order_cases.OrderCase, as_of_ms: int
) -> list[TimelineEntry]:
    """Every record the case's evidence names, as of the queue's time, in the order the workbench recorded them.

    Columns a later step rewrites (an inbox row's processing status, attempts and last error; a held delivery's
    review status) are left out, because they may describe what happened after ``as_of_ms``; the worker attempts
    finished by then say what processing did.
    """
    entries = [
        *_order_entries(conn, run_id, case, as_of_ms),
        *_provider_entries(conn, run_id, case, as_of_ms),
        *_delivery_entries(conn, run_id, case),
        *_inbox_entries(conn, run_id, case),
        *_conflict_entries(conn, run_id, case, as_of_ms),
    ]
    # Stable: records of one kind recorded at one time keep the order their table lists them in.
    return sorted(entries, key=lambda entry: (entry.at_ms, _TIMELINE_RANK[entry.kind]))


def _order_entries(
    conn: sqlite3.Connection, run_id: str, case: order_cases.OrderCase, as_of_ms: int
) -> list[TimelineEntry]:
    """The order's creation and dated cancellation, its payments' attempt rows and its fulfilment."""
    entries: list[TimelineEntry] = []
    order = storage.get_order(conn, run_id=run_id, order_id=case.order_id) if case.order_known else None
    if order is not None:
        entries.append(_timeline_entry("order_created", order.created_at_ms, order.order_id, None,
                                       {"amount_paise": order.amount_paise, "currency": order.currency}))
        if order.cancelled_at_ms is not None and order.cancelled_at_ms <= as_of_ms:
            entries.append(_timeline_entry("order_cancelled", order.cancelled_at_ms, order.order_id, None, {}))
    attempts = storage.get_payment_attempts(conn, run_id=run_id, payment_ids=case.evidence.payment_ids, as_of_ms=as_of_ms)
    entries.extend(
        _timeline_entry("payment_attempt", attempt.created_at_ms, attempt.payment_id, attempt.payment_id,
                        {"order_id": attempt.order_id, "method": attempt.method})
        for attempt in attempts
    )
    fulfilment = case.fulfilment
    if fulfilment is not None:
        entries.append(_timeline_entry("fulfilment", fulfilment.fulfilled_at_ms, fulfilment.order_id,
                                       fulfilment.payment_id, fulfilment.model_dump(mode="json", exclude={"run_id"})))
    return entries


def _provider_entries(
    conn: sqlite3.Connection, run_id: str, case: order_cases.OrderCase, as_of_ms: int
) -> list[TimelineEntry]:
    wanted = frozenset(case.evidence.observation_ids)
    if not wanted:
        return []
    observations = storage.list_observations(conn, run_id=run_id, as_of_ms=as_of_ms,
                                             payment_ids=case.evidence.payment_ids)
    return [
        _timeline_entry("provider_observation", item.known_at_ms, item.observation_id, item.payment_id,
                        item.model_dump(mode="json", exclude={"run_id", "observation_id", "payment_id"}))
        for item in observations if item.observation_id in wanted
    ]


def _delivery_entries(conn: sqlite3.Connection, run_id: str, case: order_cases.OrderCase) -> list[TimelineEntry]:
    wanted = frozenset(case.evidence.delivery_attempt_ids)
    if not wanted:
        return []
    return [
        _timeline_entry("delivery_attempt", item.received_at_ms, item.id, None,
                        item.model_dump(mode="json", exclude={"run_id", "id"}))
        for item in storage.list_delivery_attempts(conn, run_id=run_id) if item.id in wanted
    ]


def _inbox_entries(conn: sqlite3.Connection, run_id: str, case: order_cases.OrderCase) -> list[TimelineEntry]:
    """The durable events naming the order, each followed by the worker attempts on it that the case counted."""
    attempts_wanted = frozenset(case.evidence.worker_attempt_ids)
    entries: list[TimelineEntry] = []
    for inbox_id in case.evidence.inbox_ids:
        row = storage.get_inbox_row(conn, run_id=run_id, inbox_id=inbox_id)
        if row is None:  # the classification read it in this same snapshot, and the inbox is append-only
            raise storage.StorageError(f"inbox row {inbox_id} of run {run_id!r} vanished within one read snapshot")
        payment_id = ingestion.parse_payment_event(row.raw_body).payment.id
        entries.append(_timeline_entry("inbox_event", row.received_at_ms, row.inbox_id, payment_id, {
            "integration_id": row.integration_id, "event_id": row.event_id, "event_type": row.event_type,
            "body_sha256": row.body_sha256, "signature_result": row.signature_result,
        }))
        entries.extend(
            _timeline_entry("worker_attempt", attempt.finished_at_ms, attempt.id, payment_id,
                            attempt.model_dump(mode="json", exclude={"run_id", "id", "stack_trace"}), attempt.stack_trace)
            for attempt in storage.list_worker_attempts(conn, run_id=run_id, inbox_id=inbox_id)
            if attempt.id in attempts_wanted
        )
    return entries


def _conflict_entries(
    conn: sqlite3.Connection, run_id: str, case: order_cases.OrderCase, as_of_ms: int
) -> list[TimelineEntry]:
    wanted = frozenset(case.evidence.conflicting_delivery_ids)
    if not wanted:
        return []
    return [
        _timeline_entry("conflicting_delivery", conflict.received_at_ms, conflict.id, conflict.payment_id,
                        conflict.model_dump(mode="json", exclude={"run_id", "id", "raw_body", "review_status"}))
        for conflict in storage.list_conflicting_deliveries(conn, run_id=run_id, as_of_ms=as_of_ms)
        if conflict.id in wanted
    ]


def _case_receipts(conn: sqlite3.Connection, run_id: str, order_id: str, as_of_ms: int) -> list[CaseReceipt]:
    """Every reprocess action executed by ``as_of_ms`` whose plan targeted the order, oldest first.

    Receipts cover reprocess actions only (``report.build_receipt``): an escalation changes no business state,
    and a provider replay is instructions, with no action.
    """
    receipts: list[CaseReceipt] = []
    for action in executor.list_actions(conn, run_id=run_id):
        if action.executed_at_ms > as_of_ms:
            continue
        plan = plans.require_plan(conn, run_id=run_id, plan_id=action.plan_id)
        if plan.operation is not Operation.REPROCESS_INBOX or order_id not in plan.target_ids:
            continue
        audit = executor.get_action_audit(conn, run_id=run_id, action_id=action.action_id)
        executed = None if audit is None else next((item for item in audit.orders if item.order_id == order_id), None)
        receipts.append(CaseReceipt(
            action_id=action.action_id, plan_id=plan.plan_id, status=action.status,
            executed_at_ms=action.executed_at_ms, order_result=None if executed is None else executed.result,
            order_reasons=() if executed is None else executed.reasons,
            receipt_path=f"/api/runs/{quote(run_id, safe='')}/actions/{quote(action.action_id, safe='')}/receipt",
        ))
    return receipts


def _install_webhook_routes(app: FastAPI, workbench: Workbench) -> None:
    @app.post("/api/runs/{run_id}/webhooks/{integration_id}")
    async def webhook_delivery(run_id: RecordId, integration_id: RecordId, request: Request) -> Response:
        """Verify and durably receive one simulated provider delivery (SPEC §11), as ``driver.deliver`` does.

        SPEC names ``/api/webhooks/{integration_id}``; the run is in the path because every run uses the
        same integration ID and all storage is run-scoped. The HMAC signature over the raw body
        authenticates the sender, so there is no operator session. Every request that reaches the
        database, rejected or not, moves the run's replay clock one millisecond forward and is received
        at that new time; one refused by the rate cap or the request guard leaves the clock alone, and
        so does one whose sender disconnects before its body has arrived.
        This route only receives: an accepted event waits in the inbox as ``pending``, and nothing
        here runs the worker.
        """
        # Admission stays before the body read and is not given back if the sender then disconnects:
        # the cap bounds the work requests cost, and a disconnected one has already cost some.
        workbench.admit_webhook_delivery()
        try:
            raw_body = await _read_capped_body(request)
        except ClientDisconnect:
            logger.warning("the sender of a webhook delivery for run %s, integration %s disconnected before its "
                           "body arrived; nothing was received", run_id, integration_id)
            # Nobody is left to read an answer. 400 is what FastAPI gives when a body read fails on its routes.
            return Response(status_code=400)
        delivered = await run_in_threadpool(
            _receive_delivery, workbench, run_id=run_id, integration_id=integration_id, headers=request.headers,
            raw_body=raw_body,
        )
        response = JSONResponse(_delivery_document(delivered), status_code=delivered.http_status)
        if len(raw_body) > ingestion.MAX_BODY_BYTES_CEILING:
            # The read stopped at the ceiling while the sender may still be sending. On a connection kept
            # alive, the server would go on reading the rest of the body only to discard it.
            response.headers["Connection"] = "close"
        return response


async def _read_capped_body(request: Request) -> bytes:
    """The raw body, streamed, stopping once it holds one byte more than the receiver's ceiling.

    ``request.body()`` would read everything the sender sends. One byte past the ceiling is enough
    for the receiver to refuse the body as too large and record that it did.
    """
    limit = ingestion.MAX_BODY_BYTES_CEILING + 1
    held = bytearray()
    async with aclosing(request.stream()) as chunks:
        async for chunk in chunks:
            held += chunk[: limit - len(held)]
            if len(held) >= limit:
                break
    return bytes(held)


def _receive_delivery(
    workbench: Workbench, *, run_id: str, integration_id: str, headers: Mapping[str, str], raw_body: bytes
) -> ingestion.DeliveryResponse:
    """One HTTP delivery through the in-process path of ``driver.deliver``; blocking, so run it in a thread.

    The delivery is received strictly after the run's replay clock. The clock moves one receipt
    step in its own transaction, the merchant receiver deployed at that new time prepares the bytes
    the signature is checked against, and ingestion receives them at that time. ``clock_lock`` is
    held from the step through the receipt's commit, so no evidence time can be fixed in between.
    The receiver prepares only a body that passes ingestion's size and nesting limits
    (``_receiver_may_read``); ingestion refuses any other body as it was sent, as it would behind the
    baseline receiver. A storage failure anywhere here means nothing was acknowledged, so it becomes
    ``DeliveryNotStoredError`` (503); an unknown run is ``UnknownRunError`` (404), with nothing
    recorded, because there is no run to record it in.
    """
    try:
        with workbench.connection() as conn, workbench.clock_lock:
            received_at_ms = _step_clock_for_receipt(conn, run_id)
            verified_body = (
                _prepared_by_receiver(conn, run_id=run_id, at_ms=received_at_ms, raw_body=raw_body)
                if _receiver_may_read(conn, run_id=run_id, integration_id=integration_id, raw_body=raw_body)
                else raw_body
            )
            return ingestion.receive_webhook(
                conn, run_id=run_id, integration_id=integration_id, headers=headers, raw_body=verified_body,
                received_at_ms=received_at_ms,
            )
    except (sqlite3.Error, storage.StorageError, ingestion.ReceiptNotDurableError) as storage_failure:
        # The traceback names the run, the event and the storage error; it stays on the server console.
        logger.exception("a webhook delivery for run %s was not stored", run_id)
        raise DeliveryNotStoredError(
            "the delivery was not stored and is not acknowledged; send it again later",
            retry_after_seconds=NOT_DURABLE_RETRY_AFTER_SECONDS,
        ) from storage_failure


def _step_clock_for_receipt(conn: sqlite3.Connection, run_id: str) -> int:
    """Move the run's replay clock one receipt step forward, in its own transaction, and return the new time."""
    with storage.write_transaction(conn):
        run = storage.get_run(conn, run_id)
        if run is None:
            raise UnknownRunError(run_id)
        return storage.advance_clock(conn, run_id, run.clock_ms + RECEIPT_CLOCK_STEP_MS)


def _receiver_may_read(conn: sqlite3.Connection, *, run_id: str, integration_id: str, raw_body: bytes) -> bool:
    """Whether the deployed receiver may prepare these unverified bytes: they pass ingestion's size and nesting limits.

    Ingestion applies both only to what the receiver prepares, too late for a receiver that parses and re-serialises
    the bytes: receiver@2 indents every level, so a body nested 900 deep grew about 900-fold under ``clock_lock``.
    Within both limits no line it writes is indented more than 64 spaces, so its output stays within about 34 times
    the input. Size comes first, so the nesting count never walks more than the limit. A body for an integration the
    run does not have has no limit to be within, and ingestion refuses it whatever the receiver would make of it.
    """
    integration = storage.get_integration(conn, run_id=run_id, integration_id=integration_id)
    if integration is None:
        return False
    within_size = len(raw_body) <= min(integration.max_body_bytes, ingestion.MAX_BODY_BYTES_CEILING)
    return within_size and not ingestion.nests_deeper_than(raw_body, ingestion.MAX_JSON_NESTING)


def _prepared_by_receiver(conn: sqlite3.Connection, *, run_id: str, at_ms: int, raw_body: bytes) -> bytes:
    """The bytes the merchant receiver deployed at ``at_ms`` checks the signature against."""
    receiver = runtime.active_merchant(conn, run_id=run_id, at_ms=at_ms)
    try:
        return receiver.body_for_verification(raw_body)
    except Exception as failed:  # merchant fixture code: whatever it raises, the deployed receiver failed
        logger.warning("receiver %s of run %s raised %s before verification; nothing was stored",
                       receiver.version, run_id, type(failed).__name__)
        raise ReceiverFailedError(
            "the deployed receiver failed before it could verify this delivery; nothing was stored"
        ) from failed


def _seed_run(workbench: Workbench, config: ApiConfig, body: CreateRunRequest) -> dict[str, JsonValue]:
    world = generate_world(scenarios.demo_scenario(body.scenario), body.seed)
    with workbench.seed_lock, workbench.connection() as conn:
        if storage.get_run(conn, world.run_id) is not None:
            raise RunExistsError(world.run_id)
        with workbench.clock_lock:  # the replay steps the new run's clock; a delivery for it waits until it ends
            summary = driver.seed_world(conn, world, oracle_dir=config.data_dir / cli.ORACLE_DIRECTORY,
                                        model_mode=ModelMode(workbench.settings.model_mode),
                                        stale_after_ms=workbench.settings.stale_after_seconds * 1000)
        return _run_document(conn, summary.run_id)


def _seed_public_run(
    workbench: Workbench, public: settings.PublicSettings, body: CreateRunRequest, session_id: str | None
) -> tuple[dict[str, JsonValue], bool]:
    """Public mode's seeding: the run's document, and whether this request created it.

    Viewers share runs (scope A), so a scenario and seed that already have a run open that run, which costs no quota;
    _seed_run answers 409 there instead. Only a published seed is taken, and a new run must pass the caps and the data
    limit. The world is generated under seed_lock, after those checks, so neither a run that exists nor a refused one
    costs a generation.
    """
    spec = scenarios.demo_scenario(body.scenario)
    if body.seed not in public.seeds:
        workbench.log_public(logging.WARNING, "quota_refused", reason="seed_not_offered", status=422, seed=body.seed)
        raise SeedNotOfferedError(f"seed {body.seed} is not offered on this demo; choose one of {sorted(public.seeds)}")
    run_id = run_id_for(spec, body.seed)
    with workbench.seed_lock, workbench.connection() as conn:
        if storage.get_run(conn, run_id) is not None:
            return _run_document(conn, run_id), False
        workbench.reserve_public_run(session_id, public)
        started = time.monotonic()
        world = generate_world(spec, body.seed)
        with workbench.clock_lock:  # the replay steps the new run's clock
            summary = driver.seed_world(conn, world, oracle_dir=workbench.config.data_dir / cli.ORACLE_DIRECTORY,
                                        model_mode=ModelMode(workbench.settings.model_mode),
                                        stale_after_ms=workbench.settings.stale_after_seconds * 1000)
        label, total = workbench.count_public_run(session_id)
        workbench.log_public(logging.INFO, "run_created", run=summary.run_id, scenario=spec.name, seed=body.seed,
                              session=label, runs_in_process=total, seconds=round(time.monotonic() - started, 1))
        return _run_document(conn, summary.run_id), True


def _start_diagnosis_job(workbench: Workbench, run_id: str, body: DiagnoseRequest) -> dict[str, JsonValue]:
    if body.mode is ModelMode.HOSTED and body.mode not in workbench.accepted_modes():
        raise ContractError(
            "hosted mode is not configured on this server: it needs WORKBENCH_HOSTED_URL, WORKBENCH_HOSTED_MODEL and "
            "WORKBENCH_HOSTED_API_KEY at start. Choose local, recorded or rules"
        )
    with workbench.connection() as conn:
        with workbench.clock_lock:  # every delivery not yet committed is received after this cutoff
            cutoff_ms = storage.require_run(conn, run_id).clock_ms
        incident_id = body.incident_id or cli.latest_incident(conn, run_id)
        if incident_id not in {item.incident_id for item in storage.list_incidents(conn, run_id=run_id)}:
            raise ScopeError(f"incident {incident_id!r} is not part of run {run_id!r}")
    mode = body.mode or ModelMode(workbench.settings.model_mode)
    return _job_document(workbench, workbench.start_diagnosis(run_id, incident_id, mode, cutoff_ms))


def _plan_document_for(
    conn: sqlite3.Connection, workbench: Workbench, run_id: str, body: PlanRequest, session: OperatorSession
) -> dict[str, JsonValue]:
    """Build the requested plan: a reprocess preview, an escalation record or provider-replay instructions."""
    if body.operation == "reprocess_inbox":
        if body.fix_validation_id is None:
            raise ContractError("a reprocess_inbox plan needs fix_validation_id from a passing fix validation")
        preview = cli.build_plan(conn, run_id=run_id, fix_validation_id=body.fix_validation_id,
                                 stale_after_ms=workbench.settings.stale_after_seconds * 1000,
                                 ttl_ms=workbench.settings.plan_ttl_seconds * 1000)
        return _plan_preview_document(conn, run_id, preview)
    if body.diagnosis_id is None:
        raise ContractError(f"a {body.operation} plan needs diagnosis_id")
    if body.operation == "escalate":
        record = cli.escalate_diagnosis(conn, run_id=run_id, diagnosis_id=body.diagnosis_id, operator=session.operator_id)
        return {"operation": "escalate", **record.model_dump(mode="json")}
    return {"operation": "prepare_provider_replay", **_replay_instructions(conn, workbench, run_id, body.diagnosis_id)}


# --------------------------------------------------------------------------- documents


def _run_document(conn: sqlite3.Connection, run_id: str) -> dict[str, JsonValue]:
    run = storage.require_run(conn, run_id)
    return {
        "run_id": run.run_id,
        "scenario": run.scenario,
        "seed": run.seed,
        "split": run.split,
        "environment": run.environment,
        "generator_version": run.generator_version,
        "model_mode": run.model_mode.value,
        "replay_clock_ms": run.clock_ms,
        "incident_ids": [item.incident_id for item in storage.list_incidents(conn, run_id=run_id)],
        "created_wall_ms": run.created_wall_ms,
    }


def _job_document(workbench: Workbench, job: DiagnosisJob) -> dict[str, JsonValue]:
    stored_document: JsonValue = None
    if job.diagnosis_id is not None:
        with workbench.connection() as conn:
            stored = diagnosis_store.get_diagnosis(conn, run_id=job.run_id, diagnosis_id=job.diagnosis_id)
        stored_document = None if stored is None else cli.diagnosis_document(stored)
    return {
        "job_id": job.job_id,
        "run_id": job.run_id,
        "incident_id": job.incident_id,
        "mode": job.mode.value,
        "evidence_cutoff_ms": job.cutoff_ms,
        "status": job.status,
        "error": job.error,
        "created_wall_ms": job.created_wall_ms,
        "finished_wall_ms": job.finished_wall_ms,
        "diagnosis": stored_document,
        "packet": job.packet,
    }


def _plan_preview_document(conn: sqlite3.Connection, run_id: str, preview: plans.PlanPreview) -> dict[str, JsonValue]:
    stale_only = cli.stale_only_orders(preview)
    wanted = set(stale_only)
    payments: dict[str, list[str]] = {order_id: [] for order_id in stale_only}
    for attempt in storage.list_run_payment_attempts(conn, run_id=run_id, as_of_ms=None):
        if attempt.order_id in wanted:
            payments[attempt.order_id].append(attempt.payment_id)
    return {
        **cli.plan_document(preview),
        "excluded": [item.model_dump(mode="json") for item in preview.excluded],
        "stale_only_orders": [{"order_id": order_id, "payment_ids": payments[order_id]} for order_id in stale_only],
        "candidate_orders": preview.candidate_orders,
        "definitions": {"plan_value": METRIC_DEFINITIONS["plan_value"]},
    }


def _delivery_document(delivered: ingestion.DeliveryResponse) -> dict[str, JsonValue]:
    """The outcome and, once the delivery verified, its event ID: no inbox IDs and no receiver reasons."""
    document: dict[str, JsonValue] = {"outcome": delivered.outcome.value}
    if delivered.event_id is not None:
        document["event_id"] = delivered.event_id
    return document


def _replay_instructions(
    conn: sqlite3.Connection, workbench: Workbench, run_id: str, diagnosis_id: str
) -> dict[str, JsonValue]:
    stored = diagnosis_store.get_diagnosis(conn, run_id=run_id, diagnosis_id=diagnosis_id)
    if stored is None:
        raise ScopeError(f"diagnosis {diagnosis_id!r} is not part of run {run_id!r}")
    rechecked = _recheck_before_replay(conn, workbench, run_id)
    # In the re-check's own operator step when it took one, so the plan still follows one step after it.
    clock_ms = storage.require_run(conn, run_id).clock_ms
    log_read = cli.read_delivery_log(conn, workbench.config.data_dir, run_id,
                                     now_ms=clock_ms if rechecked["refresh"] is not None else None)
    instructions = plans.prepare_provider_replay(
        conn, run_id=run_id, incident_id=stored.incident_id, diagnosis_id=diagnosis_id,
        now_ms=cli.next_operator_time(conn, run_id), ttl_ms=workbench.settings.plan_ttl_seconds * 1000,
        stale_after_ms=workbench.settings.stale_after_seconds * 1000,
    )
    return {**instructions.model_dump(mode="json"), **rechecked, **_delivery_log_document(log_read)}


def _recheck_before_replay(conn: sqlite3.Connection, workbench: Workbench, run_id: str) -> dict[str, JsonValue]:
    """The read-only refresh of ``cli.refresh_candidates`` before a replay is prepared (owner decision Q26).

    Exactly one of ``refresh`` (the refresh route's answer) and ``refresh_skipped_because`` is set. The refresh
    takes an operator step, so the replay plan that follows is prepared one step later than without it.
    """
    candidates = cli.refresh_candidates(conn, run_id)
    if not candidates:
        return {"refresh": None, "refresh_skipped_because": cli.NOTHING_TO_REFRESH}
    if not cli.provider_records_path(workbench.config.data_dir, run_id).is_file():
        logger.warning("run %s has no provider records on this server; replay prepared without a re-check", run_id)
        return {"refresh": None,
                "refresh_skipped_because": "this server has no provider records for the run, so recently failed "
                                           "payments were not re-checked; a capture whose webhooks were all lost "
                                           "may be missing from these instructions"}
    summary = cli.refresh_payments(conn, workbench.config.data_dir, run_id=run_id, payment_ids=candidates)
    return {"refresh": _refresh_document(summary), "refresh_skipped_because": None}


def _delivery_log_document(read: cli.DeliveryLogRead) -> dict[str, JsonValue]:
    """Exactly one of ``delivery_log`` (what the read recorded) and ``delivery_log_skipped_because`` is set."""
    summary = read.summary
    if summary is None:
        return {"delivery_log": None, "delivery_log_skipped_because": read.skipped_because}
    return {"delivery_log": {"run_id": summary.run_id, "now_ms": summary.now_ms, "payment_ids": list(summary.payment_ids),
                             "attempts_recorded": len(summary.recorded),
                             "attempts_already_recorded": len(summary.already_recorded),
                             "acknowledged_captures": list(summary.acknowledged_captures)},
            "delivery_log_skipped_because": None}


def _refresh_document(summary: refresh.RefreshSummary) -> dict[str, JsonValue]:
    return {"run_id": summary.run_id, "now_ms": summary.now_ms, "refreshed": list(summary.refreshed),
            "already_recorded": list(summary.already_recorded),
            "unknown_at_provider": list(summary.unknown_at_provider), "statuses": dict(summary.statuses)}


# --------------------------------------------------------------------------- request guard and errors


def _install_request_guard(app: FastAPI, config: ApiConfig, workbench: Workbench) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        refusal = _refusal(request, config)
        if refusal:
            response: Response = JSONResponse({"error": "ScopeError", "detail": refusal}, status_code=403)
        else:
            response = _rate_limited(request, workbench) or await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if config.public is not None:
            response.headers.setdefault("Strict-Transport-Security", PUBLIC_TRANSPORT_SECURITY)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response


def _refusal(request: Request, config: ApiConfig) -> str | None:
    """Why this request may not proceed, or None. Checked before any route runs."""
    if request.headers.get("host", "") not in config.allowed_hosts:
        if config.public is not None:
            return "this server only answers requests addressed to its own public host"
        return "this server only answers requests addressed to its own loopback host"
    if request.method in SAFE_METHODS:
        return None
    origin = request.headers.get("origin")
    if origin is not None and origin not in config.allowed_origins:
        return "cross-origin state changes are refused"
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in SAME_ORIGIN_FETCH_SITES:
        return "cross-site state changes are refused"
    webhook_waiver = config.public is None and _is_webhook_delivery(request.scope)  # public mode has no webhook route
    if request.headers.get(REQUEST_HEADER) != "1" and not webhook_waiver:
        return f"state-changing requests need the {REQUEST_HEADER} header that the workbench UI sends"
    return None


def _rate_limited(request: Request, workbench: Workbench) -> JSONResponse | None:
    """Public mode's 429 for a request past its client address's or its session's minute, or None.

    Checked after the guard's refusals, before any route runs. Addresses are counted only behind the proxies that
    WORKBENCH_PUBLIC_TRUSTED_PROXIES names: without them every request seems to come from the host's proxy, and one
    count would hold back every viewer together.
    """
    public = workbench.public
    if public is None:
        return None
    if public.trusted_proxies and request.client is not None:
        wait = workbench.admit_address(request.client.host)
        if wait is not None:
            return _too_many_requests(workbench, request, wait, limit="address", client=request.client.host,
                                      detail="too many requests from this address in the last minute")
    if request.method == "POST" and EXPENSIVE_PATH.fullmatch(request.scope["path"]):
        wait, label = workbench.admit_expensive(request.cookies.get(workbench.session_cookie))
        if wait is not None:
            return _too_many_requests(workbench, request, wait, limit="session", session=label,
                                      detail="this session has made too many seeding, diagnosis, fix validation, "
                                             "planning or execution requests in the last minute")
    return None


def _too_many_requests(
    workbench: Workbench, request: Request, wait: int, *, limit: str, detail: str, **fields: object
) -> JSONResponse:
    workbench.log_public(logging.WARNING, "rate_limited", limit=limit, status=429, method=request.method,
                         path=request.scope["path"], retry_after=wait, **fields)
    return JSONResponse({"error": "RateLimitedError", "detail": f"{detail}; wait {wait} s and try again"},
                        status_code=429, headers={"Retry-After": str(wait)})


def _is_webhook_delivery(scope: Scope) -> bool:
    """A POST to the webhook route's exact path shape; a provider cannot send the UI's header.

    The request guard waives that header for it, and ``_BodyLimit`` leaves its body to the route's own read.
    It reads ``scope["path"]``, the path the router matches. ``request.url.path`` goes through
    ``urllib.parse``, which drops tab, CR and LF, so a path with ``%0a`` inside would match there.
    """
    return scope["method"] == "POST" and WEBHOOK_PATH.fullmatch(scope["path"]) is not None


class _BodyLimit:
    """Answer 413 for a body over ``MAX_REQUEST_BODY_BYTES`` before any route reads it; a webhook delivery is exempt.

    FastAPI reads and decodes a route's whole JSON body before it runs any dependency, so before ``current_operator``
    and before ``Workbench.login``. A declared length over the limit is answered here, before the app is called, with
    the plain-text 413 Starlette's limiter gives it. The limiter refuses it only at a route's first read, so a route
    that never reads its body (logout, execute, every GET) would run first and only then have its answer replaced.
    The limiter still stops a body sent without a length at the read that takes it past the limit, so nothing is
    decoded and no dependency runs. A webhook delivery keeps its own read (``_read_capped_body``), so one over the
    receiver's ceiling is still read to one byte past it and recorded as rejected_size.

    It sits inside the request guard: the guard's refusals stay 403, and its headers go on this 413 too. Outside the
    guard, the limit's exception would reach FastAPI through the guard's task group, wrapped in an exception group,
    and a body sent without a length would get FastAPI's 400 instead.
    """

    def __init__(self, app: ASGIApp, *, webhook_exempt: bool = True) -> None:
        self.app = app
        self.limited = RequestBodyLimitMiddleware(app, max_body_size=MAX_REQUEST_BODY_BYTES)
        self.webhook_exempt = webhook_exempt  # False in public mode, which installs no webhook route

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or (self.webhook_exempt and _is_webhook_delivery(scope)):
            await self.app(scope, receive, send)
            return
        if _declares_over_the_limit(scope):
            await PlainTextResponse("Content Too Large", status_code=413)(scope, receive, send)
            return
        await self.limited(scope, receive, send)


def _declares_over_the_limit(scope: Scope) -> bool:
    """Whether any ``Content-Length`` header of the request declares more than ``MAX_REQUEST_BODY_BYTES``.

    Each is read as Starlette's limiter reads the first one: with ``int()``, which also takes a sign, underscores and
    surrounding spaces. A stricter reading would let through a length the limiter then acts on after the route has run.
    Reading every header, not only the first, refuses two lengths when either is over the limit. A value ``int()``
    cannot read declares nothing, as for the limiter, whose running total still stops any read past the limit. Behind
    uvicorn's h11, which ``main`` runs, the app only ever sees one length of plain digits: uvicorn answers 400 to any
    other and to two that differ, and h11 passes on equal ones as one.
    """
    for value in Headers(scope=scope).getlist("content-length"):
        try:
            declared = int(value)
        except ValueError:
            continue  # declares nothing; the limiter's running total still stops any read past the limit
        if declared > MAX_REQUEST_BODY_BYTES:
            return True
    return False


def _install_error_handlers(app: FastAPI, workbench: Workbench) -> None:
    def respond(status: int, problem: BaseException, **extra: JsonValue) -> JSONResponse:
        return JSONResponse({"error": type(problem).__name__, "detail": workbench.public_detail(problem), **extra},
                            status_code=status)

    async def plan_rejected(_: Request, problem: Exception) -> JSONResponse:  # NOSONAR(S7503) no blocking work and nothing to await; as a plain function Starlette would run it in a thread pool for nothing
        assert isinstance(problem, PlanRejectedError)
        return respond(409, problem, result=problem.result.model_dump(mode="json"))

    async def run_exists(_: Request, problem: Exception) -> JSONResponse:  # NOSONAR(S7503) no blocking work and nothing to await; as a plain function Starlette would run it in a thread pool for nothing
        assert isinstance(problem, RunExistsError)
        return respond(409, problem, run_id=problem.run_id)

    async def busy(_: Request, problem: Exception) -> JSONResponse:  # NOSONAR(S7503) no blocking work and nothing to await; as a plain function Starlette would run it in a thread pool for nothing
        assert isinstance(problem, DiagnosisBusyError)
        return respond(409, problem, job_id=problem.job_id)

    def status_for(status: int) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
        async def handler(_: Request, problem: Exception) -> JSONResponse:  # NOSONAR(S7503) no blocking work and nothing to await; as a plain function Starlette would run it in a thread pool for nothing
            response = respond(status, problem)
            if isinstance(problem, RetryLaterError):
                response.headers["Retry-After"] = str(problem.retry_after_seconds)
            return response
        return handler

    async def missing_file(_: Request, problem: Exception) -> JSONResponse:  # NOSONAR(S7503) no blocking work and nothing to await; as a plain function Starlette would run it in a thread pool for nothing
        logger.error("missing server file: %s", problem)  # the path stays on the server console
        return JSONResponse({"error": "MissingServerData", "detail": "a file this run needs is missing from the "
                             "server's data directory; seed the run again in this data directory"}, status_code=422)

    app.add_exception_handler(PlanRejectedError, plan_rejected)
    app.add_exception_handler(RunExistsError, run_exists)
    app.add_exception_handler(DiagnosisBusyError, busy)
    app.add_exception_handler(TooManyLoginsError, status_for(429))
    app.add_exception_handler(QuotaExceededError, status_for(429))
    app.add_exception_handler(TooManyDeliveriesError, status_for(429))
    app.add_exception_handler(DeliveryNotStoredError, status_for(503))
    app.add_exception_handler(ReceiverFailedError, status_for(500))
    app.add_exception_handler(StaleStateError, status_for(409))
    app.add_exception_handler(UnknownRunError, status_for(404))  # Starlette picks the most specific class
    app.add_exception_handler(OrderNotInQueueError, status_for(404))
    app.add_exception_handler(JobNotKeptError, status_for(404))
    app.add_exception_handler(ScopeError, status_for(403))
    app.add_exception_handler(ContractError, status_for(422))
    app.add_exception_handler(WorkbenchError, status_for(422))
    app.add_exception_handler(ValueError, status_for(422))
    app.add_exception_handler(FileNotFoundError, missing_file)


def _mount_ui(app: FastAPI, ui_dir: Path | None) -> None:
    if ui_dir is not None and (ui_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=ui_dir, html=True), name="ui")
        return

    @app.get("/")
    def ui_missing() -> PlainTextResponse:
        return PlainTextResponse("The UI is not built. Run: npm --prefix frontend ci && npm --prefix frontend run build")


# --------------------------------------------------------------------------- entry point


def local_origins(port: int) -> tuple[frozenset[str], frozenset[str]]:
    """Allowed Host headers and Origins for a server bound to 127.0.0.1:port."""
    hosts = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
    return hosts, frozenset(f"http://{host}" for host in hosts)  # NOSONAR(S5332) origins of a plain-HTTP server bound to 127.0.0.1 only; TLS does not apply on loopback


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RayTrace API and UI on 127.0.0.1 (synthetic sandbox).")
    parser.add_argument("--data-dir", type=Path, default=None, help="default: WORKBENCH_DATA_DIR or ./local_data")
    parser.add_argument("--port", type=int, default=None,
                        help=f"port to listen on (default {DEFAULT_PORT}; in public mode, $PORT when the host sets it)")
    parser.add_argument("--host", choices=(BIND_HOST, ALL_INTERFACES), default=BIND_HOST,
                        help=f"address to listen on (default {BIND_HOST}); {ALL_INTERFACES} only in public mode")
    args = parser.parse_args(argv)
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error(f"--port must be between 1024 and 65535, got {args.port}")
    public = settings.load_public_settings()
    if args.host == ALL_INTERFACES and public is None:
        parser.error(f"--host {ALL_INTERFACES} needs public mode: set WORKBENCH_PUBLIC_ORIGIN (see .env.example), or "
                     f"leave --host out to listen on {BIND_HOST} only")
    configured = settings.load_settings()
    if public is not None:
        return _serve_public(args, public, configured)
    port = DEFAULT_PORT if args.port is None else args.port
    hosts, origins = local_origins(port)
    token = secrets.token_urlsafe(32)
    app = create_app(ApiConfig(data_dir=args.data_dir or configured.data_dir, operator_token=token,
                               allowed_hosts=hosts, allowed_origins=origins), configured)
    print(f"RayTrace (synthetic sandbox) on http://{BIND_HOST}:{port}/ ; execution mode "
          f"{configured.model_mode}")
    print(app.state.workbench.model_source.start_line())  # resolved once, in create_app; never the key
    print(f"Operator token for this server session (shown once): {token}", flush=True)
    # No proxy stands in front of this server. uvicorn would otherwise take X-Forwarded-For and X-Forwarded-Proto from
    # any loopback client, letting it set the request's client address and scheme.
    uvicorn.run(app, host=BIND_HOST, port=port, log_level="info", proxy_headers=False)
    return 0


def _serve_public(args: argparse.Namespace, public: settings.PublicSettings, configured: settings.Settings) -> int:
    """main() in public mode: one public origin, the demo token, which is never printed, and the public server limits.

    It listens on 127.0.0.1 unless --host says 0.0.0.0, so the same build can serve a tunnel on the owner's machine
    without being exposed to the local network.
    """
    port = args.port if args.port is not None else (public.port or DEFAULT_PORT)
    public_mode.show_events()
    app = create_app(ApiConfig(data_dir=args.data_dir or configured.data_dir, operator_token=public.demo_token,
                               allowed_hosts=frozenset({public.host}), allowed_origins=frozenset({public.origin}),
                               public=public), configured)
    proxies = ", ".join(public.trusted_proxies) or "none"
    print(f"RayTrace (synthetic sandbox) in public mode for {public.origin} ; listening on "
          f"{args.host} port {port} ; execution mode {configured.model_mode}")
    print(app.state.workbench.model_source.start_line())  # resolved once, in create_app; never the key
    print("Operator token: the demo token from WORKBENCH_DEMO_TOKEN (never printed or logged)")
    print(f"Public mode: seeds {sorted(public.seeds)}; {public.runs_per_session} runs a session and "
          f"{public.runs_per_process} a process; data limit {public.data_limit_bytes // 2**20} MB; webhook route off; "
          f"forwarded headers trusted from: {proxies}", flush=True)
    if app.state.demo_runs is not None:
        print(app.state.demo_runs.start_line(), flush=True)
    uvicorn.run(app, host=args.host, port=port, log_level="info", **public_mode.server_options(public))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
