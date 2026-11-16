"""Public mode (docs/DEMO_HOSTING.md, section 3.3, scope A): the server behind one private demo link.

Each test starts the server as a host would: it sets the variables, then calls ``api.main`` with ``uvicorn.run``
replaced. So it checks what main hands uvicorn, and drives the app main built through FastAPI's TestClient, addressed
as a viewer's browser addresses it: https://demo.example.test, a name under the reserved .test domain. Connections to
a real server over 127.0.0.1 are in test_public_server.py. New seeded runs use seeds 20701 to 20800; none is the
README's 101, so these runs are not the demo runs.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
from collections.abc import Iterator
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from uvicorn.protocols.http.h11_impl import H11Protocol

from recovery_workbench import api, cli, public_mode, settings, storage

ORIGIN = "https://demo.example.test"
HOST = "demo.example.test"
OPERATOR = "op_public_viewer"
SEEDS = (20701, 20702, 20703, 20704, 20705)
SCENARIO = "worker_backlog"  # the quickest of the three to seed
UI_HEADER = {api.REQUEST_HEADER: "1"}
STATE_CHANGE = {**UI_HEADER, "origin": ORIGIN}  # what a browser on the public origin sends with every state change
WEBHOOK = "/api/runs/run_0000000000/webhooks/int_SYNTHETIC_MAIN"
PUBLIC_VARIABLES = (
    "WORKBENCH_PUBLIC_ORIGIN", "WORKBENCH_DEMO_TOKEN", "WORKBENCH_PUBLIC_SEEDS", "WORKBENCH_PUBLIC_RUNS_PER_SESSION",
    "WORKBENCH_PUBLIC_RUNS_PER_PROCESS", "WORKBENCH_PUBLIC_DATA_LIMIT_MB", "WORKBENCH_PUBLIC_TRUSTED_PROXIES", "PORT",
    "WORKBENCH_PUBLIC_TEMPLATE_DIR",
)
# Public mode's values, stated here so the tests pin them by what the server does.
COOKIE = "__Host-workbench_session"
IDLE_SECONDS = 30 * 60
LIFETIME_SECONDS = 8 * 60 * 60
MAX_SESSIONS = 20
EXPENSIVE_PER_MINUTE = 10
REQUESTS_PER_ADDRESS_PER_MINUTE = 300
LIMIT_CONCURRENCY = 64
DATA_MARKER = ".workbench-public-mode"
DATA_MARKER_TEXT = "Recovery Workbench public mode made this directory its own and empties it at every start.\n"
DATABASE_FILES = {"workbench.sqlite", "workbench.sqlite-wal", "workbench.sqlite-shm"}
EVENT_LOGGER = "recovery_workbench.public"


# --------------------------------------------------------------------------- helpers


@dataclass(frozen=True)
class Served:
    app: FastAPI
    options: dict[str, object]  # what api.main passed to uvicorn.run besides the app
    token: str


@dataclass
class Clock:
    """The monotonic clock the API reads, set by the test."""

    now: float = 1000.0


@pytest.fixture(autouse=True)
def no_public_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test sets exactly the variables it means; none leaks in from the environment that runs the suite."""
    for name in PUBLIC_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    current = Clock()
    # Only api's own name for the time module is replaced, so nothing else sees the test's clock.
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: current.now))
    return current


def demo_token() -> str:
    """A token made as the operator is told to make one. Public mode refuses about 1 in 1,600 of them, nearly all for
    holding no digit, as it would refuse the operator's; a test draws again then, so no test depends on that chance."""
    while True:
        token = secrets.token_urlsafe(32)
        if settings._demo_token_problem(token) is None:
            return token


def set_variables(monkeypatch: pytest.MonkeyPatch, variables: dict[str, str | None]) -> dict[str, str]:
    """Public mode's variables: none from an earlier start, then the defaults, then ``variables``, where None
    leaves one unset. Returns what was set."""
    for name in PUBLIC_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    chosen: dict[str, str | None] = {
        "WORKBENCH_PUBLIC_ORIGIN": ORIGIN, "WORKBENCH_DEMO_TOKEN": demo_token(), "WORKBENCH_MODEL_MODE": "rules",
        "WORKBENCH_PUBLIC_SEEDS": ",".join(str(seed) for seed in SEEDS), **variables,
    }
    for name, value in chosen.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    return {name: value for name, value in chosen.items() if value is not None}


def serve(monkeypatch: pytest.MonkeyPatch, data_dir: Path, *argv: str, **variables: str | None) -> Served:
    """Start the server as a host does: the variables set, then api.main, with uvicorn.run replaced."""
    chosen = set_variables(monkeypatch, variables)
    served: dict[str, object] = {}
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **options: served.update(options, app=app))
    assert api.main(["--data-dir", str(data_dir), *argv]) == 0
    app = served.pop("app")
    assert isinstance(app, FastAPI)
    return Served(app=app, options=served, token=chosen.get("WORKBENCH_DEMO_TOKEN", ""))


def refusal(monkeypatch: pytest.MonkeyPatch, data_dir: Path, *argv: str, **variables: str | None) -> str:
    """The message api.main refuses to start with; the server never started."""
    set_variables(monkeypatch, variables)
    started: list[dict[str, object]] = []
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **options: started.append(options))
    with pytest.raises(ValueError) as refused:
        api.main(["--data-dir", str(data_dir), *argv])
    assert started == []
    return str(refused.value)


def browser(app: FastAPI, address: str = "testclient") -> TestClient:
    """A viewer's browser on the public origin: Host demo.example.test over https, so a Secure cookie comes back."""
    return TestClient(app, base_url=ORIGIN, client=(address, 50_000))


def log_in(client: TestClient, token: str, operator: str = OPERATOR) -> httpx.Response:
    return client.post("/api/session", json={"token": token, "operator_id": operator}, headers=STATE_CHANGE)


def seed(client: TestClient, seed_value: int, scenario: str = SCENARIO) -> httpx.Response:
    return client.post("/api/runs", json={"scenario": scenario, "seed": seed_value}, headers=STATE_CHANGE)


def run_ids(app: FastAPI) -> list[str]:
    with closing(storage.connect(app.state.workbench.database)) as conn:
        return [str(row["run_id"]) for row in conn.execute("SELECT run_id FROM runs ORDER BY run_id")]


def delivery_attempts(app: FastAPI) -> int:
    with closing(storage.connect(app.state.workbench.database)) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM delivery_attempts").fetchone()[0])


def events(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == EVENT_LOGGER]


# --------------------------------------------------------------------------- off by default: nothing changes


def test_without_an_origin_main_serves_loopback_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A control: these pass on the code before public mode too, apart from the model chain's start line (Q46)."""
    monkeypatch.setenv("PORT", "10000")  # a host's port variable means nothing with public mode off
    served: dict[str, object] = {}
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **options: served.update(options, app=app))
    assert api.main(["--data-dir", str(tmp_path / "data")]) == 0
    app = served.pop("app")
    assert served == {"host": "127.0.0.1", "port": 8000, "log_level": "info", "proxy_headers": False}
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "RayTrace (synthetic sandbox) on http://127.0.0.1:8000/ ; execution mode local"
    # The model chain (Q46) prints the model source the server chose at start before the token.
    assert out[1] == "Model source: local model qwen3:8b; set by WORKBENCH_MODEL_MODE=local"
    assert out[2].startswith("Operator token for this server session (shown once): ")
    token = out[2].rsplit(": ", 1)[1]
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert {"model", "versions", "wall_clock_ms"} <= set(client.get("/health").json())
        login = client.post("/api/session", json={"token": token, "operator_id": OPERATOR}, headers=UI_HEADER)
        assert login.status_code == 200
        cookie = login.headers["set-cookie"]
        assert cookie.startswith(f"{api.SESSION_COOKIE}=")
        assert "secure" not in cookie.lower()
        assert client.get("/health", headers={"host": HOST}).status_code == 403
    assert any(isinstance(route, APIRoute) and "webhooks" in route.path for route in app.routes)
    assert not (tmp_path / "data" / DATA_MARKER).exists()


# --------------------------------------------------------------------------- item 1: the switch, the origin, binding


@pytest.mark.parametrize("origin", [
    "http://demo.example.test",  # not https
    "HTTPS://demo.example.test",  # not as a browser writes it
    "https://*.example.test",  # a wildcard
    "*",
    "https://demo.example.test/",  # a path, even a bare slash
    "https://demo.example.test/app",
    "https://demo.example.test?x=1",
    "https://demo.example.test#top",
    "https://viewer@demo.example.test",  # user information
    "https://demo.example.test:443",  # the default port, which a browser leaves out of Origin
    "https://demo.example.test:0",
    "https://demo.example.test:99999",
    "https://Demo.example.test",  # not lowercase
    "https://demo",  # one label
    "https://demo.example.test.",  # a trailing dot
    "https://203.0.113.7",  # an address, not a name
    "https://demo.example.test, https://other.example.test",  # two origins
])
def test_main_refuses_anything_but_one_exact_https_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    message = refusal(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_ORIGIN=origin)
    assert message.startswith("WORKBENCH_PUBLIC_ORIGIN must be")
    assert repr(origin) in message  # it says what it was given
    assert not (tmp_path / "data").exists()  # refused before the data directory was touched


def test_public_mode_serves_only_its_origin_and_listens_on_loopback_unless_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    assert (served.options["host"], served.options["port"]) == ("127.0.0.1", 8000)  # a tunnel's case: loopback only
    with browser(served.app) as client:
        assert client.get("/api/session").status_code == 200
        assert log_in(client, served.token).status_code == 200
        assert client.get("/api/runs").status_code == 200
        # The loopback names are refused: the Secure cookie could not work over them, and a second door is one too many.
        for host in ("127.0.0.1:8000", "localhost:8000", "other.example.test", f"{HOST}:8000"):
            assert client.get("/api/session", headers={"host": host}).status_code == 403, host
        for origin in ("http://127.0.0.1:8000", "http://localhost:8000", f"http://{HOST}", "https://other.example.test",
                       "null"):
            refused = client.post("/api/runs", json={"scenario": SCENARIO, "seed": SEEDS[0]},
                                  headers={**UI_HEADER, "origin": origin})
            assert refused.status_code == 403, origin
        assert run_ids(served.app) == []


def test_listening_on_every_interface_needs_public_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **options: pytest.fail("the server must not start"))
    with pytest.raises(SystemExit) as refused:
        api.main(["--data-dir", str(tmp_path / "off"), "--host", "0.0.0.0"])
    assert refused.value.code == 2
    assert "--host 0.0.0.0 needs public mode: set WORKBENCH_PUBLIC_ORIGIN" in capsys.readouterr().err
    served = serve(monkeypatch, tmp_path / "data", "--host", "0.0.0.0")
    assert served.options["host"] == "0.0.0.0"


@pytest.mark.parametrize(("port_variable", "argv", "expected"), [
    pytest.param("10000", (), 10000, id="PORT"),
    pytest.param(None, (), 8000, id="default"),
    pytest.param("10000", ("--port", "9000"), 9000, id="--port-wins"),
])
def test_public_mode_takes_the_port_from_PORT(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port_variable: str | None, argv: tuple[str, ...], expected: int
) -> None:
    served = serve(monkeypatch, tmp_path / "data", *argv, PORT=port_variable)
    assert served.options["port"] == expected


@pytest.mark.parametrize("port", ["80", "1023", "65536", "abc", "8000.0", "-1"])
def test_public_mode_refuses_a_port_variable_it_cannot_listen_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, port: str
) -> None:
    message = refusal(monkeypatch, tmp_path / "data", PORT=port)
    assert message.startswith(f"PORT must be a port number from 1024 to 65535, got {port!r}")


def test_the_example_settings_file_leaves_public_mode_off() -> None:
    """Every variable .env.example lists, with the value it lists, starts the loopback server as before."""
    example = Path(__file__).resolve().parents[1] / ".env.example"
    listed = dict(line.split("=", 1) for line in example.read_text(encoding="utf-8").splitlines()
                  if line and not line.startswith("#"))
    assert {"WORKBENCH_PUBLIC_ORIGIN", "WORKBENCH_DEMO_TOKEN", "WORKBENCH_PUBLIC_SEEDS"} <= set(listed)
    assert listed["WORKBENCH_PUBLIC_ORIGIN"] == listed["WORKBENCH_DEMO_TOKEN"] == ""  # no secret, no switch
    assert settings.load_public_settings(listed) is None
    assert settings.load_settings(listed).model_mode == "local"


def test_public_settings_read_every_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    token = demo_token()
    loaded = settings.load_public_settings({
        "WORKBENCH_PUBLIC_ORIGIN": "https://demo.example.test:8443", "WORKBENCH_DEMO_TOKEN": token,
        "WORKBENCH_PUBLIC_SEEDS": " 101, 20701 ", "WORKBENCH_PUBLIC_RUNS_PER_SESSION": "2",
        "WORKBENCH_PUBLIC_RUNS_PER_PROCESS": "5", "WORKBENCH_PUBLIC_DATA_LIMIT_MB": "64",
        "WORKBENCH_PUBLIC_TRUSTED_PROXIES": "10.0.0.0/8, 192.0.2.10,::1", "PORT": "10000",
    })
    assert loaded is not None
    assert (loaded.origin, loaded.host) == ("https://demo.example.test:8443", "demo.example.test:8443")
    assert loaded.demo_token == token
    assert token not in repr(loaded)  # the token stays out of the settings' repr
    assert loaded.seeds == {101, 20701}
    assert (loaded.runs_per_session, loaded.runs_per_process, loaded.data_limit_bytes) == (2, 5, 64 * 2**20)
    assert loaded.trusted_proxies == ("10.0.0.0/8", "192.0.2.10", "::1")
    assert loaded.port == 10000
    assert settings.load_public_settings({}) is None
    assert settings.load_public_settings({"WORKBENCH_PUBLIC_ORIGIN": "", "WORKBENCH_DEMO_TOKEN": ""}) is None


# --------------------------------------------------------------------------- item 2: the demo token from a secret


@pytest.mark.parametrize("token", [
    pytest.param(None, id="unset"),
    pytest.param("", id="empty"),
    pytest.param(secrets.token_urlsafe(32)[:31], id="31-characters"),
    pytest.param("x" + secrets.token_urlsafe(192), id="257-characters"),
    pytest.param("a" * 40, id="one-character"),
    pytest.param("passwordpasswordpasswordpassword", id="few-characters"),
    pytest.param("0123456789abcdef" * 3, id="a-repeated-run"),
    pytest.param(secrets.token_urlsafe(32) + " ", id="a-space"),
    pytest.param(secrets.token_urlsafe(32) + "é", id="not-ascii"),
])
def test_public_mode_refuses_to_start_without_a_strong_demo_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str | None
) -> None:
    message = refusal(monkeypatch, tmp_path / "data", WORKBENCH_DEMO_TOKEN=token)
    assert message.startswith("WORKBENCH_DEMO_TOKEN ")
    assert "secrets.token_urlsafe(32)" in message  # it says what to do
    if token:
        assert token.strip() not in message  # and never shows the value
        assert f"{len(token)} characters (not shown)" in message


CHOSEN_TOKENS = {  # tokens a person picks, each refused by the rule named
    "abcdefghijklmnopqrstuvwxyzABCDEF": "must be 43 to 256 characters long",  # an alphabet
    "RecoveryWorkbench-DemoToken-2026-11": "must be 43 to 256 characters long",  # names and a date
    "correct-horse-battery-staple-viewer": "must be 43 to 256 characters long",  # a passphrase
    "qwertyuiopasdfghjklzxcvbnm123456": "must be 43 to 256 characters long",  # keyboard rows
    "correct-horse-battery-staple-2026": "must be 43 to 256 characters long",
    "recovery-workbench-demo-password": "must be 43 to 256 characters long",
    "correct-horse-battery-staple-viewer-demo-link-2026": "must hold at least 3 uppercase letters",  # long, one case
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQ": "and 1 digit",  # long, no digit
    "QWERTYUIOPASDFGHJKLZXCVBNM-DEMO-LINK-2026-ab": "must hold at least 3 uppercase letters",  # 2 lowercase
    "Aa1Bb2Cc3Dd4Aa1Bb2Cc3Dd4Aa1Bb2Cc3Dd4Aa1Bb2Cc3": "must use at least 20 different characters",
    "ABCDEFGHIJKLMNOPabcdefghijklmnop0123ABCDEFGH": "repeats a run of 8 characters",
}


@pytest.mark.parametrize("token", list(CHOSEN_TOKENS))
def test_public_mode_refuses_a_chosen_or_structured_demo_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str
) -> None:
    """Security audit run 3: login tests every guess (Q38), so the start check must refuse what a person picks."""
    message = refusal(monkeypatch, tmp_path / "data", WORKBENCH_DEMO_TOKEN=token)
    assert message.startswith("WORKBENCH_DEMO_TOKEN ")
    assert CHOSEN_TOKENS[token] in message
    assert token not in message
    assert f"got a value of {len(token)} characters (not shown)" in message
    assert "secrets.token_urlsafe(32)" in message


def test_generated_demo_tokens_pass_the_start_check_but_about_one_in_1600() -> None:
    """The check's cost to an operator who follows the advice: about 1 generated token in 1,600 is refused at 43
    characters, nearly always for holding no digit (479 of 800,000 measured), and longer ones all but never."""
    drawn = [secrets.token_urlsafe(32) for _ in range(10_000)]
    refused = [token for token in drawn if settings._demo_token_problem(token) is not None]
    assert len(refused) <= 30  # about 6 expected; more than 30 is a chance far below one in a billion
    for size in (48, 64, 96, 192):
        assert settings._demo_token_problem(secrets.token_urlsafe(size)) is None


def test_public_mode_takes_the_token_from_the_secret_and_never_prints_or_logs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        served = serve(monkeypatch, tmp_path / "data")
        with browser(served.app) as client:
            assert client.get("/api/runs").json()["detail"] == (
                "an operator session is required; log in with the demo token you were given")
            wrong = log_in(client, "a-guess-that-is-wrong-" + "x" * 20)
            assert (wrong.status_code, wrong.json()["detail"]) == (
                403, "the demo token is not valid; use the demo token you were given")
            accepted = log_in(client, served.token)
            assert accepted.status_code == 200
            assert seed(client, SEEDS[0]).status_code == 201
            assert client.post(WEBHOOK, content=b"{}", headers=STATE_CHANGE).status_code != 200
            pages = [accepted.text, client.get("/health").text, client.get("/api/session").text]
    printed = capsys.readouterr()
    assert "Operator token: the demo token from WORKBENCH_DEMO_TOKEN (never printed or logged)" in printed.out
    cookie_value = accepted.cookies[COOKIE]
    for secret in (served.token, cookie_value):
        assert secret not in printed.out
        assert secret not in printed.err
        assert not [record for record in caplog.records if secret in record.getMessage()]
        assert not [page for page in pages if secret in page]


def test_public_only_settings_without_the_origin_refuse_to_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A demo token or a proxy list says public mode was meant: a missing switch must not quietly start without it."""
    token = demo_token()
    message = refusal(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_ORIGIN=None, WORKBENCH_DEMO_TOKEN=token)
    assert message.startswith("WORKBENCH_DEMO_TOKEN is set but WORKBENCH_PUBLIC_ORIGIN is not")
    assert token not in message
    message = refusal(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_ORIGIN=None, WORKBENCH_DEMO_TOKEN=None,
                      WORKBENCH_PUBLIC_TRUSTED_PROXIES="10.0.0.0/8")
    assert message.startswith("WORKBENCH_PUBLIC_TRUSTED_PROXIES is set but WORKBENCH_PUBLIC_ORIGIN is not")


# --------------------------------------------------------------------------- item 3: cookie and session lifetime


def test_the_public_session_cookie_is_host_prefixed_secure_http_only_and_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    with browser(served.app) as client:
        login = log_in(client, served.token)
        assert login.status_code == 200
        attributes = [part.strip().lower() for part in login.headers["set-cookie"].split(";")]
        assert attributes[0].startswith(f"{COOKIE.lower()}=")
        assert {"secure", "httponly", "samesite=strict", "path=/"} <= set(attributes)
        assert not [part for part in attributes if part.startswith(("domain=", "max-age=", "expires="))]
        assert client.get("/api/session").json() == {"operator_id": OPERATOR}
        logout = client.delete("/api/session", headers=STATE_CHANGE)
        assert logout.status_code == 200
        removal = [part.strip().lower() for part in logout.headers["set-cookie"].split(";")]
        assert removal[0] == f'{COOKIE.lower()}=""'
        assert {"secure", "max-age=0", "path=/"} <= set(removal)  # a browser ignores a __Host- cookie without Secure
        assert client.get("/api/runs").status_code == 403


def test_a_public_session_ends_after_30_idle_minutes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    with browser(served.app) as client:
        assert log_in(client, served.token).status_code == 200
        clock.now += IDLE_SECONDS - 1
        assert client.get("/api/runs").status_code == 200  # a request in time starts the idle time again
        clock.now += IDLE_SECONDS - 1
        assert client.get("/api/runs").status_code == 200
        clock.now += IDLE_SECONDS
        assert client.get("/api/runs").status_code == 403
        assert client.get("/api/session").json() == {"operator_id": None}


def test_a_public_session_ends_after_8_hours_however_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    with browser(served.app) as client:
        assert log_in(client, served.token).status_code == 200
        started = clock.now
        while clock.now + 600 < started + LIFETIME_SECONDS:
            clock.now += 600
            assert client.get("/api/runs").status_code == 200
        clock.now = started + LIFETIME_SECONDS
        assert client.get("/api/runs").status_code == 403


def test_at_the_session_cap_a_login_ends_the_least_recently_used_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    """Security audit run 3: a full cap refused every login for up to 8 hours, so one holder of the token who opened
    20 sessions kept every other viewer out. Now the session idle the longest makes room, and the log says so."""
    caplog.set_level(logging.INFO, logger=EVENT_LOGGER)
    served = serve(monkeypatch, tmp_path / "data")
    with ExitStack() as stack:
        browsers = [stack.enter_context(closing(browser(served.app))) for _ in range(MAX_SESSIONS + 1)]
        logins = []
        for index, viewer in enumerate(browsers[:MAX_SESSIONS]):
            clock.now += 1
            logins.append(log_in(viewer, served.token, f"op_viewer_{index}"))
        assert [login.status_code for login in logins] == [200] * MAX_SESSIONS
        clock.now += 1
        assert browsers[0].get("/api/runs").status_code == 200  # the first session is no longer the least recent
        clock.now += 1
        late = log_in(browsers[MAX_SESSIONS], served.token, "op_one_more")
        assert late.status_code == 200
        assert browsers[1].get("/api/runs").status_code == 403  # idle 20 s, the longest of all
        assert [viewer.get("/api/runs").status_code for viewer in (browsers[0], *browsers[2:])] == [200] * MAX_SESSIONS
        cookies = [response.cookies[COOKIE] for response in (*logins, late)]
    lines = events(caplog)
    [evicted] = [line for line in lines if "event=session_evicted" in line]
    assert evicted.startswith("public-mode event=session_evicted session=s_")
    assert evicted.endswith("operator=op_viewer_1 idle_seconds=20 live_sessions=19")
    assert lines[-1].startswith("public-mode event=login result=ok status=200 operator=op_one_more session=s_")
    assert lines[-1].endswith(f"live_sessions={MAX_SESSIONS}")  # one made room, so the cap holds
    assert not [line for line in lines for cookie in cookies if cookie in line]  # never a cookie value


def test_a_login_ends_the_session_its_browser_presented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Security audit run 3: logging in again left the earlier session alive, holding a slot for up to 8 hours though
    no browser held its cookie any more. A wrong token changes nothing."""
    caplog.set_level(logging.INFO, logger=EVENT_LOGGER)
    served = serve(monkeypatch, tmp_path / "data")
    with browser(served.app) as client:
        earlier = log_in(client, served.token).cookies[COOKIE]
        assert log_in(client, "a-wrong-token-" + "z" * 30).status_code == 403
        assert client.get("/api/runs").status_code == 200  # a failed login leaves the browser's session as it was
        later = log_in(client, served.token, "op_public_viewer_again").cookies[COOKIE]
        assert client.get("/api/session").json() == {"operator_id": "op_public_viewer_again"}
        replayed = client.get("/api/runs", headers={"cookie": f"{COOKIE}={earlier}"})  # the earlier cookie, sent again
    assert later != earlier
    assert replayed.status_code == 403
    logins = [line for line in events(caplog) if "event=login result=ok" in line]
    first_label = logins[0].split("session=")[1].split()[0]
    assert logins[0].endswith("live_sessions=1")
    assert logins[1].endswith(f"live_sessions=1 replaced={first_label}")


def test_with_public_mode_off_a_login_leaves_earlier_sessions_alone(tmp_path: Path) -> None:
    """A control: the loopback server keeps every session until logout or restart, as before."""
    from test_api import STATE_CHANGE as LOOPBACK_CHANGE
    from test_api import TOKEN, make_client

    with make_client(tmp_path) as client:
        def loopback_login() -> str:
            login = client.post("/api/session", json={"token": TOKEN, "operator_id": OPERATOR}, headers=LOOPBACK_CHANGE)
            assert login.status_code == 200
            return login.cookies[api.SESSION_COOKIE]

        earlier, later = loopback_login(), loopback_login()
        for session_id in (earlier, later):
            assert client.get("/api/runs", headers={"cookie": f"{api.SESSION_COOKIE}={session_id}"}).status_code == 200


def test_with_public_mode_off_sessions_are_neither_capped_nor_timed(tmp_path: Path, clock: Clock) -> None:
    """A control: the loopback server keeps a session until logout or restart, as before."""
    from test_api import STATE_CHANGE as LOOPBACK_CHANGE
    from test_api import TOKEN, make_client

    with make_client(tmp_path) as client:
        for index in range(MAX_SESSIONS + 5):
            login = client.post("/api/session", json={"token": TOKEN, "operator_id": f"op_viewer_{index}"},
                                headers=LOOPBACK_CHANGE)
            assert login.status_code == 200
        clock.now += LIFETIME_SECONDS * 3
        assert client.get("/api/runs").status_code == 200


# --------------------------------------------------------------------------- item 5: quotas, shared runs, resets


def test_public_mode_seeds_only_the_published_seeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = serve(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_SEEDS="20706")
    with browser(served.app) as client:
        assert log_in(client, served.token).status_code == 200
        refused = seed(client, 20707)
        assert refused.status_code == 422
        assert refused.json() == {"error": "SeedNotOfferedError",
                                  "detail": "seed 20707 is not offered on this demo; choose one of [20706]"}
        assert run_ids(served.app) == []
        assert seed(client, 20706).status_code == 201


def test_public_mode_caps_runs_per_session_and_per_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = serve(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_RUNS_PER_SESSION="2",
                   WORKBENCH_PUBLIC_RUNS_PER_PROCESS="3")
    with browser(served.app) as first, closing(browser(served.app)) as second:
        assert log_in(first, served.token).status_code == 200
        assert log_in(second, served.token, "op_second_viewer").status_code == 200
        assert [seed(first, value).status_code for value in SEEDS[:2]] == [201, 201]
        over_session = seed(first, SEEDS[2])
        assert over_session.status_code == 429
        assert over_session.json()["error"] == "QuotaExceededError"
        assert "the most public mode allows a session" in over_session.json()["detail"]
        assert seed(first, SEEDS[0]).status_code == 200  # opening a run that exists costs no quota
        assert seed(second, SEEDS[2]).status_code == 201  # the process's third
        over_process = seed(second, SEEDS[3])
        assert over_process.status_code == 429
        assert "this server has created 3 runs" in over_process.json()["detail"]
        assert len(run_ids(served.app)) == 3


def test_public_mode_refuses_new_runs_past_the_data_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = serve(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_DATA_LIMIT_MB="1")
    with browser(served.app) as client:
        assert log_in(client, served.token).status_code == 200
        assert seed(client, SEEDS[0]).status_code == 201  # a run holds about 3 MB, so the limit is passed after it
        refused = seed(client, SEEDS[1])
        assert refused.status_code == 429
        assert refused.json()["error"] == "QuotaExceededError"
        assert "at public mode's limit of 1 MB" in refused.json()["detail"]
        assert str(tmp_path) not in refused.text
        assert len(run_ids(served.app)) == 1


def test_two_viewers_who_seed_the_same_run_share_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scope A shares runs, so the second viewer gets the run instead of the 409 the loopback server gives."""
    served = serve(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_RUNS_PER_SESSION="1")
    with browser(served.app) as first, closing(browser(served.app)) as second:
        assert log_in(first, served.token).status_code == 200
        assert log_in(second, served.token, "op_second_viewer").status_code == 200
        created = seed(first, SEEDS[0])
        assert created.status_code == 201
        shared = seed(second, SEEDS[0])
        assert shared.status_code == 200
        assert shared.json() == second.get(f"/api/runs/{created.json()['run_id']}").json()
        assert shared.json()["run_id"] == created.json()["run_id"]
        assert seed(second, SEEDS[1]).status_code == 201  # the shared run took nothing from the second viewer's quota
        assert len(run_ids(served.app)) == 2


def test_public_mode_starts_each_process_from_an_empty_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    served = serve(monkeypatch, data_dir)
    assert (data_dir / DATA_MARKER).is_file()
    with browser(served.app) as client:
        assert log_in(client, served.token).status_code == 200
        run_id = seed(client, SEEDS[0]).json()["run_id"]
    assert (data_dir / cli.ORACLE_DIRECTORY / f"{run_id}.json").is_file()
    restarted = serve(monkeypatch, data_dir)
    assert run_ids(restarted.app) == []
    assert {path.name for path in data_dir.iterdir()} <= {DATA_MARKER, *DATABASE_FILES}  # the new database only
    assert (data_dir / DATA_MARKER).read_text(encoding="utf-8") == DATA_MARKER_TEXT


@pytest.mark.parametrize("unowned", ["a file", "a marker with other text", "a marker that is a link", "lost+found"])
def test_public_mode_refuses_a_data_directory_it_did_not_mark_and_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unowned: str
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    if unowned == "a file":
        (data_dir / "notes.txt").write_text("not public mode's", encoding="utf-8")
    elif unowned == "a marker with other text":
        (data_dir / DATA_MARKER).write_text("something else\n", encoding="utf-8")
    elif unowned == "a marker that is a link":
        real = elsewhere / DATA_MARKER
        real.write_text(DATA_MARKER_TEXT, encoding="utf-8")  # the right text, but reached through a link
        (data_dir / DATA_MARKER).symlink_to(real)
    else:
        (data_dir / "lost+found").mkdir()
    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    message = refusal(monkeypatch, data_dir)
    assert message.startswith("public mode needs an empty data directory, or one it marked as its own")
    assert message.endswith("Nothing was deleted")
    assert sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")) == before


def test_public_mode_never_follows_a_link_out_of_its_data_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    serve(monkeypatch, data_dir)  # marks it
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("outside the data directory", encoding="utf-8")
    (data_dir / "linked_dir").symlink_to(outside, target_is_directory=True)
    nested = data_dir / "nested"
    nested.mkdir()
    (nested / "inner_link").symlink_to(outside, target_is_directory=True)
    serve(monkeypatch, data_dir)
    assert (outside / "keep.txt").read_text(encoding="utf-8") == "outside the data directory"
    assert not (data_dir / "linked_dir").exists()
    assert not nested.exists()
    assert (data_dir / DATA_MARKER).is_file()


def test_public_mode_never_writes_its_marker_through_a_link_put_in_its_place(tmp_path: Path) -> None:
    """Security audit run 3: the marker was written with write_text, which follows a symbolic link made between the
    empty listing and the write. Here the link appears just after the listing, as a racing process would put it."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside the data directory\n", encoding="utf-8")

    class RacedDirectory(type(tmp_path)):  # type: ignore[misc]
        def iterdir(self) -> Iterator[Path]:
            listed = list(super().iterdir())
            (self / DATA_MARKER).symlink_to(outside)
            return iter(listed)

    data_dir = RacedDirectory(tmp_path / "data")
    data_dir.mkdir()
    with pytest.raises(ValueError, match=f"{DATA_MARKER} appeared in it before the marker was written") as refused:
        public_mode.claim_data_dir(data_dir)
    assert str(refused.value).endswith("Nothing was deleted")
    assert outside.read_text(encoding="utf-8") == "outside the data directory\n"
    assert (tmp_path / "data" / DATA_MARKER).is_symlink()  # left where it was found


def marked_data_dir(tmp_path: Path) -> Path:
    """A data directory as public mode leaves it: its marker, and nothing else yet."""
    data_dir = tmp_path / "data"
    public_mode.claim_data_dir(data_dir)
    assert [path.name for path in data_dir.iterdir()] == [DATA_MARKER]
    return data_dir


def test_with_public_mode_off_the_server_refuses_a_directory_public_mode_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security audit run 3: public mode empties the directory it marked at its next start, so whatever the loopback
    server wrote there would be lost. It now refuses the directory and writes nothing. A marker that public mode would
    not accept, so would never empty the directory for, changes nothing."""
    data_dir = marked_data_dir(tmp_path)
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **options: pytest.fail("the server must not start"))
    with pytest.raises(ValueError) as refused:
        api.main(["--data-dir", str(data_dir)])
    assert str(refused.value).startswith(f"{data_dir} is public mode's data directory: it holds the {DATA_MARKER} "
                                         "file, and public mode empties the directory at every start")
    assert [path.name for path in data_dir.iterdir()] == [DATA_MARKER]
    (data_dir / DATA_MARKER).write_text("a note, not public mode's marker\n", encoding="utf-8")
    started: list[object] = []
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **options: started.append(app))
    assert api.main(["--data-dir", str(data_dir)]) == 0
    assert len(started) == 1


CLI_COMMANDS = {  # every command that opens a data directory, with the arguments it needs; {tmp} is the test's tmp_path
    "seed": ["--scenario", SCENARIO, "--seed", "21001"],
    "status": ["--run-id", "run_none"],
    "diagnose": ["--run-id", "run_none", "--mode", "rules"],
    "validate-fix": ["--run-id", "run_none", "--fix-id", "payload@5", "--diagnosis-id", "diag_none",
                     "--operator", OPERATOR],
    "plan": ["--run-id", "run_none", "--fix-validation-id", "val_none"],
    "approve": ["--run-id", "run_none", "--plan-id", "plan_none", "--plan-hash", "0" * 64, "--operator", OPERATOR],
    "execute": ["--run-id", "run_none", "--plan-id", "plan_none"],
    "escalate": ["--run-id", "run_none", "--diagnosis-id", "diag_none", "--operator", OPERATOR],
    "refresh": ["--run-id", "run_none", "--payment-id", "pay_none"],
    "report": ["--run-id", "run_none", "--output", "{tmp}/report.json"],
    "receipt": ["--run-id", "run_none", "--action-id", "act_none", "--output-dir", "{tmp}/receipts"],
    "demo": ["--scenario", SCENARIO, "--seed", "21001", "--model-mode", "rules"],
    "webhook-request": ["--run-id", "run_none", "--payment-id", "pay_none", "--status", "captured",
                        "--event-id", "evt_none", "--output", "{tmp}/body.json"],
}


@pytest.mark.parametrize("command", list(CLI_COMMANDS))
@pytest.mark.parametrize("named_by", ["--data-dir", "WORKBENCH_DATA_DIR"])
def test_cli_commands_refuse_a_directory_public_mode_marked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, named_by: str
) -> None:
    """Security audit run 3: the CLI used a marked directory as it was, so what it wrote there was lost at public
    mode's next start. Each command now refuses it, exits 2 and writes nothing, however the directory is named."""
    data_dir = marked_data_dir(tmp_path)
    argv = [command, *(part.format(tmp=tmp_path) for part in CLI_COMMANDS[command])]
    if named_by == "--data-dir":
        argv += ["--data-dir", str(data_dir)]
    else:
        monkeypatch.setenv("WORKBENCH_DATA_DIR", str(data_dir))
    out, err = StringIO(), StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    assert (code, out.getvalue()) == (cli.EXIT_USAGE_ERROR, "")
    refused = json.loads(err.getvalue().splitlines()[-1])
    assert refused["error"] == "ValueError"
    assert refused["detail"].startswith(f"{data_dir} is public mode's data directory")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["data"]  # no report, receipt or body was written
    assert [path.name for path in data_dir.iterdir()] == [DATA_MARKER]


# --------------------------------------------------------------------------- item 6: no webhook route


def test_public_mode_has_no_webhook_route_and_no_exemption_for_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    assert not [route for route in served.app.routes if isinstance(route, APIRoute) and "webhooks" in route.path]
    with browser(served.app) as client:
        assert log_in(client, served.token).status_code == 200
        run_id = seed(client, SEEDS[0]).json()["run_id"]
        path = f"/api/runs/{run_id}/webhooks/int_SYNTHETIC_MAIN"
        attempts_after_seeding = delivery_attempts(served.app)  # the seeding replay delivered the world's events
        # Without the UI's header the guard refuses it, as any other state change: there is no waiver any more.
        unsigned = client.post(path, content=b'{"x": 1}', headers={"content-type": "application/json"})
        assert unsigned.status_code == 403
        assert "x-workbench-request" in unsigned.json()["detail"]
        # With the header it finds no route, and a body over the 64 KiB limit is refused before anything reads it.
        assert client.post(path, content=b"{}", headers=STATE_CHANGE).status_code in (404, 405)
        oversized = client.post(path, content=b" " * (64 * 1024 + 1), headers=STATE_CHANGE)
        assert oversized.status_code == 413
        assert delivery_attempts(served.app) == attempts_after_seeding  # nothing was received


# --------------------------------------------------------------------------- item 7: limits behind a proxy


def test_public_mode_keeps_the_global_login_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    with browser(served.app) as client:
        codes = [log_in(client, f"wrong-token-{index:02d}-{'x' * 24}").status_code
                 for index in range(api.MAX_FAILED_LOGINS_PER_MINUTE + 1)]
        assert codes == [403] * api.MAX_FAILED_LOGINS_PER_MINUTE + [429]
        assert log_in(client, served.token).status_code == 200  # the right token still gets in


def test_public_mode_limits_expensive_requests_per_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    expensive = ["/api/runs", "/api/runs/run_none/diagnoses", "/api/runs/run_none/fixes/payload@5/validate",
                 "/api/runs/run_none/plans", "/api/runs/run_none/plans/plan_none/execute"]
    with browser(served.app) as busy, closing(browser(served.app)) as other:
        assert log_in(busy, served.token).status_code == 200
        assert log_in(other, served.token, "op_other_viewer").status_code == 200
        # Bodies the routes refuse after the count: the limit counts requests, before any route runs.
        codes = [busy.post(expensive[index % len(expensive)], json={"unexpected": True}, headers=STATE_CHANGE)
                 .status_code for index in range(EXPENSIVE_PER_MINUTE)]
        assert codes == [422, 422, 422, 422, 403] * 2  # execute takes no body, and plan_none is no plan of the run
        for path in expensive:
            refused = busy.post(path, json={}, headers=STATE_CHANGE)
            assert refused.status_code == 429, path
            assert refused.json()["error"] == "RateLimitedError"
            assert refused.headers["retry-after"] == "60"
        # Cheap routes, reads and other sessions are not held back.
        assert busy.post("/api/runs/run_none/advance", json={"by_ms": 1}, headers=STATE_CHANGE).status_code == 403
        assert busy.get("/api/runs").status_code == 200
        assert other.post(expensive[0], json={"unexpected": True}, headers=STATE_CHANGE).status_code == 422
        clock.now += 30
        assert busy.post(expensive[0], json={}, headers=STATE_CHANGE).headers["retry-after"] == "30"
        clock.now += 30.5  # the first ten are more than a minute old
        assert busy.post(expensive[0], json={"unexpected": True}, headers=STATE_CHANGE).status_code == 422


def test_every_post_route_is_classified_for_the_session_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A POST route added later must be placed on one side or the other on purpose."""
    served = serve(monkeypatch, tmp_path / "data")
    limited = {route.path for route in served.app.routes
               if isinstance(route, APIRoute) and "POST" in route.methods
               and api.EXPENSIVE_PATH.fullmatch(route.path.format(**dict.fromkeys(route.param_convertors, "x")))}
    unlimited = {route.path for route in served.app.routes
                 if isinstance(route, APIRoute) and route.methods & {"POST", "DELETE", "PUT", "PATCH"}} - limited
    assert limited == {"/api/runs", "/api/runs/{run_id}/diagnoses", "/api/runs/{run_id}/fixes/{fix_id}/validate",
                       "/api/runs/{run_id}/plans", "/api/runs/{run_id}/plans/{plan_id}/execute"}
    assert unlimited == {"/api/session", "/api/runs/{run_id}/advance", "/api/runs/{run_id}/plans/{plan_id}/approve",
                         "/api/runs/{run_id}/refresh"}


def test_per_address_limits_apply_only_behind_named_proxies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proxied = serve(monkeypatch, tmp_path / "proxied", WORKBENCH_PUBLIC_TRUSTED_PROXIES="198.51.100.1")
    with browser(proxied.app, "203.0.113.7") as flooding, closing(browser(proxied.app, "203.0.113.8")) as calm:
        codes = [flooding.get("/api/session").status_code for _ in range(REQUESTS_PER_ADDRESS_PER_MINUTE + 1)]
        assert codes == [200] * REQUESTS_PER_ADDRESS_PER_MINUTE + [429]
        refused = flooding.get("/health")
        assert refused.json()["error"] == "RateLimitedError"
        assert 1 <= int(refused.headers["retry-after"]) <= 60
        assert calm.get("/api/session").status_code == 200
    unproxied = serve(monkeypatch, tmp_path / "unproxied")
    with browser(unproxied.app, "203.0.113.7") as client:
        codes = [client.get("/api/session").status_code for _ in range(REQUESTS_PER_ADDRESS_PER_MINUTE + 1)]
        assert codes == [200] * (REQUESTS_PER_ADDRESS_PER_MINUTE + 1)


def test_public_mode_hands_uvicorn_its_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    alone = serve(monkeypatch, tmp_path / "alone")
    protocol = alone.options["http"]
    assert isinstance(protocol, type)
    assert issubclass(protocol, H11Protocol)
    assert protocol is not H11Protocol  # uvicorn's own sets no deadline for a request that never finishes arriving
    assert alone.options["limit_concurrency"] == LIMIT_CONCURRENCY
    assert alone.options["proxy_headers"] is False  # no client may choose its own address
    assert "forwarded_allow_ips" not in alone.options
    proxied = serve(monkeypatch, tmp_path / "proxied", WORKBENCH_PUBLIC_TRUSTED_PROXIES="10.0.0.0/8, 192.0.2.10")
    assert proxied.options["proxy_headers"] is True
    assert proxied.options["forwarded_allow_ips"] == ["10.0.0.0/8", "192.0.2.10"]


@pytest.mark.parametrize("proxies", ["*", "0.0.0.0/0", "::/0", "10.0.0.1/8", "proxy.example.test", "10.0.0.1,,10.0.0.2"])
def test_trusted_proxies_must_name_addresses_and_never_everyone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proxies: str
) -> None:
    message = refusal(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_TRUSTED_PROXIES=proxies)
    assert message.startswith("WORKBENCH_PUBLIC_TRUSTED_PROXIES must")


@pytest.mark.parametrize(("variables", "start"), [
    ({"WORKBENCH_PUBLIC_RUNS_PER_SESSION": "0"}, "WORKBENCH_PUBLIC_RUNS_PER_SESSION must be a positive integer"),
    ({"WORKBENCH_PUBLIC_RUNS_PER_PROCESS": "-1"}, "WORKBENCH_PUBLIC_RUNS_PER_PROCESS must be a positive integer"),
    ({"WORKBENCH_PUBLIC_RUNS_PER_SESSION": "40"}, "WORKBENCH_PUBLIC_RUNS_PER_SESSION (40) must not exceed"),
    ({"WORKBENCH_PUBLIC_DATA_LIMIT_MB": "lots"}, "WORKBENCH_PUBLIC_DATA_LIMIT_MB must be a positive integer"),
    ({"WORKBENCH_PUBLIC_SEEDS": ""}, "WORKBENCH_PUBLIC_SEEDS must list seeds"),
    ({"WORKBENCH_PUBLIC_SEEDS": "101,x"}, "WORKBENCH_PUBLIC_SEEDS must list seeds"),
    ({"WORKBENCH_PUBLIC_SEEDS": "2147483648"}, "WORKBENCH_PUBLIC_SEEDS must list seeds"),
    ({"WORKBENCH_PUBLIC_SEEDS": "101,101"}, "WORKBENCH_PUBLIC_SEEDS lists a seed twice"),
])
def test_quota_settings_are_checked_at_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variables: dict[str, str], start: str
) -> None:
    assert refusal(monkeypatch, tmp_path / "data", **variables).startswith(start)


# --------------------------------------------------------------------------- item 11: one log line per event


def test_public_mode_logs_each_login_run_quota_refusal_and_429_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=EVENT_LOGGER)
    served = serve(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_RUNS_PER_SESSION="1")
    with browser(served.app) as client:
        assert log_in(client, "a-wrong-token-" + "y" * 30).status_code == 403
        assert log_in(client, served.token).status_code == 200
        created = seed(client, SEEDS[0])
        assert created.status_code == 201
        assert seed(client, SEEDS[1]).status_code == 429  # the session's one run is used
        assert seed(client, 20799).status_code == 422  # not published
        for _ in range(EXPENSIVE_PER_MINUTE - 3):
            client.post("/api/runs/run_none/plans", json={}, headers=STATE_CHANGE)
        assert client.post("/api/runs/run_none/plans", json={}, headers=STATE_CHANGE).status_code == 429
    lines = events(caplog)
    run_id = created.json()["run_id"]
    assert [line.split()[1] for line in lines] == ["event=login", "event=login", "event=run_created",
                                                  "event=quota_refused", "event=quota_refused", "event=rate_limited"]
    assert "result=wrong_token status=403 operator=op_public_viewer" in lines[0]
    assert "result=ok status=200 operator=op_public_viewer session=s_" in lines[1]
    assert f"run={run_id} scenario=worker_backlog seed=20701 session=s_" in lines[2]
    assert "reason=session_runs status=429" in lines[3]
    assert "reason=seed_not_offered status=422 seed=20799" in lines[4]
    limited = re.search(r"limit=session status=429 method=POST path=/api/runs/run_none/plans retry_after=(\d+) session=s_",
                        lines[5])
    # The window opened at the first counted request; seeding takes about a second on a slow machine, so up to 60.
    assert limited is not None and 1 <= int(limited.group(1)) <= 60
    session_labels = {part for line in lines for part in line.split() if part.startswith("session=")}
    assert len(session_labels) == 1  # one session, named the same way in every line
    assert all(line.startswith("public-mode event=") for line in lines)


def test_with_public_mode_off_no_event_is_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A control: the loopback server logs none of these events, as before."""
    from test_api import STATE_CHANGE as LOOPBACK_CHANGE
    from test_api import TOKEN, make_client

    caplog.set_level(logging.INFO, logger=EVENT_LOGGER)
    with make_client(tmp_path) as client:
        client.post("/api/session", json={"token": "wrong", "operator_id": OPERATOR}, headers=LOOPBACK_CHANGE)
        client.post("/api/session", json={"token": TOKEN, "operator_id": OPERATOR}, headers=LOOPBACK_CHANGE)
        assert client.post("/api/runs", json={"scenario": SCENARIO, "seed": SEEDS[0]},
                           headers=LOOPBACK_CHANGE).status_code == 201
    assert events(caplog) == []


# --------------------------------------------------------------------------- item 12: a minimal /health


def test_public_health_answers_its_status_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = serve(monkeypatch, tmp_path / "data")
    with browser(served.app) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json() == {"status": "ok"}
        assert client.get("/health", headers={"host": "127.0.0.1:8000"}).status_code == 403


# --------------------------------------------------------------------------- security audit run 3: headers, repr


def test_public_mode_sends_strict_transport_security_with_every_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Security audit run 3: without it, a viewer who later types the name without https:// can be served a plain
    HTTP page that asks for the demo token. Every answer carries it: pages, API answers, refusals and errors."""
    served = serve(monkeypatch, tmp_path / "data")
    with browser(served.app) as client:
        answers = {
            "health": client.get("/health"),
            "no session": client.get("/api/runs"),
            "wrong host": client.get("/health", headers={"host": "other.example.test"}),
            "wrong token": log_in(client, "a-wrong-token-" + "w" * 30),
            "login": log_in(client, served.token),
            "api": client.get("/api/runs"),
            "page": client.get("/"),
            "not found": client.get("/no-such-page"),
            "too large": client.post("/api/runs", content=b" " * (64 * 1024 + 1), headers=STATE_CHANGE),
        }
    assert {name: answer.headers.get("strict-transport-security") for name, answer in answers.items()} == dict.fromkeys(
        answers, "max-age=31536000")


def test_with_public_mode_off_no_answer_carries_strict_transport_security(tmp_path: Path) -> None:
    """A control: the loopback server, plain HTTP, sends the same headers as before and no HSTS."""
    from test_api import make_client

    with make_client(tmp_path) as client:
        for answer in (client.get("/health"), client.get("/api/runs"), client.get("/health", headers={"host": HOST})):
            assert "strict-transport-security" not in answer.headers
            assert {name: answer.headers[name] for name in api.SECURITY_HEADERS} == api.SECURITY_HEADERS


def test_the_server_configuration_shows_no_token_in_its_repr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Security audit run 3: ApiConfig's repr showed the demo token, though PublicSettings hides the same value."""
    served = serve(monkeypatch, tmp_path / "data")
    config = served.app.state.workbench.config
    assert config.operator_token == served.token
    for shown in (repr(config), str(config), f"{config!r}", repr(served.app.state.workbench.public)):
        assert served.token not in shown
    loopback_token = "loopback-operator-token-" + secrets.token_hex(16)
    hosts, origins = api.local_origins(8000)
    loopback = api.ApiConfig(data_dir=tmp_path / "loopback", operator_token=loopback_token, allowed_hosts=hosts,
                             allowed_origins=origins)
    assert loopback_token not in repr(loopback)
    assert "data_dir=" in repr(loopback)  # the rest is still shown


# --------------------------------------------------------------------------- the whole of Flow A, as a viewer


def test_a_viewer_runs_flow_a_through_public_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every step the UI takes still works behind public mode's guard, cookie and limits."""
    from test_api import diagnose, get, post

    served = serve(monkeypatch, tmp_path / "data")
    with browser(served.app) as client:
        client.headers.update(STATE_CHANGE)  # the browser's Origin, on every request this client sends
        assert log_in(client, served.token).status_code == 200
        run_id = post(client, "/api/runs", {"scenario": SCENARIO, "seed": SEEDS[0]}, expect=201)["run_id"]
        found = diagnose(client, run_id)["diagnosis"]
        validation = post(client, f"/api/runs/{run_id}/fixes/payload@5/validate", {"diagnosis_id": found["diagnosis_id"]})
        plan = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
        post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
        executed = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute")
        receipt = get(client, f"/api/runs/{run_id}/actions/{executed['action_id']}/receipt")
    assert executed["status"] == "succeeded"
    assert receipt["identity"]["action_id"] == executed["action_id"]
