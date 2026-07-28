#!/usr/bin/env python3
"""Attributable same-host plants for scoped SABLE test workloads.

The plant is deliberately not a general command runner.  Its built-in catalog
contains one scoped pytest module for each workload class named by
SABLE-x2r7g, and every successful worker must emit the catalog's exact pytest
membership summary.  This keeps the benchmark from becoming a side door to
the sealed full-suite verdict.

Each worker owns a stable log and a GNU-time record.  The parent owns every
PID, timeout, exit status, and result parse; a missing or malformed record is
red.  Host samples come from procfs in the same pass so profiling never
launches a second test execution.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import pwd
import re
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence


SCHEMA_VERSION = 2
TIME_RECORD_PREFIX = "sable-time-v1"
TIME_FORMAT = (
    TIME_RECORD_PREFIX
    + r"\t%e\t%U\t%S\t%M\t%x\t%w\t%c\t%F\t%R\t%I\t%O"
)
DEFAULT_SAMPLE_INTERVAL = 0.25


class PlantError(RuntimeError):
    """A plant precondition or result contract could not be satisfied."""


@dataclass(frozen=True)
class Workload:
    name: str
    command: tuple[str, ...]
    resource_class: str
    required_tools: tuple[str, ...]
    expected_passed: int
    timeout_seconds: float
    boundary: str


@dataclass(frozen=True)
class Assignment:
    index: int
    workload: Workload


@dataclass
class WorkerResult:
    index: int
    workload: str
    resource_class: str
    boundary: str
    command: list[str]
    launch_offset_seconds: float
    completion_offset_seconds: float
    scheduler_wait_seconds: float
    latency_seconds: float
    exit_code: int | None
    timed_out: bool
    planted: str | None
    log_path: str
    resource_path: str
    log_sha256: str | None
    pytest: dict | None
    resources: dict | None
    collisions: list[dict]
    malformed_reasons: list[str]
    status: str


@dataclass
class _RunningWorker:
    assignment: Assignment
    process: subprocess.Popen
    command: list[str]
    launch_monotonic: float
    launch_offset: float
    timeout_seconds: float
    log_path: Path
    resource_path: Path
    log_handle: object
    planted: str | None


@dataclass
class _HostReading:
    monotonic: float
    cpu_total: int
    cpu_idle: int
    ctxt: int
    processes: int
    procs_running: int
    procs_blocked: int
    mem_total_bytes: int
    mem_available_bytes: int
    load1: float
    load5: float
    load15: float
    pressure: dict[str, dict[str, dict[str, float | int]]]


@dataclass
class _HostSample:
    offset_seconds: float
    cpu_busy_percent: float | None
    ctxt_delta: int | None
    processes_delta: int | None
    procs_running: int
    procs_blocked: int
    mem_total_bytes: int
    mem_available_bytes: int
    load1: float
    load5: float
    load15: float
    pressure: dict[str, dict[str, dict[str, float | int]]]


_MIXED_PATTERN = (
    "cheap-unit",
    "subprocess-heavy",
    "cheap-unit",
    "tmux",
    "subprocess-heavy",
    "cheap-unit",
    "real-bd",
    "cheap-unit",
    "subprocess-heavy",
    "tmux",
    "cheap-unit",
    "subprocess-heavy",
    "real-bd",
    "cheap-unit",
    "tmux",
)

_COLLISION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "dolt-lock",
        re.compile(
            r"(database|dolt|working set).{0,50}(locked|lock timeout|busy)"
            r"|lock file.{0,30}(exists|busy)",
            re.IGNORECASE,
        ),
    ),
    (
        "tmux-socket",
        re.compile(
            r"duplicate session|server exited unexpectedly|lost server"
            r"|error connecting to .{0,120}tmux|address already in use",
            re.IGNORECASE,
        ),
    ),
    (
        "process-resource",
        re.compile(
            r"resource temporarily unavailable|too many open files"
            r"|cannot fork|can't fork|out of memory|cannot allocate memory",
            re.IGNORECASE,
        ),
    ),
    (
        "shared-path",
        re.compile(
            r"file exists: .{0,160}(pytest|tmux|beads|dolt)"
            r"|already exists: .{0,160}(pytest|tmux|beads|dolt)",
            re.IGNORECASE,
        ),
    ),
)

_PYTEST_SUMMARY = re.compile(
    r"(?P<body>\d+\s+(?:passed|failed|skipped|errors?|xfailed|xpassed|warnings?)"
    r"(?:,\s+\d+\s+(?:passed|failed|skipped|errors?|xfailed|xpassed|warnings?))*)"
    r"\s+in\s+(?P<seconds>[0-9]+(?:\.[0-9]+)?)s"
)
_PYTEST_COUNT = re.compile(
    r"(?P<count>\d+)\s+"
    r"(?P<kind>passed|failed|skipped|errors?|xfailed|xpassed|warnings?)"
)


def scenario_catalog(
    *, python_executable: str = sys.executable,
) -> dict[str, Workload]:
    """Return the sealed catalog of scoped benchmark workloads."""
    common = ("-q", "-p", "no:cacheprovider")
    catalog = {
        "cheap-unit": Workload(
            name="cheap-unit",
            command=(
                python_executable,
                "-m",
                "pytest",
                "bin/test_gaudi_prefilter.py",
                *common,
            ),
            resource_class="ordinary",
            required_tools=(),
            expected_passed=25,
            timeout_seconds=30.0,
            boundary="in-process AST decision tests plus pytest startup",
        ),
        "subprocess-heavy": Workload(
            name="subprocess-heavy",
            command=(
                python_executable,
                "-m",
                "pytest",
                "bin/test_sable_contained.py",
                *common,
            ),
            resource_class="ordinary",
            required_tools=("git",),
            expected_passed=44,
            timeout_seconds=90.0,
            boundary="real git repositories and many short subprocesses",
        ),
        "tmux": Workload(
            name="tmux",
            command=(
                python_executable,
                "-m",
                "pytest",
                "bin/test_sable_worker_status_integration.py",
                *common,
                "-rs",
            ),
            resource_class="host-heavy",
            required_tools=("git", "tmux"),
            expected_passed=23,
            timeout_seconds=300.0,
            boundary="real tmux servers, sockets, panes, clients, and process reads",
        ),
        "real-bd": Workload(
            name="real-bd",
            command=(
                python_executable,
                "-m",
                "pytest",
                "bin/test_footprint_lib_integration.py",
                *common,
                "-rs",
            ),
            resource_class="host-heavy",
            required_tools=("bd", "dolt", "git"),
            expected_passed=5,
            timeout_seconds=300.0,
            boundary="real isolated bd stores backed by embedded Dolt plus real git",
        ),
    }
    validate_catalog(catalog)
    return catalog


def validate_catalog(catalog: Mapping[str, Workload]) -> None:
    """Refuse any catalog entry that could invoke broad or sealed testing."""
    expected_names = {"cheap-unit", "subprocess-heavy", "tmux", "real-bd"}
    if set(catalog) != expected_names:
        raise PlantError(
            f"catalog must contain exactly {sorted(expected_names)}; "
            f"got {sorted(catalog)}"
        )
    for name, workload in catalog.items():
        argv = workload.command
        if "pytest" not in argv:
            raise PlantError(f"{name}: benchmark command is not scoped pytest")
        test_paths = [
            token
            for token in argv
            if token.startswith("bin/test_") and token.endswith(".py")
        ]
        if len(test_paths) != 1:
            raise PlantError(
                f"{name}: expected exactly one scoped Python test module; "
                f"got {test_paths}"
            )
        forbidden = {
            "bin/",
            ".github/ci/shell-run-set.sh",
            ".github/ci/sable-sealed-verification.sh",
        }
        if forbidden.intersection(argv):
            raise PlantError(f"{name}: broad/sealed command is forbidden: {argv}")
        if workload.expected_passed <= 0:
            raise PlantError(f"{name}: expected membership must be positive")


def assignments_for(
    scenario: str,
    workers: int,
    *,
    catalog: Mapping[str, Workload] | None = None,
) -> list[Assignment]:
    """Build deterministic worker assignments for one plant."""
    if workers <= 0:
        raise PlantError("workers must be positive")
    catalog = dict(catalog or scenario_catalog())
    if scenario == "mixed":
        names = [_MIXED_PATTERN[i % len(_MIXED_PATTERN)] for i in range(workers)]
    else:
        if scenario not in catalog:
            raise PlantError(
                f"unknown scenario {scenario!r}; expected mixed or "
                f"{', '.join(sorted(catalog))}"
            )
        names = [scenario] * workers
    return [
        Assignment(index=index + 1, workload=catalog[name])
        for index, name in enumerate(names)
    ]


def parse_time_record(text: str) -> dict:
    """Parse the fail-closed GNU-time record emitted for one worker."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise PlantError(f"expected one resource record, got {len(lines)}")
    fields = lines[0].split("\t")
    if len(fields) != 12 or fields[0] != TIME_RECORD_PREFIX:
        raise PlantError(f"malformed resource record: {lines[0]!r}")
    try:
        return {
            "elapsed_seconds": float(fields[1]),
            "user_seconds": float(fields[2]),
            "system_seconds": float(fields[3]),
            "max_rss_kib": int(fields[4]),
            "exit_code": int(fields[5]),
            "voluntary_context_switches": int(fields[6]),
            "involuntary_context_switches": int(fields[7]),
            "major_page_faults": int(fields[8]),
            "minor_page_faults": int(fields[9]),
            "filesystem_inputs": int(fields[10]),
            "filesystem_outputs": int(fields[11]),
        }
    except ValueError as exc:
        raise PlantError(f"non-numeric resource record: {lines[0]!r}") from exc


def parse_pytest_summary(text: str) -> dict | None:
    """Return the final pytest membership summary, if present."""
    matches = list(_PYTEST_SUMMARY.finditer(text))
    if not matches:
        return None
    match = matches[-1]
    counts = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "xfailed": 0,
        "xpassed": 0,
        "warnings": 0,
    }
    for count_match in _PYTEST_COUNT.finditer(match.group("body")):
        kind = count_match.group("kind")
        if kind in {"error", "errors"}:
            kind = "errors"
        elif kind in {"warning", "warnings"}:
            kind = "warnings"
        counts[kind] = int(count_match.group("count"))
    return {**counts, "seconds": float(match.group("seconds"))}


def detect_collisions(text: str, *, max_events: int = 20) -> list[dict]:
    """Extract attributable shared-resource collision signatures."""
    events: list[dict] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        for kind, pattern in _COLLISION_PATTERNS:
            if pattern.search(line):
                events.append(
                    {
                        "kind": kind,
                        "line": line_number,
                        "excerpt": line[:500],
                    }
                )
                break
        if len(events) >= max_events:
            break
    return events


def percentile(values: Sequence[float], quantile: float) -> float | None:
    """Nearest-rank percentile with deterministic small-sample behavior."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(quantile * len(ordered)))
    return ordered[rank - 1]


def _parse_proc_stat(path: Path = Path("/proc/stat")) -> dict:
    values: dict[str, int | tuple[int, int]] = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "cpu":
            numbers = [int(value) for value in fields[1:]]
            idle = numbers[3] + (numbers[4] if len(numbers) > 4 else 0)
            values["cpu"] = (sum(numbers), idle)
        elif fields[0] in {
            "ctxt",
            "processes",
            "procs_running",
            "procs_blocked",
        }:
            values[fields[0]] = int(fields[1])
    required = {
        "cpu",
        "ctxt",
        "processes",
        "procs_running",
        "procs_blocked",
    }
    missing = required - set(values)
    if missing:
        raise PlantError(f"/proc/stat missing fields: {sorted(missing)}")
    return values


def _parse_meminfo(path: Path = Path("/proc/meminfo")) -> tuple[int, int]:
    values = {}
    for line in path.read_text().splitlines():
        key, raw = line.split(":", 1)
        values[key] = int(raw.strip().split()[0]) * 1024
    try:
        return values["MemTotal"], values["MemAvailable"]
    except KeyError as exc:
        raise PlantError(f"/proc/meminfo missing {exc.args[0]}") from exc


def _parse_pressure_file(path: Path) -> dict[str, dict[str, float | int]]:
    result: dict[str, dict[str, float | int]] = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if not fields:
            continue
        row: dict[str, float | int] = {}
        for field_value in fields[1:]:
            key, raw = field_value.split("=", 1)
            row[key] = int(raw) if key == "total" else float(raw)
        result[fields[0]] = row
    return result


def read_host() -> _HostReading:
    """Read one host-wide procfs sample."""
    stat = _parse_proc_stat()
    cpu_total, cpu_idle = stat["cpu"]
    mem_total, mem_available = _parse_meminfo()
    load_fields = Path("/proc/loadavg").read_text().split()
    pressure = {
        resource: _parse_pressure_file(Path("/proc/pressure") / resource)
        for resource in ("cpu", "memory", "io")
    }
    return _HostReading(
        monotonic=time.monotonic(),
        cpu_total=cpu_total,
        cpu_idle=cpu_idle,
        ctxt=int(stat["ctxt"]),
        processes=int(stat["processes"]),
        procs_running=int(stat["procs_running"]),
        procs_blocked=int(stat["procs_blocked"]),
        mem_total_bytes=mem_total,
        mem_available_bytes=mem_available,
        load1=float(load_fields[0]),
        load5=float(load_fields[1]),
        load15=float(load_fields[2]),
        pressure=pressure,
    )


def host_sample(
    current: _HostReading,
    previous: _HostReading | None,
    *,
    plant_started: float,
) -> _HostSample:
    cpu_percent = None
    ctxt_delta = None
    processes_delta = None
    if previous is not None:
        total_delta = current.cpu_total - previous.cpu_total
        idle_delta = current.cpu_idle - previous.cpu_idle
        if total_delta > 0:
            cpu_percent = 100.0 * (total_delta - idle_delta) / total_delta
        ctxt_delta = current.ctxt - previous.ctxt
        processes_delta = current.processes - previous.processes
    return _HostSample(
        offset_seconds=current.monotonic - plant_started,
        cpu_busy_percent=cpu_percent,
        ctxt_delta=ctxt_delta,
        processes_delta=processes_delta,
        procs_running=current.procs_running,
        procs_blocked=current.procs_blocked,
        mem_total_bytes=current.mem_total_bytes,
        mem_available_bytes=current.mem_available_bytes,
        load1=current.load1,
        load5=current.load5,
        load15=current.load15,
        pressure=current.pressure,
    )


def summarize_host_samples(samples: Sequence[_HostSample]) -> dict:
    if not samples:
        raise PlantError("host sampler produced no samples")
    cpu_values = [
        sample.cpu_busy_percent
        for sample in samples
        if sample.cpu_busy_percent is not None
    ]
    first = samples[0]
    last = samples[-1]
    pressure_delta: dict[str, dict[str, int]] = {}
    for resource in ("cpu", "memory", "io"):
        pressure_delta[resource] = {}
        for mode in ("some", "full"):
            if (
                mode in first.pressure[resource]
                and mode in last.pressure[resource]
            ):
                pressure_delta[resource][f"{mode}_total_usec"] = int(
                    last.pressure[resource][mode]["total"]
                ) - int(first.pressure[resource][mode]["total"])
    return {
        "samples": len(samples),
        "cpu_busy_percent_mean": (
            statistics.fmean(cpu_values) if cpu_values else None
        ),
        "cpu_busy_percent_peak": max(cpu_values) if cpu_values else None,
        "load1_peak": max(sample.load1 for sample in samples),
        "load5_peak": max(sample.load5 for sample in samples),
        "procs_running_peak": max(sample.procs_running for sample in samples),
        "procs_blocked_peak": max(sample.procs_blocked for sample in samples),
        "memory_total_bytes": first.mem_total_bytes,
        "memory_available_bytes_min": min(
            sample.mem_available_bytes for sample in samples
        ),
        "memory_used_bytes_peak": max(
            sample.mem_total_bytes - sample.mem_available_bytes
            for sample in samples
        ),
        "context_switches_delta": sum(
            sample.ctxt_delta or 0 for sample in samples
        ),
        "processes_created_delta": sum(
            sample.processes_delta or 0 for sample in samples
        ),
        "pressure_delta": pressure_delta,
    }


def _run_version(
    argv: Sequence[str],
    *,
    cwd: Path,
    output_limit: int = 2000,
    env: Mapping[str, str] | None = None,
) -> dict:
    try:
        cp = subprocess.run(
            list(argv),
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=15,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"argv": list(argv), "ok": False, "detail": str(exc)}
    return {
        "argv": list(argv),
        "ok": cp.returncode == 0,
        "exit_code": cp.returncode,
        "output": cp.stdout.strip()[:output_limit],
    }


def _read_optional(path: Path, *, limit: int = 4000) -> str | None:
    try:
        return path.read_text(errors="replace").strip()[:limit]
    except OSError:
        return None


def _filesystem_fingerprint(path: Path) -> dict:
    values = os.statvfs(path)
    return {
        "path": str(path),
        "block_size": values.f_frsize,
        "blocks_total": values.f_blocks,
        "blocks_available": values.f_bavail,
        "bytes_total": values.f_frsize * values.f_blocks,
        "bytes_available": values.f_frsize * values.f_bavail,
    }


def _host_process_snapshot(*, cwd: Path, limit: int = 40) -> dict:
    """Capture a bounded, argument-free view of ambient CPU consumers."""
    result = _run_version(
        [
            "ps",
            "-eo",
            "pid=,ppid=,stat=,pcpu=,pmem=,rss=,comm=",
            "--sort=-pcpu",
        ],
        cwd=cwd,
        output_limit=20_000,
    )
    if not result["ok"]:
        return result
    lines = result["output"].splitlines()
    result["output"] = None
    rows = []
    benchmark_process = None
    for line in lines:
        fields = line.split(None, 6)
        if len(fields) != 7:
            continue
        row = {
            "pid": int(fields[0]),
            "ppid": int(fields[1]),
            "state": fields[2],
            "cpu_percent": float(fields[3]),
            "memory_percent": float(fields[4]),
            "rss_kib": int(fields[5]),
            "command": fields[6],
        }
        if row["pid"] == os.getpid():
            benchmark_process = row
            continue
        if row["ppid"] == os.getpid():
            continue
        rows.append(row)
    result["benchmark_process"] = benchmark_process
    result["rows"] = rows[:limit]
    result["truncated"] = len(rows) > limit
    return result


def _cpu_model() -> str | None:
    cpuinfo = _read_optional(Path("/proc/cpuinfo"), limit=100_000)
    if cpuinfo is None:
        return None
    for line in cpuinfo.splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return None


def _cgroup_fingerprint() -> dict:
    paths = {
        "membership": Path("/proc/self/cgroup"),
        "cpu_max": Path("/sys/fs/cgroup/cpu.max"),
        "memory_max": Path("/sys/fs/cgroup/memory.max"),
        "memory_current": Path("/sys/fs/cgroup/memory.current"),
        "pids_max": Path("/sys/fs/cgroup/pids.max"),
        "pids_current": Path("/sys/fs/cgroup/pids.current"),
    }
    return {name: _read_optional(path) for name, path in paths.items()}


def tmux_positive_control(*, cwd: Path) -> dict:
    """Prove this environment can create and inspect a real tmux socket."""
    label = f"sable-x2r7g-{uuid.uuid4().hex[:12]}"
    argv = [
        "tmux",
        "-L",
        label,
        "new-session",
        "-d",
        "-s",
        "probe",
        "sleep 5",
        ";",
        "has-session",
        "-t",
        "probe",
        ";",
        "kill-server",
    ]
    with tempfile.TemporaryDirectory(
        prefix="sable-x2r7g-tmux-control-"
    ) as temporary:
        env = dict(os.environ)
        env["TMUX_TMPDIR"] = temporary
        result = _run_version(argv, cwd=cwd, env=env)
    result["socket_label"] = label
    result["isolated_tmpdir"] = True
    return result


def _git_output(repo_root: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-C", str(repo_root), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
    )
    if cp.returncode != 0:
        raise PlantError(cp.stderr.strip() or f"git {' '.join(args)} failed")
    return cp.stdout


def git_fingerprint(repo_root: Path) -> dict:
    return {
        "head": _git_output(repo_root, "rev-parse", "HEAD").strip(),
        "branch": _git_output(
            repo_root, "rev-parse", "--abbrev-ref", "HEAD"
        ).strip(),
        "status_porcelain_v1": _git_output(
            repo_root, "status", "--porcelain=v1", "--untracked-files=all"
        ).splitlines(),
    }


def environment_fingerprint(
    repo_root: Path,
    assignments: Sequence[Assignment],
    *,
    time_binary: str,
) -> dict:
    """Record and positively control every declared benchmark dependency."""
    required = {"git"}
    for assignment in assignments:
        required.update(assignment.workload.required_tools)
    tool_paths = {tool: shutil.which(tool) for tool in sorted(required)}
    missing = [tool for tool, path in tool_paths.items() if path is None]
    if missing:
        raise PlantError(f"required tools unavailable: {' '.join(missing)}")
    if not Path(time_binary).is_file():
        raise PlantError(f"GNU time binary is unavailable: {time_binary}")

    versions = {
        "python": _run_version(
            [sys.executable, "--version"], cwd=repo_root
        ),
        "git": _run_version(["git", "--version"], cwd=repo_root),
    }
    if "tmux" in required:
        versions["tmux"] = _run_version(["tmux", "-V"], cwd=repo_root)
    if "bd" in required:
        versions["bd"] = _run_version(["bd", "version"], cwd=repo_root)
    if "dolt" in required:
        versions["dolt"] = _run_version(["dolt", "version"], cwd=repo_root)
    failed_versions = [
        name for name, record in versions.items() if not record["ok"]
    ]
    if failed_versions:
        raise PlantError(
            "version controls failed: " + " ".join(failed_versions)
        )

    controls = {}
    if "tmux" in required:
        controls["tmux_socket"] = tmux_positive_control(cwd=repo_root)
        if not controls["tmux_socket"]["ok"]:
            raise PlantError(
                "tmux socket positive control failed: "
                + controls["tmux_socket"].get("output", "")
            )

    host = read_host()
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "kernel": platform.release(),
        "python": sys.version,
        "cpu_count": os.cpu_count(),
        "cpu_model": _cpu_model(),
        "memory_total_bytes": host.mem_total_bytes,
        "relevant_environment": {
            name: os.environ.get(name)
            for name in ("PYTEST_ADDOPTS", "TMPDIR", "TMUX_TMPDIR")
        },
        "cgroup": _cgroup_fingerprint(),
        "filesystems": {
            "repository": _filesystem_fingerprint(repo_root),
            "tmp": _filesystem_fingerprint(Path("/tmp")),
        },
        "ambient_processes": _host_process_snapshot(cwd=repo_root),
        "tool_paths": tool_paths,
        "versions": versions,
        "positive_controls": controls,
        "git": git_fingerprint(repo_root),
    }


def snapshot_shared_resources(repo_root: Path) -> dict:
    """Snapshot the bounded shared paths these workloads might collide on."""
    username = pwd.getpwuid(os.getuid()).pw_name
    roots = [
        Path(f"/tmp/tmux-{os.getuid()}"),
        Path("/tmp") / f"pytest-of-{username}",
    ]
    paths: dict[str, list[str] | None] = {}
    for root in roots:
        try:
            paths[str(root)] = sorted(entry.name for entry in root.iterdir())
        except FileNotFoundError:
            paths[str(root)] = None
        except OSError as exc:
            paths[str(root)] = [f"<could-not-read: {exc}>"]
    return {
        "paths": paths,
        "git": git_fingerprint(repo_root),
    }


def shared_resource_delta(before: dict, after: dict) -> dict:
    def digest(values: list[str]) -> str:
        payload = "\0".join(sorted(values)).encode()
        return hashlib.sha256(payload).hexdigest()

    path_delta = {}
    for root in sorted(set(before["paths"]) | set(after["paths"])):
        before_values = before["paths"].get(root)
        after_values = after["paths"].get(root)
        before_known = (
            [] if before_values is None else before_values
        )
        after_known = [] if after_values is None else after_values
        if not isinstance(before_known, list) or not isinstance(after_known, list):
            path_delta[root] = {
                "before": before_values,
                "after": after_values,
                "added": None,
                "removed": None,
            }
            continue
        path_delta[root] = {
            "before_count": len(before_known),
            "after_count": len(after_known),
            "before_sha256": digest(before_known),
            "after_sha256": digest(after_known),
            "added": sorted(set(after_known) - set(before_known)),
            "removed": sorted(set(before_known) - set(after_known)),
        }
    return {
        "paths": path_delta,
        "git_status_changed": (
            before["git"]["status_porcelain_v1"]
            != after["git"]["status_porcelain_v1"]
        ),
        "git_before": before["git"],
        "git_after": after["git"],
    }


def _worker_environment(
    *,
    isolation: str,
    worker_root: Path,
    tmux_tmp_root: Path | None,
    index: int,
    experiment_id: str,
) -> dict[str, str]:
    env = dict(os.environ)
    env["SABLE_TEST_CONTENTION_WORKER"] = str(index)
    env["SABLE_TEST_CONTENTION_EXPERIMENT"] = experiment_id
    if isolation == "native":
        return env
    private_tmp = worker_root / "tmp"
    private_tmp.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(private_tmp)
    if tmux_tmp_root is None:
        raise PlantError("private isolation requires a tmux temp root")
    tmux_tmp_root.mkdir(parents=True, exist_ok=False, mode=0o700)
    env["TMUX_TMPDIR"] = str(tmux_tmp_root)
    if isolation == "private-tmp":
        return env
    if isolation != "private-home":
        raise PlantError(f"unknown isolation mode: {isolation}")
    private_home = worker_root / "home"
    private_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(private_home)
    env["XDG_CACHE_HOME"] = str(private_home / ".cache")
    env["XDG_CONFIG_HOME"] = str(private_home / ".config")
    env["XDG_DATA_HOME"] = str(private_home / ".local" / "share")
    return env


def _planted_command(kind: str, index: int) -> list[str]:
    if kind == "failure":
        return [
            sys.executable,
            "-c",
            (
                "import sys; "
                f"print('SABLE-X2R7G-PLANTED-FAILURE worker={index}', flush=True); "
                "sys.exit(17)"
            ),
        ]
    if kind == "timeout":
        return [
            sys.executable,
            "-c",
            (
                "import time; "
                f"print('SABLE-X2R7G-PLANTED-TIMEOUT worker={index}', flush=True); "
                "time.sleep(3600)"
            ),
        ]
    raise PlantError(f"unknown plant kind: {kind}")


def _start_worker(
    assignment: Assignment,
    *,
    repo_root: Path,
    artifacts_dir: Path,
    plant_started: float,
    isolation: str,
    time_binary: str,
    timeout_override: float | None,
    planted: str | None,
    experiment_id: str,
    isolated_tmux_root: Path | None,
) -> _RunningWorker:
    worker_dir = artifacts_dir / f"worker-{assignment.index:02d}"
    worker_dir.mkdir(parents=True, exist_ok=False)
    log_path = worker_dir / "worker.log"
    resource_path = worker_dir / "resources.tsv"
    command = (
        _planted_command(planted, assignment.index)
        if planted
        else list(assignment.workload.command)
    )
    timed_command = [
        time_binary,
        "--quiet",
        "-f",
        TIME_FORMAT,
        "-o",
        str(resource_path),
        "--",
        *command,
    ]
    env = _worker_environment(
        isolation=isolation,
        worker_root=worker_dir,
        tmux_tmp_root=(
            isolated_tmux_root / f"w{assignment.index:02d}"
            if isolated_tmux_root is not None
            else None
        ),
        index=assignment.index,
        experiment_id=experiment_id,
    )
    log_handle = log_path.open("w", encoding="utf-8")
    header = {
        "worker": assignment.index,
        "workload": assignment.workload.name,
        "resource_class": assignment.workload.resource_class,
        "command": command,
        "planted": planted,
    }
    log_handle.write("# " + json.dumps(header, sort_keys=True) + "\n")
    log_handle.flush()
    launched = time.monotonic()
    process = subprocess.Popen(
        timed_command,
        cwd=repo_root,
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return _RunningWorker(
        assignment=assignment,
        process=process,
        command=command,
        launch_monotonic=launched,
        launch_offset=launched - plant_started,
        timeout_seconds=(
            timeout_override
            if timeout_override is not None
            else assignment.workload.timeout_seconds
        ),
        log_path=log_path,
        resource_path=resource_path,
        log_handle=log_handle,
        planted=planted,
    )


def _terminate_worker(running: _RunningWorker) -> int | None:
    try:
        os.killpg(running.process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        return running.process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(running.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            return running.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return None


def _sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _attributable_processes(experiment_id: str) -> list[dict]:
    """Find surviving processes that inherited this plant's unique marker."""
    marker = (
        f"SABLE_TEST_CONTENTION_EXPERIMENT={experiment_id}".encode() + b"\0"
    )
    command_marker = f"sable-x2r7g-{experiment_id[:8]}-"
    found = []
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            environment = (proc_dir / "environ").read_bytes()
        except OSError:
            continue
        try:
            raw_cmdline = (proc_dir / "cmdline").read_bytes()
            cmdline = raw_cmdline.replace(b"\0", b" ").decode(
                errors="replace"
            ).strip()[:500]
            stat_fields = (proc_dir / "stat").read_text().rsplit(
                ")", 1
            )[1].split()
        except OSError:
            continue
        if marker not in environment and command_marker not in cmdline:
            continue
        found.append(
            {
                "pid": int(proc_dir.name),
                "ppid": int(stat_fields[1]),
                "starttime_ticks": int(stat_fields[19]),
                "comm": _read_optional(proc_dir / "comm"),
                "cmdline": cmdline,
            }
        )
    return sorted(found, key=lambda row: row["pid"])


def _same_process(record: Mapping) -> bool:
    try:
        fields = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(
            ")", 1
        )[1].split()
        return (
            fields[0] != "Z"
            and int(fields[19]) == record["starttime_ticks"]
        )
    except (OSError, ValueError, IndexError):
        return False


def _cleanup_process_records(
    records: Sequence[Mapping],
    *,
    term_grace_seconds: float = 2.0,
) -> dict:
    """Terminate only the immutable PID identities recorded for this plant."""
    attempted = []
    errors = []
    for record in records:
        if not _same_process(record):
            continue
        attempted.append(record["pid"])
        try:
            os.kill(record["pid"], signal.SIGTERM)
        except (OSError, ProcessLookupError) as exc:
            errors.append(f"SIGTERM {record['pid']}: {exc}")

    deadline = time.monotonic() + term_grace_seconds
    while time.monotonic() < deadline:
        if not any(_same_process(record) for record in records):
            break
        time.sleep(0.05)

    sigkill = []
    for record in records:
        if not _same_process(record):
            continue
        sigkill.append(record["pid"])
        try:
            os.kill(record["pid"], signal.SIGKILL)
        except (OSError, ProcessLookupError) as exc:
            errors.append(f"SIGKILL {record['pid']}: {exc}")
    kill_deadline = time.monotonic() + 1.0
    while time.monotonic() < kill_deadline:
        if not any(_same_process(record) for record in records):
            break
        time.sleep(0.05)
    return {
        "attempted_pids": attempted,
        "sigkill_pids": sigkill,
        "remaining_pids": [
            record["pid"] for record in records if _same_process(record)
        ],
        "errors": errors,
    }


def _artifact_sockets(artifacts_dir: Path) -> list[str]:
    sockets = []
    for path in artifacts_dir.rglob("*"):
        try:
            mode = path.stat(follow_symlinks=False).st_mode
        except OSError:
            continue
        if stat.S_ISSOCK(mode):
            sockets.append(str(path.relative_to(artifacts_dir)))
    return sorted(sockets)


def _finish_worker(
    running: _RunningWorker,
    *,
    plant_started: float,
    timed_out: bool,
) -> WorkerResult:
    completed = time.monotonic()
    running.log_handle.close()
    try:
        log_text = running.log_path.read_text(errors="replace")
    except OSError as exc:
        log_text = ""
        log_error = str(exc)
    else:
        log_error = None

    malformed = []
    if log_error:
        malformed.append(f"log unreadable: {log_error}")
    resources = None
    try:
        resources = parse_time_record(
            running.resource_path.read_text(encoding="utf-8")
        )
    except (OSError, PlantError) as exc:
        malformed.append(f"resource record unavailable/malformed: {exc}")

    exit_code = running.process.returncode
    if resources is not None and exit_code is not None:
        if resources["exit_code"] != exit_code:
            malformed.append(
                "resource exit code "
                f"{resources['exit_code']} != parent exit code {exit_code}"
            )

    pytest_summary = parse_pytest_summary(log_text)
    if running.planted is None:
        if pytest_summary is None:
            malformed.append("missing pytest membership summary")
        else:
            expected = running.assignment.workload.expected_passed
            if pytest_summary["passed"] != expected:
                malformed.append(
                    f"pytest passed {pytest_summary['passed']} != expected {expected}"
                )
            for field_name in (
                "failed",
                "skipped",
                "errors",
                "xfailed",
                "xpassed",
            ):
                if pytest_summary[field_name] != 0:
                    malformed.append(
                        f"pytest {field_name}={pytest_summary[field_name]} != 0"
                    )

    status = "passed"
    if timed_out:
        status = "timeout"
    elif exit_code != 0:
        status = "failed"
    elif malformed:
        status = "malformed"
    elif running.planted is not None:
        malformed.append(
            f"planted {running.planted} unexpectedly exited zero"
        )
        status = "malformed"

    return WorkerResult(
        index=running.assignment.index,
        workload=running.assignment.workload.name,
        resource_class=running.assignment.workload.resource_class,
        boundary=running.assignment.workload.boundary,
        command=running.command,
        launch_offset_seconds=running.launch_offset,
        completion_offset_seconds=completed - plant_started,
        scheduler_wait_seconds=running.launch_offset,
        latency_seconds=completed - running.launch_monotonic,
        exit_code=exit_code,
        timed_out=timed_out,
        planted=running.planted,
        log_path=str(running.log_path),
        resource_path=str(running.resource_path),
        log_sha256=_sha256(running.log_path),
        pytest=pytest_summary,
        resources=resources,
        collisions=detect_collisions(log_text),
        malformed_reasons=malformed,
        status=status,
    )


def _can_launch(
    assignment: Assignment,
    running: Iterable[_RunningWorker],
    *,
    parallelism: int,
    heavy_slots: int | None,
) -> bool:
    running_list = list(running)
    if len(running_list) >= parallelism:
        return False
    if (
        heavy_slots is None
        or assignment.workload.resource_class != "host-heavy"
    ):
        return True
    active_heavy = sum(
        worker.assignment.workload.resource_class == "host-heavy"
        for worker in running_list
    )
    return active_heavy < heavy_slots


def summarize_workers(results: Sequence[WorkerResult], wall_seconds: float) -> dict:
    latencies = [result.latency_seconds for result in results]
    waits = [result.scheduler_wait_seconds for result in results]
    resource_rows = [
        result.resources for result in results if result.resources is not None
    ]
    resources_complete = len(resource_rows) == len(results)
    counts = {
        status: sum(result.status == status for result in results)
        for status in ("passed", "failed", "timeout", "malformed")
    }
    by_workload = {}
    for workload in sorted({result.workload for result in results}):
        rows = [result for result in results if result.workload == workload]
        row_latencies = [result.latency_seconds for result in rows]
        by_workload[workload] = {
            "workers": len(rows),
            "passed": sum(result.status == "passed" for result in rows),
            "latency_seconds": {
                "min": min(row_latencies),
                "median": statistics.median(row_latencies),
                "p95": percentile(row_latencies, 0.95),
                "max": max(row_latencies),
                "mean": statistics.fmean(row_latencies),
            },
        }
    return {
        "workers": len(results),
        **counts,
        "wall_seconds": wall_seconds,
        "aggregate_worker_wall_seconds": sum(latencies),
        "resource_records": len(resource_rows),
        "aggregate_user_seconds": (
            sum(row["user_seconds"] for row in resource_rows)
            if resources_complete
            else None
        ),
        "aggregate_system_seconds": (
            sum(row["system_seconds"] for row in resource_rows)
            if resources_complete
            else None
        ),
        "peak_worker_rss_kib_sum": (
            sum(row["max_rss_kib"] for row in resource_rows)
            if resources_complete
            else None
        ),
        "latency_seconds": {
            "min": min(latencies) if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
            "p95": percentile(latencies, 0.95),
            "max": max(latencies) if latencies else None,
            "mean": statistics.fmean(latencies) if latencies else None,
        },
        "scheduler_wait_seconds": {
            "min": min(waits) if waits else None,
            "median": statistics.median(waits) if waits else None,
            "p95": percentile(waits, 0.95),
            "max": max(waits) if waits else None,
        },
        "collision_events": sum(len(result.collisions) for result in results),
        "by_workload": by_workload,
    }


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def run_plant(
    *,
    repo_root: Path,
    scenario: str,
    workers: int,
    parallelism: int,
    report_path: Path,
    artifacts_dir: Path,
    isolation: str = "native",
    heavy_slots: int | None = None,
    timeout_override: float | None = None,
    sample_interval: float = DEFAULT_SAMPLE_INTERVAL,
    plant_failure: int | None = None,
    plant_timeout: int | None = None,
    label: str | None = None,
    require_clean: bool = False,
    time_binary: str = "/usr/bin/time",
    catalog: Mapping[str, Workload] | None = None,
) -> dict:
    """Run one attributable plant and atomically write its JSON report."""
    experiment_id = uuid.uuid4().hex
    repo_root = repo_root.resolve()
    report_path = report_path.resolve()
    artifacts_dir = artifacts_dir.resolve()
    if report_path.exists():
        raise PlantError(f"report already exists: {report_path}")
    if artifacts_dir.exists():
        raise PlantError(f"artifact directory already exists: {artifacts_dir}")
    if parallelism <= 0 or parallelism > workers:
        raise PlantError("parallelism must be between 1 and workers")
    if heavy_slots is not None and heavy_slots <= 0:
        raise PlantError("heavy_slots must be positive when configured")
    if sample_interval <= 0:
        raise PlantError("sample_interval must be positive")
    if timeout_override is not None and timeout_override <= 0:
        raise PlantError("timeout_override must be positive")
    if plant_failure is not None and plant_timeout is not None:
        if plant_failure == plant_timeout:
            raise PlantError("one worker cannot carry both failure plants")
    for planted_index in (plant_failure, plant_timeout):
        if planted_index is not None and not 1 <= planted_index <= workers:
            raise PlantError(
                f"planted worker {planted_index} outside 1..{workers}"
            )
    if isolation not in {"native", "private-tmp", "private-home"}:
        raise PlantError(f"unsupported isolation mode: {isolation}")

    assignments = assignments_for(
        scenario, workers, catalog=catalog or scenario_catalog()
    )
    environment = environment_fingerprint(
        repo_root, assignments, time_binary=time_binary
    )
    if require_clean and environment["git"]["status_porcelain_v1"]:
        raise PlantError(
            "benchmark requires a clean worktree; found: "
            + " | ".join(environment["git"]["status_porcelain_v1"])
        )

    isolated_tmux_root = (
        Path("/tmp") / f"sx2t-{experiment_id[:8]}"
        if isolation != "native"
        else None
    )
    if isolated_tmux_root is not None and isolated_tmux_root.exists():
        raise PlantError(
            f"private tmux root already exists: {isolated_tmux_root}"
        )
    artifacts_dir.mkdir(parents=True, exist_ok=False)
    if isolated_tmux_root is not None:
        isolated_tmux_root.mkdir(mode=0o700)
    before = snapshot_shared_resources(repo_root)
    started_wall = datetime.now(timezone.utc)
    started = time.monotonic()
    pending = list(assignments)
    running: dict[int, _RunningWorker] = {}
    results: list[WorkerResult] = []
    baseline_reading = read_host()
    readings: list[_HostReading] = [baseline_reading]
    samples: list[_HostSample] = [
        host_sample(baseline_reading, None, plant_started=started)
    ]
    previous_reading = baseline_reading
    next_sample = started + sample_interval

    try:
        while pending or running:
            launched_any = True
            while pending and launched_any:
                launched_any = False
                for assignment in list(pending):
                    if not _can_launch(
                        assignment,
                        running.values(),
                        parallelism=parallelism,
                        heavy_slots=heavy_slots,
                    ):
                        continue
                    planted = None
                    if assignment.index == plant_failure:
                        planted = "failure"
                    elif assignment.index == plant_timeout:
                        planted = "timeout"
                    running[assignment.index] = _start_worker(
                        assignment,
                        repo_root=repo_root,
                        artifacts_dir=artifacts_dir,
                        plant_started=started,
                        isolation=isolation,
                        time_binary=time_binary,
                        timeout_override=timeout_override,
                        planted=planted,
                        experiment_id=experiment_id,
                        isolated_tmux_root=isolated_tmux_root,
                    )
                    pending.remove(assignment)
                    launched_any = True
                    if len(running) >= parallelism:
                        break

            now = time.monotonic()
            if now >= next_sample or not samples:
                reading = read_host()
                readings.append(reading)
                samples.append(
                    host_sample(
                        reading, previous_reading, plant_started=started
                    )
                )
                previous_reading = reading
                next_sample = now + sample_interval

            for index, worker in list(running.items()):
                return_code = worker.process.poll()
                timed_out = False
                if (
                    return_code is None
                    and now - worker.launch_monotonic > worker.timeout_seconds
                ):
                    return_code = _terminate_worker(worker)
                    worker.process.returncode = return_code
                    timed_out = True
                if return_code is None:
                    continue
                results.append(
                    _finish_worker(
                        worker,
                        plant_started=started,
                        timed_out=timed_out,
                    )
                )
                del running[index]

            if pending or running:
                time.sleep(min(sample_interval / 4, 0.05))
    finally:
        for worker in running.values():
            _terminate_worker(worker)
            try:
                worker.log_handle.close()
            except OSError:
                pass

    final_reading = read_host()
    if not readings or final_reading.monotonic > readings[-1].monotonic:
        samples.append(
            host_sample(
                final_reading, previous_reading, plant_started=started
            )
        )
    completed = time.monotonic()
    ended_wall = datetime.now(timezone.utc)
    after = snapshot_shared_resources(repo_root)
    delta = shared_resource_delta(before, after)
    before_cleanup = _attributable_processes(experiment_id)
    cleanup = _cleanup_process_records(before_cleanup)
    after_cleanup = _attributable_processes(experiment_id)
    delta["attributable_processes_before_cleanup"] = before_cleanup
    delta["process_cleanup"] = cleanup
    delta["attributable_processes_after_cleanup"] = after_cleanup
    delta["artifact_socket_residue"] = _artifact_sockets(artifacts_dir)
    delta["isolated_socket_residue"] = (
        _artifact_sockets(isolated_tmux_root)
        if isolated_tmux_root is not None
        else []
    )
    if isolated_tmux_root is not None:
        shutil.rmtree(isolated_tmux_root)
    delta["isolated_tmux_root_cleaned"] = (
        isolated_tmux_root is None or not isolated_tmux_root.exists()
    )
    tmux_root = f"/tmp/tmux-{os.getuid()}"
    delta["attributable_shared_path_residue"] = [
        entry
        for entry in (delta["paths"].get(tmux_root, {}).get("added") or [])
        if entry.startswith(f"sable-x2r7g-{experiment_id[:8]}-")
    ]
    results.sort(key=lambda result: result.index)
    worker_summary = summarize_workers(results, completed - started)
    host_summary = summarize_host_samples(samples)

    reasons = []
    if len(results) != workers:
        reasons.append(f"only {len(results)} of {workers} worker results exist")
    for result in results:
        if result.status != "passed":
            reasons.append(
                f"worker {result.index} ({result.workload}) {result.status}"
            )
    if delta["git_status_changed"]:
        reasons.append("benchmark changed the repository worktree")
    if worker_summary["collision_events"]:
        reasons.append(
            f"{worker_summary['collision_events']} attributable collision "
            "signature(s) appeared in worker logs"
        )
    if delta["attributable_processes_before_cleanup"]:
        reasons.append(
            "plant-owned processes survived worker shutdown: "
            + repr(delta["attributable_processes_before_cleanup"])
        )
    if delta["attributable_processes_after_cleanup"]:
        reasons.append(
            "plant-owned processes survived parent cleanup: "
            + repr(delta["attributable_processes_after_cleanup"])
        )
    if cleanup["errors"]:
        reasons.append("plant process cleanup errors: " + repr(cleanup["errors"]))
    report = {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "label": label,
        "started_at": started_wall.isoformat(),
        "ended_at": ended_wall.isoformat(),
        "source": environment["git"],
        "config": {
            "scenario": scenario,
            "workers": workers,
            "parallelism": parallelism,
            "isolation": isolation,
            "heavy_slots": heavy_slots,
            "timeout_override_seconds": timeout_override,
            "sample_interval_seconds": sample_interval,
            "plant_failure": plant_failure,
            "plant_timeout": plant_timeout,
            "mixed_pattern": list(_MIXED_PATTERN) if scenario == "mixed" else None,
        },
        "environment": environment,
        "workers": [asdict(result) for result in results],
        "summary": {
            **worker_summary,
            "host": host_summary,
            "shared_resources": delta,
        },
        "host_samples": [asdict(sample) for sample in samples],
        "verdict": {
            "status": "passed" if not reasons else "failed",
            "reasons": reasons,
        },
    }
    _atomic_json(report_path, report)
    return report


def render_summary(report: Mapping) -> str:
    summary = report["summary"]
    latency = summary["latency_seconds"]
    host = summary["host"]
    aggregate_cpu = None
    if (
        summary["aggregate_user_seconds"] is not None
        and summary["aggregate_system_seconds"] is not None
    ):
        aggregate_cpu = (
            summary["aggregate_user_seconds"]
            + summary["aggregate_system_seconds"]
        )
    aggregate_cpu_text = (
        f"{aggregate_cpu:.2f}s" if aggregate_cpu is not None else "unknown"
    )
    lines = [
        (
            f"sable-test-contention: {report['verdict']['status'].upper()} "
            f"{summary['passed']}/{summary['workers']} workers passed"
        ),
        (
            f"  wall={summary['wall_seconds']:.2f}s "
            f"aggregate-worker={summary['aggregate_worker_wall_seconds']:.2f}s "
            f"aggregate-cpu={aggregate_cpu_text}"
        ),
        (
            f"  latency min/median/p95/max="
            f"{latency['min']:.2f}/{latency['median']:.2f}/"
            f"{latency['p95']:.2f}/{latency['max']:.2f}s"
        ),
        (
            f"  host cpu mean/peak="
            f"{host['cpu_busy_percent_mean']:.1f}/"
            f"{host['cpu_busy_percent_peak']:.1f}% "
            f"memory-available-min={host['memory_available_bytes_min'] / 2**30:.2f}GiB "
            f"load1-peak={host['load1_peak']:.2f}"
        ),
        (
            f"  failures={summary['failed']} timeouts={summary['timeout']} "
            f"malformed={summary['malformed']} "
            f"collisions={summary['collision_events']}"
        ),
    ]
    if report["verdict"]["reasons"]:
        lines.extend(
            f"  RED: {reason}" for reason in report["verdict"]["reasons"]
        )
    return "\n".join(lines)
