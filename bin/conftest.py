"""Pytest cost reporting and fail-closed ordinary-test budgets (SABLE-ssu6v).

This adapter measures the test process pytest is already running; it never
starts a second suite. Files named ``*_integration.py`` are an explicit tier
declaration. An ordinary module may opt into measured heavyweight behavior
with a reasoned file-level comment:

    # sable-test-load: measured-slow -- REASON

The thresholds are intentionally generous. Their job is to catch accidental
host setup or nested runners entering ordinary tests, not micro-regressions.
"""

from __future__ import annotations

import json
import re
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


def pytest_addoption(parser):
    parser.addoption(
        "--sable-test-cost-report",
        metavar="PATH",
        help="Write full-precision per-test/module duration JSON from this run.",
    )


def pytest_sessionstart(session):
    # pytest.main() can be called more than once in one interpreter by tooling.
    # A report must describe this session only, never inherit the prior run.
    _DURATIONS.clear()


def pytest_runtest_logreport(report):
    _DURATIONS[report.nodeid] = _DURATIONS.get(report.nodeid, 0.0) + report.duration


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

    if not cost_report["violations"]:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
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
