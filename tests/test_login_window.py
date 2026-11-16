"""The failed-login window throttles wrong operator tokens only (security audit finding 6, owner decision Q38).

``POST /api/session`` counts wrong tokens in one window per server process: every caller on 127.0.0.1 has the same
address, so the window cannot tell callers apart. A full window used to refuse the right token too, so any local
program without the token could keep the operator from logging in with a burst of wrong tokens each minute. The token
is now compared first. The right one always gets a session; a wrong one is answered 403, and 429 while the last minute
holds ``api.MAX_FAILED_LOGINS_PER_MINUTE`` wrong ones. The window does not protect the token: only ``api.main`` draws
it, 256 random bits that no request rate can guess.

The controls pin what did not change: the limit, the minute, a refusal that takes no slot, and a session that lasts
until logout.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from test_api import OPERATOR, STATE_CHANGE, TOKEN, make_client

from recovery_workbench import api

LIMIT = api.MAX_FAILED_LOGINS_PER_MINUTE
SESSION_ROUTE = "/api/session"
GATED_ROUTE = "/api/runs"  # needs an operator session


@dataclass
class LoginClock:
    """The monotonic clock the API reads, set by the test."""

    now: float = 1000.0


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> LoginClock:
    login_clock = LoginClock()
    # Only api's own name for the time module is replaced, so nothing else sees the test's clock.
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=lambda: login_clock.now))
    return login_clock


@pytest.fixture
def clients(tmp_path: Path) -> Iterator[tuple[TestClient, TestClient]]:
    """The operator and another local program without the token: one app, two cookie jars, the same address."""
    with make_client(tmp_path) as operator, closing(TestClient(operator.app)) as stranger:
        yield operator, stranger


def log_in(client: TestClient, token: str) -> httpx.Response:
    return client.post(SESSION_ROUTE, json={"token": token, "operator_id": OPERATOR}, headers=STATE_CHANGE)


def wrong_logins(client: TestClient, count: int) -> list[int]:
    """The status of each of ``count`` logins with a wrong token."""
    return [log_in(client, f"wrong-token-{index}").status_code for index in range(count)]


def wrong_login_at(client: TestClient, clock: LoginClock, seconds: float) -> int:
    clock.now = seconds
    return log_in(client, "wrong-token").status_code


# --------------------------------------------------------------------------- the right token is never refused


def test_wrong_tokens_from_another_client_never_refuse_the_operators_right_token(
    clients: tuple[TestClient, TestClient],
) -> None:
    operator, stranger = clients
    assert wrong_logins(stranger, LIMIT + 1) == [403] * LIMIT + [429]
    response = log_in(operator, TOKEN)
    assert response.status_code == 200, response.text
    assert api.SESSION_COOKIE in response.cookies
    assert operator.get(GATED_ROUTE).status_code == 200  # the session works
    assert stranger.get(GATED_ROUTE).status_code == 403  # and the stranger still has none


def test_a_right_token_neither_takes_nor_frees_a_slot_in_a_full_window(
    clients: tuple[TestClient, TestClient], clock: LoginClock,
) -> None:
    operator, stranger = clients
    assert wrong_logins(stranger, LIMIT) == [403] * LIMIT  # at 1000 s: full
    clock.now = 1030.0
    assert log_in(operator, TOKEN).status_code == 200
    assert wrong_logins(stranger, 1) == [429]  # it freed no slot
    clock.now = 1060.25  # the failures from 1000 s have left
    assert wrong_logins(stranger, LIMIT + 1) == [403] * LIMIT + [429]  # and it took none


# --------------------------------------------------------------------------- unchanged: the limit, the minute, sessions


def test_wrong_tokens_still_get_403_below_the_limit_and_429_at_it_without_a_cookie(
    clients: tuple[TestClient, TestClient],
) -> None:
    _, stranger = clients
    responses = [log_in(stranger, f"wrong-token-{index}") for index in range(LIMIT + 1)]
    assert [(response.status_code, response.json()["error"]) for response in responses] == [
        *[(403, "ScopeError")] * LIMIT, (429, "TooManyLoginsError"),
    ]
    assert not any("set-cookie" in response.headers for response in responses)


def test_each_failure_still_leaves_the_window_once_it_is_more_than_a_minute_old(
    clients: tuple[TestClient, TestClient], clock: LoginClock,
) -> None:
    _, stranger = clients
    tried_at = [1000.0 + second for second in range(LIMIT)] + [1030.0, 1060.0, 1060.25, 1060.5, 1061.25]
    assert [wrong_login_at(stranger, clock, seconds) for seconds in tried_at] == [
        *[403] * LIMIT,  # one a second from 1000 s fills the window
        429,  # 1030: full, and a refused login takes no slot
        429,  # 1060: the failure from 1000 s is exactly a minute old, so it still counts
        403,  # 1060.25: it has left, so exactly one more fits
        429,  # 1060.5: full again until the failure from 1001 s leaves
        403,  # 1061.25: it has left
    ]


def test_a_session_still_lasts_until_logout_whatever_the_window_or_the_clock_does(
    clients: tuple[TestClient, TestClient], clock: LoginClock,
) -> None:
    operator, stranger = clients
    assert log_in(operator, TOKEN).status_code == 200
    assert wrong_logins(stranger, LIMIT + 1)[-1] == 429
    assert operator.get(GATED_ROUTE).status_code == 200  # a full window ends no session
    clock.now += 24 * 60 * 60
    assert operator.get(GATED_ROUTE).status_code == 200  # nor does a day: a session has no lifetime of its own
    assert operator.delete(SESSION_ROUTE, headers=STATE_CHANGE).status_code == 200
    assert operator.get(GATED_ROUTE).status_code == 403
    assert operator.get(SESSION_ROUTE).json() == {"operator_id": None}
