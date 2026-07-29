"""Pytest cost/skip reporting and fail-closed ordinary-test budgets.

This adapter measures the test process pytest is already running; it never
starts a second suite. Files named ``*_integration.py`` are an explicit tier
declaration. An ordinary module may opt into measured heavyweight behavior
with a reasoned file-level comment:

    # sable-test-load: measured-slow -- REASON

The thresholds are intentionally generous. Their job is to catch accidental
host setup or nested runners entering ordinary tests, not micro-regressions.

``--sable-report-skip-set`` makes a full run self-describing: it names every
currently skipped node and compares those identities with the previous
successful full run. Pytest's ``-rs`` supplies the reasons in its standard
summary; the SABLE report supplies the missing set history, including removed
skips that cannot appear in the current run's summary.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

MAX_ORDINARY_TEST_SECONDS = 10.0
MAX_ORDINARY_MODULE_SECONDS = 45.0
_MEASURED_SLOW_DECLARATION = re.compile(
    r"^\s*#\s*sable-test-load:\s*[a-z0-9_, -]*\bmeasured-slow\b"
    r"[a-z0-9_, -]*\s+--\s+\S",
    re.MULTILINE,
)

_DURATIONS: dict[str, float] = {}
_SKIPS: dict[str, str] = {}
_SKIP_STATE_VERSION = 1
_SKIP_STATE_ENV = "SABLE_PYTEST_SKIP_SET_STATE"
_SESSION_STARTED_NS = 0


def _module_path(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def declared_heavy_modules(
    module_paths: set[str], *, repo_root: Path
) -> set[str]:
    declared = set()
    for module_path in module_paths:
        path = repo_root / module_path
        if path.name.endswith("_integration.py"):
            declared.add(module_path)
            continue
        try:
            source = path.read_text(errors="replace")
        except OSError:
            continue
        if _MEASURED_SLOW_DECLARATION.search(source):
            declared.add(module_path)
    return declared


def build_cost_report(
    durations: dict[str, float],
    *,
    declared_modules: set[str],
    max_test_seconds: float = MAX_ORDINARY_TEST_SECONDS,
    max_module_seconds: float = MAX_ORDINARY_MODULE_SECONDS,
) -> dict:
    """Build a deterministic report and list undeclared budget violations."""
    module_totals: dict[str, float] = {}
    tests = []
    violations = []
    for nodeid, duration in sorted(durations.items()):
        module = _module_path(nodeid)
        module_totals[module] = module_totals.get(module, 0.0) + duration
        declared = module in declared_modules
        record = {
            "nodeid": nodeid,
            "module": module,
            "seconds": duration,
            "declared_heavy": declared,
        }
        tests.append(record)
        if not declared and duration > max_test_seconds:
            violations.append({
                "kind": "test",
                "nodeid": nodeid,
                "seconds": duration,
                "limit_seconds": max_test_seconds,
            })

    modules = []
    for module, duration in sorted(module_totals.items()):
        declared = module in declared_modules
        modules.append({
            "module": module,
            "seconds": duration,
            "declared_heavy": declared,
        })
        if not declared and duration > max_module_seconds:
            violations.append({
                "kind": "module",
                "nodeid": module,
                "seconds": duration,
                "limit_seconds": max_module_seconds,
            })
    violations.sort(key=lambda r: (r["kind"], r["nodeid"]))
    return {
        "thresholds": {
            "ordinary_test_seconds": max_test_seconds,
            "ordinary_module_seconds": max_module_seconds,
        },
        "tests": tests,
        "modules": modules,
        "violations": violations,
    }


def _one_line(value: object) -> str:
    return " ".join(str(value).split()) or "(no reason reported)"


def _skip_reason(report) -> str:
    """Return pytest's reason without coupling to one longrepr representation."""
    longrepr = getattr(report, "longrepr", "")
    if isinstance(longrepr, tuple) and len(longrepr) >= 3:
        reason = _one_line(longrepr[-1])
    else:
        reason = _one_line(longrepr)
    if reason.startswith("Skipped:"):
        reason = reason.removeprefix("Skipped:").strip()
    return reason or "(no reason reported)"


def primary_outcome_counts(stats: dict[str, list]) -> dict[str, int]:
    """Return one node-level pass/fail/skip count, with xfails kept separate."""
    passed = {
        report.nodeid
        for report in stats.get("passed", ())
        if hasattr(report, "nodeid")
    }
    failed = {
        report.nodeid
        for category in ("failed", "error", "errors")
        for report in stats.get(category, ())
        if hasattr(report, "nodeid")
    }
    skipped = {
        report.nodeid
        for report in stats.get("skipped", ())
        if hasattr(report, "nodeid") and not hasattr(report, "wasxfail")
    }
    # A teardown error can follow a passed call report for the same node.
    passed.difference_update(failed | skipped)
    return {
        "passed": len(passed),
        "failed": len(failed),
        "skipped": len(skipped),
    }


def format_skip_set_report(
    previous: dict[str, str] | None,
    current: dict[str, str],
) -> list[str]:
    """Render current identities plus a directional delta from the last green.

    A count may accompany the report, but it is never the evidence: every
    current, added, and removed skip is named individually.
    """
    current = dict(sorted(current.items()))
    previous = None if previous is None else dict(sorted(previous.items()))
    if previous is None:
        lines = [
            "SABLE pytest skip set: no previous successful full run; "
            f"observed {len(current)} current skip(s)"
        ]
    else:
        lines = [
            f"SABLE pytest skip set: {len(current)} current skip(s), "
            f"{len(previous)} in the previous successful full run"
        ]

    if current:
        lines.extend(
            f"CURRENT skip: {nodeid} — {reason}"
            for nodeid, reason in current.items()
        )
    else:
        lines.append("CURRENT skips: none")

    if previous is None:
        return lines

    added = sorted(set(current) - set(previous))
    removed = sorted(set(previous) - set(current))
    if not added and not removed:
        lines.append("SABLE pytest skip set unchanged")
        return lines

    lines.extend(
        f"ADDED skip: {nodeid} — {current[nodeid]}"
        for nodeid in added
    )
    lines.extend(
        f"REMOVED skip: {nodeid} — {previous[nodeid]}"
        for nodeid in removed
    )
    return lines


def _read_skip_set(path: Path) -> tuple[dict[str, str] | None, int]:
    if not path.exists():
        return None, 0
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("top-level value must be an object")
    if payload.get("version") != _SKIP_STATE_VERSION:
        raise ValueError(f"unsupported version {payload.get('version')!r}")
    skips = payload.get("skips")
    if not isinstance(skips, dict) or any(
        not isinstance(nodeid, str) or not isinstance(reason, str)
        for nodeid, reason in skips.items()
    ):
        raise ValueError("'skips' must be a string-to-string object")
    run_started_ns = payload.get("run_started_ns", 0)
    if not isinstance(run_started_ns, int) or isinstance(run_started_ns, bool):
        raise ValueError("'run_started_ns' must be an integer")
    return dict(sorted(skips.items())), run_started_ns


def _write_skip_set(
    path: Path,
    skips: dict[str, str],
    *,
    run_started_ns: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(
            {
                "version": _SKIP_STATE_VERSION,
                "run_started_ns": run_started_ns,
                "skips": dict(sorted(skips.items())),
            },
            indent=2,
        ) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _skip_state_lock(state_path: Path):
    """Serialize the read/compare/write transaction across concurrent runs."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_name(f"{state_path.name}.lock")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def report_skip_set(
    current: dict[str, str],
    *,
    state_path: Path,
    completed_successfully: bool,
    run_started_ns: int | None = None,
) -> list[str]:
    """Build a report and atomically advance a non-stale green baseline."""
    current = dict(sorted(current.items()))
    started_ns = time.time_ns() if run_started_ns is None else run_started_ns
    if not completed_successfully:
        lines = format_skip_set_report(None, current)
        lines[0] = (
            "SABLE pytest skip set: unsuccessful run observed "
            f"{len(current)} current skip(s); ADDED/REMOVED delta not assessed"
        )
        lines.append(
            "SABLE pytest skip-set baseline not replaced because this run "
            "was not successful"
        )
        return lines

    try:
        with _skip_state_lock(state_path):
            warning = ""
            try:
                previous, previous_started_ns = _read_skip_set(state_path)
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                previous, previous_started_ns = None, 0
                warning = (
                    f"SABLE pytest skip-set baseline unreadable at {state_path}: "
                    f"{_one_line(exc)}"
                )

            lines = format_skip_set_report(previous, current)
            if warning:
                lines.insert(0, warning)

            if previous_started_ns > started_ns:
                lines.append(
                    "SABLE pytest skip-set baseline not replaced because a "
                    "newer successful full run finished first"
                )
                return lines

            try:
                _write_skip_set(
                    state_path,
                    current,
                    run_started_ns=started_ns,
                )
            except OSError as exc:
                lines.append(
                    f"SABLE pytest skip-set baseline NOT WRITTEN at {state_path}: "
                    f"{_one_line(exc)}; the next run cannot report a delta"
                )
            return lines
    except OSError as exc:
        lines = format_skip_set_report(None, current)
        lines.insert(
            0,
            f"SABLE pytest skip-set state lock unavailable at {state_path}: "
            f"{_one_line(exc)}",
        )
        lines.append(
            "SABLE pytest skip-set baseline NOT WRITTEN; the next run cannot "
            "report a delta"
        )
        return lines


def _skip_state_path(config) -> Path:
    override = os.environ.get(_SKIP_STATE_ENV)
    if override:
        return Path(override)
    pytest_root = Path(str(config.rootpath)).resolve()
    repo_root = next(
        (
            candidate
            for candidate in (pytest_root, *pytest_root.parents)
            if (candidate / ".git").exists()
        ),
        pytest_root,
    )
    return (
        repo_root
        / ".claude"
        / "sable"
        / "state"
        / "pytest-skip-set.json"
    )


def pytest_addoption(parser):
    parser.addoption(
        "--sable-test-cost-report",
        metavar="PATH",
        help="Write full-precision per-test/module duration JSON from this run.",
    )
    parser.addoption(
        "--sable-report-skip-set",
        action="store_true",
        help=(
            "Name every skipped node and report ADDED/REMOVED identities "
            "against the previous successful full run."
        ),
    )


def pytest_sessionstart(session):
    # pytest.main() can be called more than once in one interpreter by tooling.
    # A report must describe this session only, never inherit the prior run.
    global _SESSION_STARTED_NS
    _SESSION_STARTED_NS = time.time_ns()
    _DURATIONS.clear()
    _SKIPS.clear()


def _record_skip(report) -> None:
    # Expected xfails use pytest's protocol-level ``skipped`` outcome too, but
    # the terminal summary classifies them as XFAIL, not SKIPPED. Keep this set
    # aligned with the identities behind pytest's actual skipped count.
    if getattr(report, "skipped", False) and not hasattr(report, "wasxfail"):
        _SKIPS[report.nodeid] = _skip_reason(report)


def pytest_runtest_logreport(report):
    _DURATIONS[report.nodeid] = _DURATIONS.get(report.nodeid, 0.0) + report.duration
    _record_skip(report)


def pytest_collectreport(report):
    # pytest.importorskip() at module import time never reaches runtest hooks;
    # its collection report is the only identity/reason pytest emits.
    _record_skip(report)


def pytest_sessionfinish(session, exitstatus):
    repo_root = Path(str(session.config.rootpath))
    modules = {_module_path(nodeid) for nodeid in _DURATIONS}
    declared = declared_heavy_modules(modules, repo_root=repo_root)
    cost_report = build_cost_report(_DURATIONS, declared_modules=declared)

    report_path = session.config.getoption("--sable-test-cost-report")
    if report_path:
        path = Path(report_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cost_report, indent=2) + "\n")

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if session.config.getoption("--sable-report-skip-set"):
        lines = report_skip_set(
            _SKIPS,
            state_path=_skip_state_path(session.config),
            completed_successfully=(
                int(exitstatus) == int(pytest.ExitCode.OK)
                and not cost_report["violations"]
            ),
            run_started_ns=_SESSION_STARTED_NS,
        )
        if reporter is not None:
            counts = primary_outcome_counts(reporter.stats)
            lines.insert(
                0,
                "SABLE pytest outcomes: "
                f"passed={counts['passed']} failed={counts['failed']} "
                f"skipped={counts['skipped']}",
            )
            reporter.write_line("")
            reporter.write_sep("=", "SABLE pytest skip-set report")
            for line in lines:
                reporter.write_line(line)
        else:
            print("\n".join(lines))

    if cost_report["violations"]:
        if reporter is not None:
            reporter.write_sep("=", "SABLE ordinary-test load budget exceeded", red=True)
            for violation in cost_report["violations"]:
                reporter.write_line(
                    f"{violation['kind']}: {violation['nodeid']} took "
                    f"{violation['seconds']:.2f}s > {violation['limit_seconds']:.2f}s; "
                    "move true E2E coverage to *_integration.py, remove accidental "
                    "load, or declare '# sable-test-load: measured-slow -- REASON'",
                    red=True,
                )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
