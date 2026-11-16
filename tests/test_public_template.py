"""Public mode's demo runs, built into the production image (owner answer to Q48; ``public_template``).

The image build seeds a template of demo runs, and public mode copies it into its emptied data directory at every
start. Each test starts the server as a host would, as test_public_mode does (the variables set, then ``api.main`` with
``uvicorn.run`` replaced), and drives it as a viewer's browser on https://demo.example.test.

The templates are made by ``build_template`` below, which says what the image's build makes using only the seeding
code that existed before Q48. So every test of the server fails on code without the change by what the server does, not
by a missing import, and ``test_the_image_build_makes_exactly_that_template`` pins the real build to it. New seeded runs
use seeds 20901 to 21000; the receipt test uses the README's 101, the seed the demo receipt is known for.
"""
from __future__ import annotations

import errno
import hashlib
import importlib
import logging
import os
import secrets
import shutil
import sqlite3
import stat
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

import pytest
from conftest import seed_run
from fastapi.testclient import TestClient
from test_api import diagnose, get, post
from test_public_mode import (
    DATA_MARKER,
    DATA_MARKER_TEXT,
    EVENT_LOGGER,
    STATE_CHANGE,
    Served,
    browser,
    events,
    log_in,
    no_public_variables,  # noqa: F401  (an autouse fixture: no public variable leaks in from the environment)
    refusal,
    run_ids,
    seed,
    serve,
    set_variables,
)

from recovery_workbench import api, cli, storage
from recovery_workbench.contracts import ModelMode
from recovery_workbench.simulator import driver, scenarios
from recovery_workbench.simulator.oracle import observable_dataset_hash
from recovery_workbench.simulator.world import GENERATOR_VERSION, generate_world, run_id_for

TEMPLATE_SEED = 20901
OTHER_SEED = 20902  # published beside the template's seed in some tests, and never in a template
DEMO_SEED = 101
DEMO_RECEIPT = "eb9d6f30f04d9ebc6694a7f153cc60d4465459c60ba07b6f698b7a3304c6afa6"  # README, Flow A in rules mode
# In the README's flow order, as the template seeds them. Since receipt-2 (Q52) the order no longer decides the receipt:
# its hash leaves out the database's inbox row numbers, so Flow A gives the README's receipt wherever its run comes.
DEMO_SCENARIOS = ("worker_backlog", "method_degradation", "late_authorization")
DEMO_OPERATOR = "op_demo_operator"  # the receipt names the operator who approved, so the README demo's
DATABASE = "workbench.sqlite"
HOSTED_VARIABLES = {  # a hosted model that is never called: seeding and the start call no model
    "WORKBENCH_MODEL_MODE": "hosted", "WORKBENCH_HOSTED_URL": "https://hosted.example.test/v1",
    "WORKBENCH_HOSTED_MODEL": "@cf/openai/gpt-oss-20b",
}


# --------------------------------------------------------------------------- helpers


def build_template(directory: Path, seeds: Iterable[int]) -> Path:
    """A template as the image's build makes one: every demo scenario with each seed, lowest seed first and the
    scenarios in flow order, seeded as public mode seeds a run but in rules mode and with no oracle file, the database
    closed out of WAL mode; then read-only, as root's template is to the server."""
    directory.mkdir()
    with closing(storage.connect(directory / DATABASE)) as conn:
        for seed_value in sorted(seeds):
            for name in DEMO_SCENARIOS:
                driver.seed_world(conn, generate_world(scenarios.demo_scenario(name), seed_value), oracle_dir=None,
                                  provider_dir=directory / cli.PROVIDER_DIRECTORY, model_mode=ModelMode.RULES)
        conn.execute("PRAGMA journal_mode = DELETE")
    for path in sorted(directory.rglob("*"), reverse=True):  # children before their directories
        path.chmod(0o555 if path.is_dir() else 0o444)
    directory.chmod(0o555)
    return directory


@pytest.fixture(scope="module")
def template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The demo runs with seed 20901, as the image's build makes them. No test writes to it."""
    return build_template(tmp_path_factory.mktemp("template") / "demo-runs", [TEMPLATE_SEED])


@pytest.fixture(scope="module")
def demo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The image's default template: the demo runs with the README's seed 101."""
    return build_template(tmp_path_factory.mktemp("demo-template") / "demo-runs", [DEMO_SEED])


def template_runs(seed_value: int) -> list[str]:
    return sorted(run_id_for(scenarios.demo_scenario(name), seed_value) for name in DEMO_SCENARIOS)


def tree_digest(directory: Path) -> str:
    """Every entry's path, mode bits and bytes, so any write to the tree or change of its modes shows."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*")):
        status = path.lstat()
        digest.update(f"{path.relative_to(directory)}|{stat.S_IMODE(status.st_mode):o}|".encode())
        if stat.S_ISREG(status.st_mode):
            digest.update(path.read_bytes())
    return digest.hexdigest()


def listing(directory: Path) -> list[str]:
    return sorted(str(path.relative_to(directory)) for path in directory.rglob("*"))


def read_only(database: Path) -> sqlite3.Connection:
    """The database as it lies, written by nothing: a read-only template cannot take the WAL switch connect() makes."""
    return sqlite3.connect(f"file:{database}?mode=ro&immutable=1", uri=True)


def observable(database: Path, run_id: str) -> tuple[str, str]:
    """The run's observable rows, as one hash (model_mode included), and the mode it records."""
    with closing(read_only(database)) as conn:
        mode = conn.execute("SELECT model_mode FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
        return observable_dataset_hash(conn, run_id), str(mode)


def model_variables(mode: str) -> dict[str, str]:
    if mode == "hosted":
        return {**HOSTED_VARIABLES, "WORKBENCH_HOSTED_API_KEY": secrets.token_urlsafe(24)}
    return {"WORKBENCH_MODEL_MODE": mode}


def documents(client: TestClient, run_id: str) -> dict[str, object]:
    """What a viewer reads of a run before acting on it, less two wall-clock times: when the run was seeded (the
    image's build, for a copied run) and when the report was exported."""
    run = get(client, f"/api/runs/{run_id}")
    del run["created_wall_ms"]
    report = get(client, f"/api/runs/{run_id}/report")
    del report["clocks"]["exported_wall_ms"]
    queue = get(client, f"/api/runs/{run_id}/orders?limit={api.MAX_QUEUE_PAGE_LIMIT}")
    cases = [get(client, f"/api/runs/{run_id}/orders/{row['order_id']}") for row in queue["orders"][:3]]
    return {"run": run, "incidents": get(client, f"/api/runs/{run_id}/incidents"),
            "payment_state": get(client, f"/api/runs/{run_id}/payment-state"), "queue": queue, "cases": cases,
            "report": report}


def recover(served: Served, *, opened_with: int) -> dict:
    """Flow A in rules mode on worker_backlog with seed 101, step for step as the README's demo: its receipt."""
    with browser(served.app) as client:
        client.headers.update(STATE_CHANGE)  # the browser's Origin, on every request this client sends
        assert log_in(client, served.token, DEMO_OPERATOR).status_code == 200
        opened = post(client, "/api/runs", {"scenario": "worker_backlog", "seed": DEMO_SEED}, expect=opened_with)
        run_id = opened["run_id"]
        found = diagnose(client, run_id, mode="rules")["diagnosis"]
        validation = post(client, f"/api/runs/{run_id}/fixes/payload@5/validate",
                          {"diagnosis_id": found["diagnosis_id"]})
        draft = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
        stale = [payment for item in draft["stale_only_orders"] for payment in item["payment_ids"]]
        post(client, f"/api/runs/{run_id}/refresh", {"payment_ids": stale})
        plan = post(client, f"/api/runs/{run_id}/plans", {"fix_validation_id": validation["validation_id"]})
        post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/approve", {"plan_hash": plan["plan_hash"]})
        executed = post(client, f"/api/runs/{run_id}/plans/{plan['plan_id']}/execute")
        assert executed["status"] == "succeeded"
        return get(client, f"/api/runs/{run_id}/actions/{executed['action_id']}/receipt")


def earlier_start(data_dir: Path) -> None:
    """A data directory as an earlier public-mode process left it: its marker and a database file."""
    data_dir.mkdir()
    (data_dir / DATA_MARKER).write_text(DATA_MARKER_TEXT, encoding="utf-8")
    (data_dir / DATABASE).write_bytes(b"an earlier start's database")


# --------------------------------------------------------------------------- copied at every start


def test_public_mode_copies_the_demo_runs_in_at_every_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], template: Path
) -> None:
    before = tree_digest(template)
    data_dir = tmp_path / "data"
    variables = {"WORKBENCH_PUBLIC_TEMPLATE_DIR": str(template), "WORKBENCH_PUBLIC_SEEDS": str(TEMPLATE_SEED)}
    served = serve(monkeypatch, data_dir, **variables)
    assert run_ids(served.app) == template_runs(TEMPLATE_SEED)
    printed = capsys.readouterr().out
    assert (f"Demo runs: 3 copied from {template} (late_authorization {TEMPLATE_SEED}, method_degradation "
            f"{TEMPLATE_SEED}, worker_backlog {TEMPLATE_SEED}; ") in printed
    assert "of the data limit), recorded in model mode rules; seeded when a viewer asks: none" in printed
    # The database and the provider records the server reads; no oracle file, which only the evaluation reads.
    names = {path.name for path in data_dir.iterdir()} - {f"{DATABASE}-wal", f"{DATABASE}-shm"}
    assert names == {DATA_MARKER, DATABASE, cli.PROVIDER_DIRECTORY}
    with browser(served.app) as client:
        client.headers.update(STATE_CHANGE)
        assert log_in(client, served.token).status_code == 200
        moved = post(client, f"/api/runs/{template_runs(TEMPLATE_SEED)[0]}/advance", {"by_ms": 60_000})
    restarted = serve(monkeypatch, data_dir, **variables)
    with browser(restarted.app) as client:
        assert log_in(client, restarted.token).status_code == 200
        again = get(client, f"/api/runs/{moved['run_id']}")
    assert again["replay_clock_ms"] == moved["replay_clock_ms"] - 60_000  # a fresh copy, not the run as it was left
    assert run_ids(restarted.app) == template_runs(TEMPLATE_SEED)
    assert tree_digest(template) == before  # the server never wrote to the template


def test_a_viewer_opens_a_demo_run_at_once_and_it_costs_no_run_quota(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, template: Path
) -> None:
    caplog.set_level(logging.INFO, logger=EVENT_LOGGER)
    served = serve(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_TEMPLATE_DIR=str(template),
                   WORKBENCH_PUBLIC_SEEDS=f"{TEMPLATE_SEED},{OTHER_SEED}", WORKBENCH_PUBLIC_RUNS_PER_SESSION="1",
                   WORKBENCH_PUBLIC_RUNS_PER_PROCESS="1")
    with browser(served.app) as first, closing(browser(served.app)) as second:
        assert log_in(first, served.token).status_code == 200
        assert log_in(second, served.token, "op_second_viewer").status_code == 200
        for name in DEMO_SCENARIOS:
            opened = seed(first, TEMPLATE_SEED, name)
            assert opened.status_code == 200, opened.text  # there already, as if another viewer had seeded it
            assert opened.json() == first.get(f"/api/runs/{opened.json()['run_id']}").json()
            assert seed(second, TEMPLATE_SEED, name).status_code == 200
        assert seed(first, OTHER_SEED).status_code == 201  # the session's one new run, and the process's
        over_session = seed(first, OTHER_SEED, "late_authorization")
        assert over_session.status_code == 429
        assert "the most public mode allows a session" in over_session.json()["detail"]
        over_process = seed(second, OTHER_SEED, "late_authorization")
        assert over_process.status_code == 429
        assert "this server has created 1 runs" in over_process.json()["detail"]
    created = [line for line in events(caplog) if line.split()[1] == "event=run_created"]
    assert len(created) == 1
    assert f"scenario=worker_backlog seed={OTHER_SEED}" in created[0]
    assert "runs_in_process=1" in created[0]


def test_demo_runs_count_toward_the_data_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: Path) -> None:
    served = serve(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_TEMPLATE_DIR=str(template),
                   WORKBENCH_PUBLIC_SEEDS=f"{TEMPLATE_SEED},{OTHER_SEED}", WORKBENCH_PUBLIC_DATA_LIMIT_MB="1")
    with browser(served.app) as client:
        assert log_in(client, served.token).status_code == 200
        refused = seed(client, OTHER_SEED)  # the demo runs alone hold more than 1 MB
        assert refused.status_code == 429
        assert refused.json()["error"] == "QuotaExceededError"
        assert "at public mode's limit of 1 MB" in refused.json()["detail"]
        assert seed(client, TEMPLATE_SEED).status_code == 200  # the demo runs still open
    assert run_ids(served.app) == template_runs(TEMPLATE_SEED)


# --------------------------------------------------------------------------- the same runs as seeding at run time


@pytest.mark.parametrize("mode", ["rules", "hosted"])
def test_a_copied_run_is_the_run_public_mode_seeds_at_run_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: Path, mode: str
) -> None:
    """In any model mode the server settles on: the template is seeded in rules, and each copy records the server's.

    The viewer here seeds in the template's order, which gives each run the inbox row numbers it has in the template:
    inbox observations are named by them, per-database record IDs that documents and the hashes over them carry.
    """
    variables = {"WORKBENCH_PUBLIC_SEEDS": str(TEMPLATE_SEED), **model_variables(mode)}
    copied = serve(monkeypatch, tmp_path / "copied", WORKBENCH_PUBLIC_TEMPLATE_DIR=str(template), **variables)
    seeded = serve(monkeypatch, tmp_path / "seeded", **variables)
    with browser(copied.app) as on_copy, browser(seeded.app) as on_seed:
        assert log_in(on_copy, copied.token).status_code == 200
        assert log_in(on_seed, seeded.token).status_code == 200
        for name in DEMO_SCENARIOS:
            opened, created = seed(on_copy, TEMPLATE_SEED, name), seed(on_seed, TEMPLATE_SEED, name)
            assert (opened.status_code, created.status_code) == (200, 201)
            run_id = opened.json()["run_id"]
            assert created.json()["run_id"] == run_id
            assert opened.json()["model_mode"] == mode
            assert documents(on_copy, run_id) == documents(on_seed, run_id)
    for run_id in template_runs(TEMPLATE_SEED):
        copy = observable(tmp_path / "copied" / DATABASE, run_id)
        assert copy == observable(tmp_path / "seeded" / DATABASE, run_id)  # every observable row, the mode included
        assert copy[1] == mode
        for name in (f"{run_id}.json", f"{run_id}.deliveries.json"):  # what a refresh and provider replay read
            relative = Path(cli.PROVIDER_DIRECTORY) / name
            assert (tmp_path / "copied" / relative).read_bytes() == (tmp_path / "seeded" / relative).read_bytes()


def test_a_recovery_on_a_copied_run_gives_the_demo_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, demo_template: Path
) -> None:
    before = tree_digest(demo_template)
    copied = serve(monkeypatch, tmp_path / "copied", WORKBENCH_PUBLIC_TEMPLATE_DIR=str(demo_template),
                   WORKBENCH_PUBLIC_SEEDS=str(DEMO_SEED))
    seeded = serve(monkeypatch, tmp_path / "seeded", WORKBENCH_PUBLIC_SEEDS=str(DEMO_SEED))
    on_copy = recover(copied, opened_with=200)
    on_seeded = recover(seeded, opened_with=201)
    assert on_copy["receipt_hash"] == on_seeded["receipt_hash"] == DEMO_RECEIPT
    assert on_copy["identity"]["action_id"] != on_seeded["identity"]["action_id"]  # record IDs are per database
    assert tree_digest(demo_template) == before  # the recovery wrote to the copy only


# --------------------------------------------------------------------------- the template and the published seeds


def test_a_published_seed_the_template_lacks_is_seeded_when_a_viewer_asks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], template: Path
) -> None:
    served = serve(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_TEMPLATE_DIR=str(template),
                   WORKBENCH_PUBLIC_SEEDS=f"{TEMPLATE_SEED},{OTHER_SEED}")
    assert (f"seeded when a viewer asks: late_authorization {OTHER_SEED}, method_degradation {OTHER_SEED}, "
            f"worker_backlog {OTHER_SEED}") in capsys.readouterr().out
    with browser(served.app) as client:
        assert log_in(client, served.token).status_code == 200
        assert seed(client, OTHER_SEED).status_code == 201
        assert seed(client, TEMPLATE_SEED).status_code == 200
    assert len(run_ids(served.app)) == 4


def test_a_demo_run_of_a_seed_left_unpublished_refuses_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: Path
) -> None:
    """Never a run a viewer could not ask for: a deploy that publishes other seeds than the image's build says so."""
    message = refusal(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_TEMPLATE_DIR=str(template),
                      WORKBENCH_PUBLIC_SEEDS=f"{OTHER_SEED},20903")
    assert message.startswith(f"the demo-run template {template} holds runs for seeds [{TEMPLATE_SEED}], which "
                              f"WORKBENCH_PUBLIC_SEEDS ([{OTHER_SEED}, 20903]) does not publish")
    assert "--build-arg WORKBENCH_PUBLIC_SEEDS=" in message
    assert message.endswith("leave WORKBENCH_PUBLIC_TEMPLATE_DIR empty to seed every run when a viewer asks")


@pytest.mark.parametrize("content", ["no runs", "a run of another scenario", "a run another generator made"])
def test_a_template_without_runs_a_viewer_could_seed_refuses_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    template_dir = tmp_path / "template"
    template_dir.mkdir()
    with closing(storage.connect(template_dir / DATABASE)) as conn:
        storage.migrate_simulator(conn)
        if content == "a run of another scenario":
            seed_run(conn, "run_unit_a", "merchant_SYNTHETIC_A", "int_SYNTHETIC_A", "fixture_secret_a")
        elif content == "a run another generator made":  # worker_backlog with the template's seed, but not its run ID
            with storage.write_transaction(conn):
                storage.create_run(conn, storage.RunRecord(
                    run_id="run_0123456789", seed=TEMPLATE_SEED, scenario="worker_backlog", split="demo",
                    merchant_id="merchant_SYNTHETIC_A", generator_version=f"{GENERATOR_VERSION}-older",
                    clock_ms=0, model_mode=ModelMode.RULES))
        conn.execute("PRAGMA journal_mode = DELETE")
    message = refusal(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_TEMPLATE_DIR=str(template_dir),
                      WORKBENCH_PUBLIC_SEEDS=str(TEMPLATE_SEED))
    expected = {
        "no runs": f"the demo-run template {template_dir} holds no runs; rebuild the image",
        "a run of another scenario": (f"the demo-run template {template_dir} holds run run_unit_a (scenario 'unit', "
                                      "seed 7), which is not the demo run this server seeds for that scenario"),
        "a run another generator made": (f"the demo-run template {template_dir} holds run run_0123456789 (scenario "
                                         f"'worker_backlog', seed {TEMPLATE_SEED}), which is not the demo run"),
    }[content]
    assert message.startswith(expected)


# --------------------------------------------------------------------------- only regular files and directories


@pytest.mark.parametrize("case", [
    "a link to a file", "a link to a directory", "a named pipe", "a link as the template", "missing", "a file",
    "no database", "a -wal beside the database", "a -shm beside the database", "public mode's marker",
    "inside the data directory", "holding the data directory",
])
def test_a_template_that_is_not_a_clean_tree_of_regular_files_refuses_the_start_and_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    data_dir = tmp_path / "data"
    template_dir = tmp_path / "template"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "notes.txt").write_text("outside the template", encoding="utf-8")
    if case == "holding the data directory":
        template_dir.mkdir()
        data_dir = template_dir / "data"
    earlier_start(data_dir)
    if case == "inside the data directory":
        template_dir = data_dir / "demo-runs"
    if case not in ("missing", "a file", "a link as the template"):
        template_dir.mkdir(exist_ok=True)
        if case != "no database":
            (template_dir / DATABASE).write_bytes(b"")  # never opened: each case is refused before the copy
        (template_dir / cli.PROVIDER_DIRECTORY).mkdir()
    shown = {
        "a link to a file": "holds provider/run.json, which is a symbolic link; a template holds only regular files",
        "a link to a directory": "holds oracle, which is a symbolic link; a template holds only regular files",
        "a named pipe": "holds pipe, which is a named pipe; a template holds only regular files",
        "a link as the template": "is a symbolic link, not a directory",
        "missing": "does not exist",
        "a file": "is a regular file, not a directory",
        "no database": f"holds no {DATABASE}",
        "a -wal beside the database": f"holds {DATABASE}-wal, which no template built cleanly holds",
        "a -shm beside the database": f"holds {DATABASE}-shm, which no template built cleanly holds",
        "public mode's marker": f"holds {DATA_MARKER}, which no template built cleanly holds",
        "inside the data directory": f"is not apart from the data directory {data_dir}",
        "holding the data directory": f"is not apart from the data directory {data_dir}",
    }[case]
    if case == "a link to a file":
        (template_dir / cli.PROVIDER_DIRECTORY / "run.json").symlink_to(outside / "notes.txt")
    elif case == "a link to a directory":
        (template_dir / "oracle").symlink_to(outside, target_is_directory=True)
    elif case == "a named pipe":
        os.mkfifo(template_dir / "pipe")
    elif case == "a link as the template":
        real = tmp_path / "real-template"
        real.mkdir()
        (real / DATABASE).write_bytes(b"")
        template_dir.symlink_to(real, target_is_directory=True)
    elif case == "a file":
        template_dir.write_bytes(b"not a directory")
    elif case.startswith("a -"):
        (template_dir / f"{DATABASE}-{case.split()[1][1:]}").write_bytes(b"")
    elif case == "public mode's marker":
        (template_dir / DATA_MARKER).write_text(DATA_MARKER_TEXT, encoding="utf-8")
    before = tree_digest(tmp_path)
    message = refusal(monkeypatch, data_dir, WORKBENCH_PUBLIC_TEMPLATE_DIR=str(template_dir))
    assert message.startswith(f"WORKBENCH_PUBLIC_TEMPLATE_DIR names {template_dir}, which {shown}")
    assert message.endswith("Nothing was deleted")
    assert tree_digest(tmp_path) == before  # refused before the data directory was touched
    assert (data_dir / DATABASE).read_bytes() == b"an earlier start's database"


def start_failure(monkeypatch: pytest.MonkeyPatch, data_dir: Path, variables: dict[str, str | None]) -> str:
    """The message of the OSError api.main stops with; the server never started."""
    set_variables(monkeypatch, variables)
    started: list[dict[str, object]] = []
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **options: started.append(options))
    with pytest.raises(OSError) as failed:
        api.main(["--data-dir", str(data_dir)])
    assert started == []
    return str(failed.value)


def test_a_failed_copy_stops_the_start_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, template: Path
) -> None:
    def no_space(*_: object, **__: object) -> None:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))

    monkeypatch.setattr(shutil, "copyfileobj", no_space)
    message = start_failure(monkeypatch, tmp_path / "data", {"WORKBENCH_PUBLIC_TEMPLATE_DIR": str(template),
                                                            "WORKBENCH_PUBLIC_SEEDS": str(TEMPLATE_SEED)})
    assert message.startswith(f"public mode could not copy the demo-run template {template} into {tmp_path / 'data'}: ")
    assert os.strerror(errno.ENOSPC) in message
    assert message.endswith("or leave WORKBENCH_PUBLIC_TEMPLATE_DIR empty to seed every run when a viewer asks")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a directory whatever its mode bits say")
def test_a_template_the_server_cannot_read_stops_the_start_and_deletes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template_dir = tmp_path / "template"
    hidden = template_dir / cli.PROVIDER_DIRECTORY
    hidden.mkdir(parents=True)
    (template_dir / DATABASE).write_bytes(b"")
    earlier_start(tmp_path / "data")
    hidden.chmod(0)
    try:
        message = start_failure(monkeypatch, tmp_path / "data", {"WORKBENCH_PUBLIC_TEMPLATE_DIR": str(template_dir)})
    finally:
        hidden.chmod(0o755)  # so the test's directory can be removed
    assert message.startswith(f"public mode could not read the demo-run template {template_dir}: ")
    assert os.strerror(errno.EACCES) in message
    assert "Nothing was deleted" in message
    assert (tmp_path / "data" / DATABASE).read_bytes() == b"an earlier start's database"


@pytest.mark.parametrize("value", ["demo-runs", "./demo-runs", "~/demo-runs", "/opt/demo-runs\n"])
def test_the_template_setting_must_be_an_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    message = refusal(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_TEMPLATE_DIR=value)
    assert message.startswith("WORKBENCH_PUBLIC_TEMPLATE_DIR must be the absolute path of the demo-run template")
    assert repr(value) in message
    assert not (tmp_path / "data").exists()  # refused before the data directory was touched


# --------------------------------------------------------------------------- off, or no template: as before


def test_with_public_mode_off_the_template_setting_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], template: Path
) -> None:
    """A control, which passes on the code before Q48 too: the loopback server reads no public-mode setting."""
    monkeypatch.setenv("WORKBENCH_PUBLIC_TEMPLATE_DIR", str(template))
    monkeypatch.setenv("WORKBENCH_MODEL_MODE", "rules")
    served: dict[str, object] = {}
    monkeypatch.setattr(api.uvicorn, "run", lambda app, **options: served.update(options, app=app))
    assert api.main(["--data-dir", str(tmp_path / "data")]) == 0
    app = served.pop("app")
    assert served == {"host": "127.0.0.1", "port": 8000, "log_level": "info", "proxy_headers": False}
    assert run_ids(app) == []
    printed = capsys.readouterr().out.splitlines()
    assert len(printed) == 3
    assert printed[2].startswith("Operator token for this server session (shown once): ")
    assert not (tmp_path / "data" / DATA_MARKER).exists()


def test_without_a_template_public_mode_starts_with_no_runs_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A control, which passes on the code before Q48 too: every run is seeded when a viewer asks."""
    served = serve(monkeypatch, tmp_path / "data", WORKBENCH_PUBLIC_SEEDS=str(TEMPLATE_SEED))
    assert run_ids(served.app) == []
    printed = capsys.readouterr().out.splitlines()
    assert [line.split()[0] for line in printed] == ["RayTrace", "Model", "Operator", "Public"]
    with browser(served.app) as client:
        assert log_in(client, served.token).status_code == 200
        assert seed(client, TEMPLATE_SEED).status_code == 201


# --------------------------------------------------------------------------- the image's build


def test_the_image_build_makes_exactly_that_template(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], template: Path
) -> None:
    """``python -m recovery_workbench.public_template <directory>``, the Dockerfile's step, makes what build_template
    makes: the database, closed with no -wal or -shm beside it, and the provider records, from the published seeds."""
    public_template = importlib.import_module("recovery_workbench.public_template")
    monkeypatch.setenv("WORKBENCH_PUBLIC_SEEDS", str(TEMPLATE_SEED))
    built = tmp_path / "demo-runs"
    assert public_template.main([str(built)]) == 0
    printed = capsys.readouterr().out.splitlines()
    assert [line.split(",")[0] for line in printed] == [
        f"Demo run {run_id_for(scenarios.demo_scenario(name), TEMPLATE_SEED)}: {name} with seed {TEMPLATE_SEED}"
        for name in DEMO_SCENARIOS]
    assert listing(built) == listing(template)
    assert DATABASE in listing(built)
    assert not [name for name in listing(built) if name.startswith(f"{DATABASE}-")]
    for run_id in template_runs(TEMPLATE_SEED):
        assert observable(built / DATABASE, run_id) == observable(template / DATABASE, run_id)
        assert observable(built / DATABASE, run_id)[1] == "rules"
    for name in listing(template):
        if (template / name).is_file() and name != DATABASE:
            assert (built / name).read_bytes() == (template / name).read_bytes(), name


def test_the_image_build_refuses_a_directory_that_exists_and_seeds_the_server_would_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    public_template = importlib.import_module("recovery_workbench.public_template")
    existing = tmp_path / "exists"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        public_template.main([str(existing)])
    assert list(existing.iterdir()) == []
    monkeypatch.setenv("WORKBENCH_PUBLIC_SEEDS", f"{TEMPLATE_SEED},{TEMPLATE_SEED}")
    with pytest.raises(ValueError, match="WORKBENCH_PUBLIC_SEEDS lists a seed twice"):
        public_template.main([str(tmp_path / "new")])
    assert not (tmp_path / "new").exists()
