#!/usr/bin/env python3
"""Bounded local broad-validation sampler for an explicit active bead.

This command MEASURES serial/-n2/-n4 behavior under one broad-seat lease.  It
does not edit any normal validation command and never chooses ``-n auto``.
Every run records exact collection and outcome identities, pytest's per-test
cost report, wall/CPU/load observations, and timeout/source-integrity state.
The ladder advances only when the prior width is complete, identity-equivalent,
materially faster, and free of a material tail regression.

The follow-up ``--overlap-shell`` mode first records one same-object ``-n2``
Python reference, then repeats the candidate topology: fixed ``-n2`` Python
and the complete SERIAL shell ALLOW lane at the same time.  Both process groups
are owned, both verdicts and artifact sets are required, and the mode is GO
only when repeated exact outcomes remain below the declared broad-wall target.
It is still a sampler; production adoption is a separate change after GO.

Artifacts live below git-common-dir so they are shared by linked worktrees but
can never become a changed source path.  pytest-xdist is deliberately a local
experiment dependency; this module imports none of it.  A parallel width is
refused by the pytest command itself when xdist is unavailable.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import re
import resource
import shutil
import signal
import statistics
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterator


WIDTHS = ("serial", "2", "4")
DEFAULT_TIMEOUT_SECONDS = 1800.0
DEFAULT_MIN_IMPROVEMENT = 0.10
DEFAULT_TAIL_RATIO = 1.50
DEFAULT_TAIL_ABSOLUTE_SECONDS = 5.0
DEFAULT_BROAD_TARGET_SECONDS = 600.0
BEAD_ID_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"
)


class BenchmarkError(RuntimeError):
    """A loud precondition/artifact error, never a test verdict."""


def parse_bead_identity(value: str) -> str:
    """Require one explicit tracker identity; ambient task state is unsafe."""
    if not BEAD_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "must be a bead id such as SABLE-y4nom.7.7"
        )
    return value


@functools.lru_cache(maxsize=1)
def bash_identity() -> dict[str, str]:
    """Resolve and content-address the Bash executable this sampler runs."""
    discovered = shutil.which("bash")
    if not discovered:
        raise BenchmarkError("bash is absent from PATH")
    executable = Path(discovered).resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise BenchmarkError(f"resolved bash is not executable: {executable}")
    try:
        sha256 = hashlib.sha256(executable.read_bytes()).hexdigest()
    except OSError as exc:
        raise BenchmarkError(f"cannot hash resolved bash {executable}: {exc}") from exc
    cp = subprocess.run(
        [str(executable), "--version"], text=True, capture_output=True, check=False,
    )
    version = (cp.stdout or cp.stderr).splitlines()
    if cp.returncode != 0 or not version:
        raise BenchmarkError(
            f"cannot identify resolved bash {executable}: rc={cp.returncode}"
        )
    return {
        "executable": str(executable),
        "version": version[0],
        "sha256": sha256,
    }


def _run(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cp = subprocess.run(
        list(args), cwd=repo, text=True, capture_output=True, check=False,
    )
    if check and cp.returncode != 0:
        raise BenchmarkError(
            f"{' '.join(args)} failed ({cp.returncode}): "
            f"{(cp.stdout + cp.stderr).strip()}"
        )
    return cp


def git_common_dir(repo: Path) -> Path:
    raw = _run(repo, "git", "rev-parse", "--git-common-dir").stdout.strip()
    if not raw:
        raise BenchmarkError("git rev-parse returned an empty common dir")
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (repo / path).resolve()


def git_head(repo: Path) -> str:
    return _run(repo, "git", "rev-parse", "HEAD").stdout.strip()


def git_porcelain(repo: Path) -> str:
    return _run(
        repo, "git", "status", "--porcelain", "--untracked-files=all"
    ).stdout


def _require_clean(repo: Path) -> str:
    dirty = git_porcelain(repo)
    if dirty:
        raise BenchmarkError(
            "benchmark source must be clean; commit the runner/audit first:\n" + dirty
        )
    return git_head(repo)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def artifact_root(repo: Path, requested: str | None, head: str) -> Path:
    common = git_common_dir(repo)
    if requested:
        root = Path(requested).resolve()
        if not _inside(root, common):
            raise BenchmarkError(
                f"artifact dir must live under git-common-dir {common}, got {root}"
            )
    else:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        root = common / "sable" / "xdist-benchmark" / f"{head[:12]}-{stamp}"
    root.mkdir(parents=True, exist_ok=False)
    return root


@contextlib.contextmanager
def broad_seat(repo: Path) -> Iterator[Path]:
    """One nonblocking host/repo-wide lease for the whole ladder."""
    # Reuse VE.2's broad serial-publisher lock.  A second publisher already
    # refuses this lock rather than queues, so sharing it is what makes the
    # benchmark and canonical-profile refresh mutually exclusive as well as
    # serializing two benchmark commands.  A benchmark-only lock would leave
    # the more expensive cross-kind collision completely open.
    lock_path = git_common_dir(repo) / "sable" / "test-cost" / "publish.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.seek(0)
            holder = lock.read().strip() or "unknown holder"
            raise BenchmarkError(
                f"broad benchmark seat is busy ({lock_path}; {holder})"
            ) from exc
        lock.seek(0)
        lock.truncate()
        lock.write(f"pid={os.getpid()} head={git_head(repo)}\n")
        lock.flush()
        os.fsync(lock.fileno())
        try:
            yield lock_path
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def width_args(width: str) -> list[str]:
    if width == "serial":
        return []
    if width not in {"2", "4"}:
        raise BenchmarkError(f"unsupported width {width!r}; expected serial, 2, or 4")
    return ["-n", width, "--dist=loadscope"]


def pytest_command(run_dir: Path, width: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "bin/",
        "-q",
        "-rs",
        "-p",
        "no:cacheprovider",
        "--sable-report-skip-set",
        f"--sable-test-cost-report={run_dir / 'cost.json'}",
        f"--junitxml={run_dir / 'junit.xml'}",
        *width_args(width),
    ]


def collection_command(width: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "bin/",
        "--collect-only",
        "-q",
        "-p",
        "no:cacheprovider",
        *width_args(width),
    ]


def overlap_commands(run_dir: Path) -> tuple[list[str], list[str]]:
    """The candidate topology is authorized only for the explicit active bead.

    Python is the fixed width VE.4 accepted.  Shell remains the existing
    complete serial runner: this experiment overlaps coarse lanes and never
    introduces within-shell concurrency.
    """
    return (
        pytest_command(run_dir, "2"),
        [
            bash_identity()["executable"],
            ".github/ci/shell-run-set.sh", "--profile",
            str(run_dir / "shell.tsv"),
        ],
    )


def shell_catalog(repo: Path) -> tuple[str, ...]:
    """Read the authoritative ALLOW array by sourcing its owning script."""
    script = repo / ".github" / "ci" / "shell-run-set.sh"
    if not script.is_file():
        raise BenchmarkError(f"shell run-set is absent: {script}")
    cp = _run(
        repo,
        bash_identity()["executable"], "-c",
        'source "$1"; printf "%s\\n" "${ALLOW[@]}"',
        "sable-xdist-benchmark", str(script),
    )
    suites = tuple(line.strip() for line in cp.stdout.splitlines() if line.strip())
    if not suites or len(suites) != len(set(suites)):
        raise BenchmarkError(
            "shell ALLOW discovery was empty or contained duplicate identities"
        )
    return tuple(sorted(suites))


def parse_shell_profile(path: Path, expected: tuple[str, ...]) -> dict[str, dict]:
    """Parse one complete shell profile without laundering failed statuses."""
    try:
        lines = path.read_text().splitlines()
    except OSError as exc:
        raise BenchmarkError(f"cannot read shell profile {path}: {exc}") from exc
    if not lines or lines[0] != "suite\tstatus\tseconds":
        raise BenchmarkError(f"shell profile has an invalid header: {path}")
    rows: dict[str, dict] = {}
    for line in lines[1:]:
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 3 or not fields[0] or not fields[1]:
            raise BenchmarkError(f"malformed shell profile row: {line!r}")
        suite, status, raw_seconds = fields
        if suite in rows:
            raise BenchmarkError(f"duplicate shell profile identity: {suite}")
        try:
            seconds = float(raw_seconds)
        except ValueError as exc:
            raise BenchmarkError(
                f"non-numeric shell duration for {suite}: {raw_seconds!r}"
            ) from exc
        if not math.isfinite(seconds) or seconds < 0:
            raise BenchmarkError(
                f"invalid shell duration for {suite}: {raw_seconds!r}"
            )
        rows[suite] = {"status": status, "seconds": seconds}
    actual = set(rows)
    wanted = set(expected)
    if actual != wanted:
        missing = sorted(wanted - actual)
        extra = sorted(actual - wanted)
        detail = []
        if missing:
            detail.append("missing " + " ".join(missing))
        if extra:
            detail.append("unexpected " + " ".join(extra))
        raise BenchmarkError("shell profile identity mismatch: " + "; ".join(detail))
    return dict(sorted(rows.items()))


def collection_identities(output: str) -> list[str]:
    """Exact node lines from pytest -q --collect-only, sorted for hashing."""
    return sorted({line.strip() for line in output.splitlines()
                   if line.strip().startswith("bin/")})


def digest_json(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def parse_junit(path: Path) -> dict[str, str]:
    """Return stable testcase identity -> pass/fail/error/skip/xfail."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise BenchmarkError(f"cannot read JUnit artifact {path}: {exc}") from exc
    outcomes: dict[str, str] = {}
    for case in root.iter("testcase"):
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "")
        identity = f"{classname}::{name}"
        if not classname or not name:
            raise BenchmarkError(f"JUnit testcase lacks identity: {case.attrib}")
        if identity in outcomes:
            raise BenchmarkError(f"duplicate JUnit identity: {identity}")
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "error"
        elif (skipped := case.find("skipped")) is not None:
            marker = " ".join((
                skipped.attrib.get("type", ""),
                skipped.attrib.get("message", ""),
            )).lower()
            outcome = "xfail" if "xfail" in marker else "skipped"
        else:
            outcome = "passed"
        outcomes[identity] = outcome
    if not outcomes:
        raise BenchmarkError(f"JUnit artifact contains no testcase rows: {path}")
    return dict(sorted(outcomes.items()))


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v) for v in values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def cost_tails(path: Path) -> dict[str, float | int]:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read cost artifact {path}: {exc}") from exc
    tests = payload.get("tests")
    modules = payload.get("modules")
    violations = payload.get("violations")
    if not isinstance(tests, list) or not isinstance(modules, list) or not isinstance(violations, list):
        raise BenchmarkError("cost artifact has the wrong top-level shape")
    test_seconds = [row.get("seconds") for row in tests if isinstance(row, dict)]
    module_seconds = [row.get("seconds") for row in modules if isinstance(row, dict)]
    if (len(test_seconds) != len(tests) or len(module_seconds) != len(modules)
            or any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in test_seconds + module_seconds)):
        raise BenchmarkError("cost artifact has a malformed duration row")
    return {
        "test_rows": len(tests),
        "module_rows": len(modules),
        "violation_count": len(violations),
        "test_p95_seconds": percentile(test_seconds, 0.95),
        "max_test_seconds": max(test_seconds, default=0.0),
        "module_p95_seconds": percentile(module_seconds, 0.95),
        "max_module_seconds": max(module_seconds, default=0.0),
    }


def _load_sampler(stop: threading.Event, samples: list[tuple[float, float, float]]) -> None:
    while not stop.is_set():
        samples.append(tuple(float(v) for v in os.getloadavg()))
        stop.wait(1.0)


def _terminate_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _outcome_counts(outcomes: dict[str, str]) -> dict[str, int]:
    keys = ("passed", "failed", "error", "skipped", "xfail")
    return {key: sum(value == key for value in outcomes.values()) for key in keys}


def run_once(repo: Path, root: Path, width: str, repetition: int,
             timeout_seconds: float, expected_head: str) -> dict:
    run_dir = root / f"{width}-run-{repetition}"
    run_dir.mkdir()
    collect = _run(repo, *collection_command(width), check=False)
    (run_dir / "collection.log").write_text(collect.stdout + collect.stderr)
    collected = collection_identities(collect.stdout)

    command = pytest_command(run_dir, width)
    env = dict(os.environ)
    env["SABLE_PYTEST_SKIP_SET_STATE"] = str(run_dir / "skip-set.json")
    samples: list[tuple[float, float, float]] = []
    stop = threading.Event()
    sampler = threading.Thread(target=_load_sampler, args=(stop, samples), daemon=True)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    timed_out = False
    with (run_dir / "pytest.log").open("w") as log:
        proc = subprocess.Popen(
            command, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        sampler.start()
        try:
            returncode = proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_group(proc)
            returncode = 124
        finally:
            stop.set()
            sampler.join(timeout=2)
    wall = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)

    artifact_error = ""
    outcomes: dict[str, str] = {}
    tails: dict[str, float | int] = {}
    try:
        outcomes = parse_junit(run_dir / "junit.xml")
        tails = cost_tails(run_dir / "cost.json")
    except BenchmarkError as exc:
        artifact_error = str(exc)

    head_after = git_head(repo)
    dirty_after = git_porcelain(repo)
    record = {
        "schema": 1,
        "width": width,
        "repetition": repetition,
        "command": command,
        "collection_command": collection_command(width),
        "collection_returncode": collect.returncode,
        "collection_identities": collected,
        "collection_digest": digest_json(collected),
        "returncode": returncode,
        "timed_out": timed_out,
        "wall_seconds": wall,
        "child_user_seconds": after.ru_utime - before.ru_utime,
        "child_system_seconds": after.ru_stime - before.ru_stime,
        "average_cpu_cores": (
            (after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime) / wall
            if wall > 0 else 0.0
        ),
        "load_1m_samples": [sample[0] for sample in samples],
        "load_1m_median": statistics.median([sample[0] for sample in samples]) if samples else 0.0,
        "load_1m_p95": percentile([sample[0] for sample in samples], 0.95),
        "outcomes": outcomes,
        "outcome_digest": digest_json(outcomes),
        "outcome_counts": _outcome_counts(outcomes),
        "artifact_error": artifact_error,
        "head_before": expected_head,
        "head_after": head_after,
        "source_moved": head_after != expected_head,
        "dirty_after": dirty_after,
        **tails,
    }
    (run_dir / "record.json").write_text(
        json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    return record


def run_overlap_once(repo: Path, root: Path, repetition: int,
                     timeout_seconds: float, expected_head: str) -> dict:
    """Run fixed-n2 Python and the complete serial shell lane concurrently.

    The two children are separate process-group leaders.  A shared absolute
    deadline terminates and reaps every still-live group; an ordinary red lane
    is allowed to finish alongside its sibling so the rejected sample still
    carries complete attribution rather than a misleading partial artifact.
    """
    run_dir = root / f"overlap-run-{repetition}"
    run_dir.mkdir()
    collect = _run(repo, *collection_command("2"), check=False)
    (run_dir / "collection.log").write_text(collect.stdout + collect.stderr)
    collected = collection_identities(collect.stdout)
    expected_shell = shell_catalog(repo)
    python_command, shell_command = overlap_commands(run_dir)
    env = dict(os.environ)
    env["SABLE_PYTEST_SKIP_SET_STATE"] = str(run_dir / "skip-set.json")

    samples: list[tuple[float, float, float]] = []
    stop = threading.Event()
    sampler = threading.Thread(target=_load_sampler, args=(stop, samples), daemon=True)
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    deadline = started + timeout_seconds
    timed_out = False
    processes: dict[str, subprocess.Popen] = {}
    returncodes: dict[str, int] = {}
    lane_walls: dict[str, float] = {}

    with ((run_dir / "python.log").open("w") as python_log,
          (run_dir / "shell.log").open("w") as shell_log):
        try:
            processes["python"] = subprocess.Popen(
                python_command, cwd=repo, env=env,
                stdout=python_log, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )
            processes["shell"] = subprocess.Popen(
                shell_command, cwd=repo, env=env,
                stdout=shell_log, stderr=subprocess.STDOUT,
                text=True, start_new_session=True,
            )
        except OSError as exc:
            for proc in processes.values():
                _terminate_group(proc)
            raise BenchmarkError(f"cannot start overlap lane: {exc}") from exc

        sampler.start()
        live = dict(processes)
        try:
            while live:
                now = time.monotonic()
                for lane, proc in tuple(live.items()):
                    rc = proc.poll()
                    if rc is not None:
                        returncodes[lane] = rc
                        lane_walls[lane] = now - started
                        del live[lane]
                if not live:
                    break
                if now >= deadline:
                    timed_out = True
                    for lane, proc in tuple(live.items()):
                        _terminate_group(proc)
                        returncodes[lane] = 124
                        lane_walls[lane] = time.monotonic() - started
                        del live[lane]
                    break
                time.sleep(min(0.05, max(0.0, deadline - now)))
        finally:
            stop.set()
            sampler.join(timeout=2)

    wall = time.monotonic() - started
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    artifact_errors = []
    outcomes: dict[str, str] = {}
    tails: dict[str, float | int] = {}
    shell_rows: dict[str, dict] = {}
    try:
        outcomes = parse_junit(run_dir / "junit.xml")
        tails = cost_tails(run_dir / "cost.json")
    except BenchmarkError as exc:
        artifact_errors.append(str(exc))
    try:
        shell_rows = parse_shell_profile(run_dir / "shell.tsv", expected_shell)
    except BenchmarkError as exc:
        artifact_errors.append(str(exc))

    shell_statuses = {
        suite: row["status"] for suite, row in shell_rows.items()
    }
    head_after = git_head(repo)
    dirty_after = git_porcelain(repo)
    python_rc = returncodes.get("python", 124 if timed_out else 2)
    shell_rc = returncodes.get("shell", 124 if timed_out else 2)
    aggregate_rc = 124 if timed_out else (0 if python_rc == shell_rc == 0 else 1)
    record = {
        "schema": 1,
        "width": "overlap-n2+serial-shell",
        "repetition": repetition,
        "command": python_command,
        "python_command": python_command,
        "shell_command": shell_command,
        "collection_command": collection_command("2"),
        "collection_returncode": collect.returncode,
        "collection_identities": collected,
        "collection_digest": digest_json(collected),
        "returncode": aggregate_rc,
        "python_returncode": python_rc,
        "shell_returncode": shell_rc,
        "timed_out": timed_out,
        "wall_seconds": wall,
        "python_wall_seconds": lane_walls.get("python", wall),
        "shell_wall_seconds": lane_walls.get("shell", wall),
        "child_user_seconds": after.ru_utime - before.ru_utime,
        "child_system_seconds": after.ru_stime - before.ru_stime,
        "average_cpu_cores": (
            (after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime) / wall
            if wall > 0 else 0.0
        ),
        "load_1m_samples": [sample[0] for sample in samples],
        "load_1m_median": (
            statistics.median([sample[0] for sample in samples]) if samples else 0.0
        ),
        "load_1m_p95": percentile([sample[0] for sample in samples], 0.95),
        "outcomes": outcomes,
        "outcome_digest": digest_json(outcomes),
        "outcome_counts": _outcome_counts(outcomes),
        "shell": shell_rows,
        "shell_digest": digest_json(shell_statuses),
        "shell_all_passed": (
            len(shell_rows) == len(expected_shell)
            and all(status == "pass" for status in shell_statuses.values())
        ),
        "shell_seconds_total": sum(
            float(row["seconds"]) for row in shell_rows.values()
        ),
        "artifact_error": "; ".join(artifact_errors),
        "head_before": expected_head,
        "head_after": head_after,
        "source_moved": head_after != expected_head,
        "dirty_after": dirty_after,
        **tails,
    }
    (run_dir / "record.json").write_text(
        json.dumps(record, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    return record


def width_summary(width: str, records: list[dict]) -> dict:
    def med(key: str) -> float:
        return statistics.median(float(record.get(key, 0.0)) for record in records)

    return {
        "width": width,
        "repetitions": len(records),
        "wall_median_seconds": med("wall_seconds"),
        "wall_p95_seconds": percentile([r["wall_seconds"] for r in records], 0.95),
        "average_cpu_cores_median": med("average_cpu_cores"),
        "load_1m_p95_median": med("load_1m_p95"),
        "test_p95_seconds": med("test_p95_seconds"),
        "max_test_seconds": med("max_test_seconds"),
        "module_p95_seconds": med("module_p95_seconds"),
        "max_module_seconds": med("max_module_seconds"),
        "timeout_count": sum(bool(r.get("timed_out")) for r in records),
        "returncodes": [r.get("returncode") for r in records],
        "outcome_counts": [r.get("outcome_counts") for r in records],
    }


def material_tail_regressions(previous: dict, current: dict, *,
                              ratio: float, absolute_seconds: float) -> list[str]:
    regressions = []
    for key in ("test_p95_seconds", "max_test_seconds",
                "module_p95_seconds", "max_module_seconds"):
        before = float(previous[key])
        after = float(current[key])
        if after > before * ratio and after - before > absolute_seconds:
            regressions.append(f"{key} {before:.3f}s -> {after:.3f}s")
    return regressions


def record_integrity_reasons(record: dict, anchor: dict | None) -> list[str]:
    """Hard stop reasons knowable from one run; no distribution inferred."""
    reasons = []
    rep = record["repetition"]
    if record.get("collection_returncode") != 0:
        reasons.append(f"run {rep} collection rc={record.get('collection_returncode')}")
    if record.get("returncode") != 0:
        reasons.append(f"run {rep} test command rc={record.get('returncode')}")
    if record.get("timed_out"):
        reasons.append(f"run {rep} timed out")
    if record.get("artifact_error"):
        reasons.append(f"run {rep} artifact error: {record['artifact_error']}")
    if record.get("violation_count"):
        reasons.append(f"run {rep} load-budget violations={record['violation_count']}")
    if record.get("source_moved") or record.get("dirty_after"):
        reasons.append(f"run {rep} source tree moved or became dirty")
    if anchor is not None:
        if record.get("collection_digest") != anchor.get("collection_digest"):
            reasons.append(f"run {rep} collection identity diverged")
        if record.get("outcome_digest") != anchor.get("outcome_digest"):
            reasons.append(f"run {rep} outcome identity diverged")
    return reasons


def evaluate_stage(width: str, records: list[dict], baseline: dict | None,
                   previous: dict | None, *, min_improvement: float,
                   tail_ratio: float, tail_absolute_seconds: float) -> tuple[bool, list[str], dict]:
    summary = width_summary(width, records)
    reasons = []
    anchor = baseline or records[0]
    for record in records:
        reasons.extend(record_integrity_reasons(record, anchor))

    if previous is not None:
        required = float(previous["wall_median_seconds"]) * (1.0 - min_improvement)
        if float(summary["wall_median_seconds"]) > required:
            reasons.append(
                f"median wall {summary['wall_median_seconds']:.3f}s did not improve at least "
                f"{min_improvement:.0%} from {previous['wall_median_seconds']:.3f}s"
            )
        reasons.extend(
            "material tail regression: " + item
            for item in material_tail_regressions(
                previous, summary, ratio=tail_ratio,
                absolute_seconds=tail_absolute_seconds,
            )
        )
    return not reasons, reasons, summary


def evaluate_overlap(records: list[dict], python_reference: dict, *,
                     broad_target_seconds: float, tail_ratio: float,
                     tail_absolute_seconds: float) -> tuple[bool, list[str], dict]:
    """Accept only repeated, exact, sub-target combined-lane evidence."""
    summary = width_summary("overlap-n2+serial-shell", records)
    summary.update({
        "python_wall_median_seconds": statistics.median(
            float(record.get("python_wall_seconds", record["wall_seconds"]))
            for record in records
        ),
        "shell_wall_median_seconds": statistics.median(
            float(record.get("shell_wall_seconds", record["wall_seconds"]))
            for record in records
        ),
        "shell_seconds_median": statistics.median(
            float(record.get("shell_seconds_total", 0.0)) for record in records
        ),
    })
    reasons = []
    if len(records) < 2:
        reasons.append("overlap needs at least two repetitions")
    shell_anchor = records[0].get("shell_digest") if records else None
    for record in records:
        reasons.extend(record_integrity_reasons(record, python_reference))
        rep = record.get("repetition")
        if record.get("python_returncode") != 0:
            reasons.append(
                f"run {rep} Python lane rc={record.get('python_returncode')}"
            )
        if record.get("shell_returncode") != 0:
            reasons.append(
                f"run {rep} shell lane rc={record.get('shell_returncode')}"
            )
        if not record.get("shell_all_passed"):
            reasons.append(f"run {rep} shell lane was not complete and all-pass")
        if record.get("shell_digest") != shell_anchor:
            reasons.append(f"run {rep} shell identity/status diverged")

    if float(summary["wall_p95_seconds"]) >= broad_target_seconds:
        reasons.append(
            f"combined wall p95 {summary['wall_p95_seconds']:.3f}s is not "
            f"under the {broad_target_seconds:g}s broad target"
        )
    reference_summary = width_summary("2-reference", [python_reference])
    reasons.extend(
        "material tail regression: " + item
        for item in material_tail_regressions(
            reference_summary, summary,
            ratio=tail_ratio,
            absolute_seconds=tail_absolute_seconds,
        )
    )
    return not reasons, reasons, summary


def environment_record() -> dict:
    xdist_version = None
    if importlib.util.find_spec("xdist") is not None:
        xdist_version = importlib.metadata.version("pytest-xdist")
    shell = bash_identity()
    return {
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": platform.python_version(),
        "bash_executable": shell["executable"],
        "bash_version": shell["version"],
        "bash_sha256": shell["sha256"],
        "pytest_version": importlib.metadata.version("pytest"),
        "xdist_version": xdist_version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "processor": platform.processor(),
    }


def run_ladder(repo: Path, root: Path, *, bead: str, repetitions: int,
               timeout_seconds: float,
               min_improvement: float, tail_ratio: float,
               tail_absolute_seconds: float, shell_baseline_seconds: float) -> tuple[int, dict]:
    expected_head = _require_clean(repo)
    if importlib.util.find_spec("xdist") is None:
        raise BenchmarkError(
            "pytest-xdist is absent; install it for this local experiment only before running the ladder"
        )

    stages = []
    baseline_record = None
    previous_summary = None
    recommended = None
    stop_reason = ""
    for width in WIDTHS:
        records = []
        hard_reasons = []
        for repetition in range(1, repetitions + 1):
            record = run_once(
                repo, root, width, repetition, timeout_seconds, expected_head
            )
            records.append(record)
            anchor = baseline_record or (records[0] if len(records) > 1 else None)
            hard_reasons.extend(record_integrity_reasons(record, anchor))
            if hard_reasons:
                break
        if hard_reasons:
            eligible, reasons, summary = False, hard_reasons, width_summary(width, records)
        else:
            eligible, reasons, summary = evaluate_stage(
                width, records, baseline_record, previous_summary,
                min_improvement=min_improvement,
                tail_ratio=tail_ratio,
                tail_absolute_seconds=tail_absolute_seconds,
            )
        stages.append({"width": width, "eligible": eligible,
                       "reasons": reasons, "summary": summary})
        if baseline_record is None:
            baseline_record = records[0]
        if not eligible:
            stop_reason = f"stopped at {width}: " + "; ".join(reasons)
            break
        previous_summary = summary
        if width != "serial":
            recommended = width

    verdict = "GO" if recommended is not None else "NO-GO"
    chosen = next((s["summary"] for s in stages if s["width"] == recommended), None)
    combined = (
        float(chosen["wall_median_seconds"]) + shell_baseline_seconds
        if chosen is not None else None
    )
    result = {
        "schema": 1,
        "bead": bead,
        "verdict": verdict,
        "recommended_width": recommended,
        "stop_reason": stop_reason,
        "source_head": expected_head,
        "environment": environment_record(),
        "repetitions": repetitions,
        "timeout_seconds": timeout_seconds,
        "thresholds": {
            "min_improvement": min_improvement,
            "tail_ratio": tail_ratio,
            "tail_absolute_seconds": tail_absolute_seconds,
        },
        "serial_shell_baseline_seconds": shell_baseline_seconds,
        "estimated_combined_broad_median_seconds": combined,
        "broad_target_seconds": 600.0,
        "estimated_combined_meets_target": combined is not None and combined < 600.0,
        "stages": stages,
    }
    (root / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    return (0 if verdict == "GO" else 3), result


def run_overlap_experiment(
        repo: Path, root: Path, *, bead: str, repetitions: int,
        timeout_seconds: float,
        tail_ratio: float, tail_absolute_seconds: float,
        broad_target_seconds: float) -> tuple[int, dict]:
    """Same-object n2 reference followed by repeated coarse-lane overlap."""
    expected_head = _require_clean(repo)
    if importlib.util.find_spec("xdist") is None:
        raise BenchmarkError(
            "pytest-xdist is absent; install the pinned local dependency before "
            "running the overlap experiment"
        )

    reference = run_once(
        repo, root, "2", 1, timeout_seconds, expected_head,
    )
    reference_reasons = record_integrity_reasons(reference, None)
    records = []
    hard_reasons = list(reference_reasons)
    if not hard_reasons:
        for repetition in range(1, repetitions + 1):
            record = run_overlap_once(
                repo, root, repetition, timeout_seconds, expected_head,
            )
            records.append(record)
            hard_reasons.extend(record_integrity_reasons(record, reference))
            if hard_reasons:
                break

    if records:
        eligible, reasons, summary = evaluate_overlap(
            records,
            reference,
            broad_target_seconds=broad_target_seconds,
            tail_ratio=tail_ratio,
            tail_absolute_seconds=tail_absolute_seconds,
        )
        if hard_reasons:
            eligible = False
            reasons = list(dict.fromkeys([*hard_reasons, *reasons]))
    else:
        eligible = False
        reasons = hard_reasons or ["no overlap sample ran"]
        summary = {}

    verdict = "GO" if eligible else "NO-GO"
    result = {
        "schema": 1,
        "bead": bead,
        "mode": "fixed-n2+serial-shell-overlap",
        "verdict": verdict,
        "source_head": expected_head,
        "environment": environment_record(),
        "repetitions_requested": repetitions,
        "timeout_seconds": timeout_seconds,
        "broad_target_seconds": broad_target_seconds,
        "thresholds": {
            "tail_ratio": tail_ratio,
            "tail_absolute_seconds": tail_absolute_seconds,
        },
        "python_reference": width_summary("2-reference", [reference]),
        "overlap": summary,
        "reasons": reasons,
    }
    (root / "summary.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n"
    )
    return (0 if eligible else 3), result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bead",
        required=True,
        type=parse_bead_identity,
        help="active measurement bead id recorded in summary.json",
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--output-dir")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--min-improvement", type=float, default=DEFAULT_MIN_IMPROVEMENT)
    parser.add_argument("--tail-ratio", type=float, default=DEFAULT_TAIL_RATIO)
    parser.add_argument("--tail-absolute-seconds", type=float,
                        default=DEFAULT_TAIL_ABSOLUTE_SECONDS)
    parser.add_argument("--shell-baseline-seconds", type=float, default=505.157)
    parser.add_argument(
        "--overlap-shell", action="store_true",
        help=(
            "run one same-object fixed-n2 reference, then repeat fixed-n2 "
            "Python concurrently with the complete serial shell lane"
        ),
    )
    parser.add_argument(
        "--broad-target-seconds", type=float,
        default=DEFAULT_BROAD_TARGET_SECONDS,
    )
    args = parser.parse_args(argv)
    if args.repetitions < 2:
        parser.error("--repetitions must be at least 2; a sample is not a distribution")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if not 0 < args.min_improvement < 1:
        parser.error("--min-improvement must be between 0 and 1")
    if args.tail_ratio <= 1 or args.tail_absolute_seconds < 0:
        parser.error("tail thresholds must be ratio > 1 and absolute >= 0")
    if args.broad_target_seconds <= 0:
        parser.error("--broad-target-seconds must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo = Path(args.repo).resolve()
    try:
        head = _require_clean(repo)
        with broad_seat(repo):
            root = artifact_root(repo, args.output_dir, head)
            if args.overlap_shell:
                rc, result = run_overlap_experiment(
                    repo,
                    root,
                    bead=args.bead,
                    repetitions=args.repetitions,
                    timeout_seconds=args.timeout_seconds,
                    tail_ratio=args.tail_ratio,
                    tail_absolute_seconds=args.tail_absolute_seconds,
                    broad_target_seconds=args.broad_target_seconds,
                )
            else:
                rc, result = run_ladder(
                    repo,
                    root,
                    bead=args.bead,
                    repetitions=args.repetitions,
                    timeout_seconds=args.timeout_seconds,
                    min_improvement=args.min_improvement,
                    tail_ratio=args.tail_ratio,
                    tail_absolute_seconds=args.tail_absolute_seconds,
                    shell_baseline_seconds=args.shell_baseline_seconds,
                )
    except BenchmarkError as exc:
        print(f"sable-xdist-benchmark: REFUSED — {exc}", file=sys.stderr)
        return 2
    if args.overlap_shell:
        print(
            f"sable-xdist-benchmark: overlap {result['verdict']} artifacts={root}"
        )
        if result["overlap"]:
            print(
                "sable-xdist-benchmark: combined wall p95 "
                f"{result['overlap']['wall_p95_seconds']:.3f}s "
                f"(target < {result['broad_target_seconds']:.0f}s)"
            )
        if result["reasons"]:
            print("sable-xdist-benchmark: " + "; ".join(result["reasons"]))
    else:
        print(
            f"sable-xdist-benchmark: {result['verdict']} "
            f"recommended_width={result['recommended_width']} artifacts={root}"
        )
        if result["estimated_combined_broad_median_seconds"] is not None:
            print(
                "sable-xdist-benchmark: estimated Python+serial-shell broad median "
                f"{result['estimated_combined_broad_median_seconds']:.3f}s "
                f"(target < {result['broad_target_seconds']:.0f}s)"
            )
        if result["stop_reason"]:
            print(f"sable-xdist-benchmark: {result['stop_reason']}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
