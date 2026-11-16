"""Public mode's demo runs, built into the production image (owner answer to Q48).

Limited to 0.1 CPU, a free host's share, seeding the method_degradation demo run took 23-25 s on an idle machine and up
to 68 s on a busy one, and public mode empties its data directory at every start, so every restart would seed it again.
Instead:

* ``build``, which the Dockerfile runs, seeds every demo scenario with each published seed (WORKBENCH_PUBLIC_SEEDS) into
  a template directory, once, when the image is built. Root owns it, and the server can only read it.
* At every start, ``plan`` checks the template WORKBENCH_PUBLIC_TEMPLATE_DIR names before the data directory is
  touched, and ``install`` copies it in once ``public_mode.claim_data_dir`` has emptied the directory. A viewer who
  asks for a demo run then opens it at once (HTTP 200), as one that another viewer seeded.

What is copied is what public mode's own seeding stores, apart from per-database surrogate IDs and wall-clock times:

* the database and the provider records, as ``driver.seed_world`` writes them when the API seeds a run;
* not the oracle files. Only the generator, the evaluator and tests read them, never the server, and what they hold
  depends on settings the build cannot know: the server's model mode and WORKBENCH_STALE_AFTER_SECONDS;
* each run's ``model_mode`` as the server would record it. The template is seeded in rules, what the image's auto mode
  settles on with no local model and no hosted settings, and ``install`` records the mode this server settled on
  instead, as ``api._seed_public_run`` does for a run it seeds.

Every template run must be one a viewer may seed. A run of a seed that WORKBENCH_PUBLIC_SEEDS leaves out refuses the
start rather than show viewers a run they could not ask for; the runs share one database, so they cannot be copied
apart. A published seed the template lacks is seeded when a viewer asks, as without a template. Template runs take
nothing from the run quotas, but their bytes count toward the data limit, as everything in the data directory does.
"""
from __future__ import annotations

import argparse
import os
import shutil
import stat
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from recovery_workbench import cli, public_mode, settings, storage
from recovery_workbench.contracts import ModelMode
from recovery_workbench.simulator import driver, scenarios
from recovery_workbench.simulator.world import generate_world, run_id_for

TEMPLATE_MODEL_MODE = ModelMode.RULES  # auto's choice with no local model and no hosted settings; install re-records
DATABASE_SIDECARS = tuple(cli.DATABASE_FILE + suffix for suffix in ("-wal", "-shm", "-journal"))
_COPY_CHUNK_BYTES = 1 << 20
_NO_TEMPLATE_ADVICE = "leave WORKBENCH_PUBLIC_TEMPLATE_DIR empty to seed every run when a viewer asks"


@dataclass(frozen=True)
class TemplatePlan:
    """What ``install`` copies: the template's directories, parents first, and its regular files, relative to it."""

    source: Path
    directories: tuple[PurePosixPath, ...]
    files: tuple[PurePosixPath, ...]
    size_bytes: int


@dataclass(frozen=True)
class DemoRuns:
    """The demo runs a public-mode server copied in at start."""

    source: Path
    copied: tuple[tuple[str, int], ...]  # (scenario, seed) of each run
    size_bytes: int
    model_mode: ModelMode  # the server's, recorded on each run
    seeded_on_request: tuple[tuple[str, int], ...]  # published demo runs the template lacks

    def start_line(self) -> str:
        copied = ", ".join(f"{scenario} {seed}" for scenario, seed in self.copied)
        waiting = ", ".join(f"{scenario} {seed}" for scenario, seed in self.seeded_on_request) or "none"
        return (f"Demo runs: {len(self.copied)} copied from {self.source} ({copied}; {self.size_bytes / 2**20:.1f} MB "
                f"of the data limit), recorded in model mode {self.model_mode.value}; seeded when a viewer asks: "
                f"{waiting}")


# --------------------------------------------------------------------------- at image build


def build(template_dir: Path, seeds: Iterable[int]) -> tuple[driver.SeedSummary, ...]:
    """Seed every demo scenario with each of ``seeds`` into ``template_dir``, a new directory: the image's demo runs.

    Each run is seeded as public mode seeds one at run time, but in TEMPLATE_MODEL_MODE and without an oracle file (the
    module docstring says why). The database is then taken out of WAL mode, which writes every page into the database
    file and deletes the -wal file, and closed; a -wal, -shm or -journal file still beside it fails the build.

    The lowest seed comes first, then the scenarios in the README's flow order, which DEMO_SCENARIOS keeps: Flow A, the
    one that executes. The order no longer decides a receipt: since receipt-2 (Q52) the receipt hash leaves out the
    database's inbox row numbers, so a recovery on a copied run gives the receipt the README publishes (eb9d6f30...)
    wherever its run comes in the template.
    """
    template_dir.mkdir(parents=True)  # never an existing path: one process builds a template whole
    with closing(storage.connect(template_dir / cli.DATABASE_FILE)) as conn:
        summaries = tuple(
            driver.seed_world(conn, generate_world(scenarios.demo_scenario(name), seed), oracle_dir=None,
                              provider_dir=template_dir / cli.PROVIDER_DIRECTORY, model_mode=TEMPLATE_MODEL_MODE)
            for seed in sorted(seeds) for name in scenarios.DEMO_SCENARIOS
        )
        journal_mode = str(conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0]).lower()
    if journal_mode != "delete":  # SQLite answers with the old mode when another connection keeps it from switching
        raise storage.StorageError(
            f"the demo-run template's database stayed in {journal_mode!r} journal mode, so it may not hold every page; "
            f"build the template in a new directory that no other process opens; {template_dir} is not one"
        )
    left = [name for name in DATABASE_SIDECARS if os.path.lexists(template_dir / name)]
    if left:
        raise storage.StorageError(
            f"the demo-run template's database was not closed cleanly: {', '.join(left)} still beside it in "
            f"{template_dir}; build the template again in a new directory"
        )
    return summaries


# --------------------------------------------------------------------------- at start


def plan(template_dir: Path, data_dir: Path) -> TemplatePlan:
    """What ``install`` will copy from ``template_dir``, checked before the data directory is touched; else ValueError.

    The template must be a directory, not a symbolic link, apart from the data directory, which public mode empties.
    It must hold the workbench database, closed cleanly, and nothing but regular files and directories: a symbolic
    link, a pipe, a socket or a device anywhere in it refuses the start, so nothing is followed or copied through one.
    """
    try:
        root = template_dir.lstat()
    except FileNotFoundError as missing:
        raise ValueError(_refusal(template_dir, "does not exist")) from missing
    if stat.S_ISLNK(root.st_mode) or not stat.S_ISDIR(root.st_mode):
        raise ValueError(_refusal(template_dir, f"is {_kind(root.st_mode)}, not a directory"))
    source, target = template_dir.resolve(), data_dir.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError(_refusal(template_dir, f"is not apart from the data directory {data_dir}, which public mode "
                                                "empties at every start"))
    directories: list[PurePosixPath] = []
    files: list[PurePosixPath] = []
    try:
        size_bytes = _walk(template_dir, PurePosixPath(), directories, files)
    except OSError as unreadable:
        raise OSError(f"public mode could not read the demo-run template {template_dir}: {unreadable}. Nothing was "
                      f"deleted; give the server's user read access to it, or {_NO_TEMPLATE_ADVICE}") from unreadable
    top_level = {str(path) for path in files if len(path.parts) == 1}
    if cli.DATABASE_FILE not in top_level:
        raise ValueError(_refusal(template_dir, f"holds no {cli.DATABASE_FILE}"))
    for name in (*DATABASE_SIDECARS, public_mode.DATA_MARKER):
        if name in top_level or PurePosixPath(name) in directories:
            raise ValueError(_refusal(template_dir, f"holds {name}, which no template built cleanly holds"))
    return TemplatePlan(source=template_dir, directories=tuple(directories), files=tuple(files), size_bytes=size_bytes)


def install(planned: TemplatePlan, data_dir: Path, *, seeds: frozenset[int], model_mode: ModelMode) -> DemoRuns:
    """Copy the planned template into ``data_dir``, which ``public_mode.claim_data_dir`` has just emptied, check its
    runs against the published ``seeds``, and record ``model_mode``, the mode this server settled on, as each run's.

    A failed copy raises OSError, and a run no viewer may seed raises ValueError. Either way the server does not start,
    and its next start empties the data directory again.
    """
    _copy(planned, data_dir)
    with closing(storage.connect(data_dir / cli.DATABASE_FILE)) as conn:
        storage.migrate_simulator(conn)  # the server's own migrations, before any run is read or changed
        run_ids = [str(row["run_id"]) for row in conn.execute("SELECT run_id FROM runs ORDER BY run_id")]
        runs = [storage.require_run(conn, run_id) for run_id in run_ids]
        _check_runs(planned.source, runs, seeds)
        with storage.write_transaction(conn):
            for run in runs:
                if run.model_mode is not model_mode:
                    storage.set_run_model_mode(conn, run.run_id, model_mode)
    copied = {(run.scenario, run.seed) for run in runs}
    published = {(name, seed) for seed in seeds for name in scenarios.DEMO_SCENARIOS}
    return DemoRuns(source=planned.source, copied=tuple(sorted(copied)), size_bytes=planned.size_bytes,
                    model_mode=model_mode, seeded_on_request=tuple(sorted(published - copied)))


def _walk(template_dir: Path, relative: PurePosixPath, directories: list[PurePosixPath],
          files: list[PurePosixPath]) -> int:
    """Add what lies under ``relative`` to ``directories`` and ``files``, refusing anything else; its bytes."""
    size_bytes = 0
    with os.scandir(template_dir / relative) as listing:
        entries = sorted(listing, key=lambda entry: entry.name)
    for entry in entries:
        path = relative / entry.name
        found = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(found.st_mode):
            directories.append(path)
            size_bytes += _walk(template_dir, path, directories, files)
        elif stat.S_ISREG(found.st_mode):
            files.append(path)
            size_bytes += found.st_size
        else:
            raise ValueError(_refusal(template_dir, f"holds {path}, which is {_kind(found.st_mode)}; a template holds "
                                                    "only regular files and directories"))
    return size_bytes


def _copy(planned: TemplatePlan, data_dir: Path) -> None:
    try:
        for relative in planned.directories:
            (data_dir / relative).mkdir()
        for relative in planned.files:
            _copy_file(planned.source / relative, data_dir / relative)
    except OSError as failed:
        raise OSError(
            f"public mode could not copy the demo-run template {planned.source} into {data_dir}: {failed}. The server "
            f"did not start; check the data directory's free space and permissions, or {_NO_TEMPLATE_ADVICE}"
        ) from failed


def _copy_file(source: Path, target: Path) -> None:
    """Copy one regular file. The template's mode bits stay behind: the copy is the server's to write, as a seeded file
    is. O_NOFOLLOW refuses a link put in place since the plan, and O_NONBLOCK opens a pipe at once, to be refused."""
    with open(os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            raise OSError(f"{source} is no longer a regular file")
        with open(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666), "wb") as writer:
            shutil.copyfileobj(reader, writer, _COPY_CHUNK_BYTES)


def _check_runs(source: Path, runs: list[storage.RunRecord], seeds: frozenset[int]) -> None:
    """Refuse a template run no viewer of this server could have seeded."""
    rebuild = f"rebuild the image, whose build seeds the template, or {_NO_TEMPLATE_ADVICE}"
    if not runs:
        raise ValueError(f"the demo-run template {source} holds no runs; {rebuild}")
    for run in runs:
        spec = scenarios.DEMO_SCENARIOS.get(run.scenario)
        if spec is None or run.run_id != run_id_for(spec, run.seed):
            raise ValueError(
                f"the demo-run template {source} holds run {run.run_id} (scenario {run.scenario!r}, seed {run.seed}), "
                f"which is not the demo run this server seeds for that scenario and seed; {rebuild}"
            )
    unpublished = sorted({run.seed for run in runs} - seeds)
    if unpublished:
        raise ValueError(
            f"the demo-run template {source} holds runs for seeds {unpublished}, which WORKBENCH_PUBLIC_SEEDS "
            f"({sorted(seeds)}) does not publish, so viewers would see runs they cannot ask for; publish those seeds, "
            "build the image with --build-arg WORKBENCH_PUBLIC_SEEDS=<the seeds you publish>, or "
            f"{_NO_TEMPLATE_ADVICE}"
        )


def _refusal(template_dir: Path, problem: str) -> str:
    return (f"WORKBENCH_PUBLIC_TEMPLATE_DIR names {template_dir}, which {problem}; point it at the demo-run template "
            f"the image was built with, or {_NO_TEMPLATE_ADVICE}. Nothing was deleted")


def _kind(mode: int) -> str:
    if stat.S_ISLNK(mode):
        return "a symbolic link"
    if stat.S_ISFIFO(mode):
        return "a named pipe"
    if stat.S_ISSOCK(mode):
        return "a socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "a device"
    return "a regular file" if stat.S_ISREG(mode) else "a special file"


def main(argv: list[str] | None = None) -> int:
    """The Dockerfile's step: ``python -m recovery_workbench.public_template <new directory>``."""
    parser = argparse.ArgumentParser(
        description="Seed public mode's demo runs into a new template directory (Q48): every demo scenario with each "
                    "seed in WORKBENCH_PUBLIC_SEEDS (default 101)."
    )
    parser.add_argument("template_dir", type=Path, help="a directory that does not exist yet")
    args = parser.parse_args(argv)
    for summary in build(args.template_dir, settings.published_seeds()):
        print(f"Demo run {summary.run_id}: {summary.scenario} with seed {summary.seed}, "
              f"replay clock {summary.cutoff_ms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
