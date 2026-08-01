"""Unit tests for the SABLE pytest load-budget adapter."""

import json
from pathlib import Path
from types import SimpleNamespace

import conftest as load_guard


def test_new_session_discards_prior_in_process_measurements(monkeypatch):
    # Replace the maps rather than clearing the outer pytest session's live
    # plugin state while this unit test itself is being measured.
    isolated_durations = {"bin/test_old.py::test_old": 123.0}
    isolated_skips = {"bin/test_old.py::test_old": "old reason"}
    monkeypatch.setattr(load_guard, "_DURATIONS", isolated_durations)
    monkeypatch.setattr(load_guard, "_SKIPS", isolated_skips)
    monkeypatch.setattr(load_guard, "_SESSION_STARTED_NS", 0)

    load_guard.pytest_sessionstart(None)

    assert isolated_durations == {}
    assert isolated_skips == {}
    assert load_guard._SESSION_STARTED_NS > 0


def test_xdist_worker_never_publishes_partial_session_artifacts(monkeypatch, tmp_path):
    """Only xdist's controller owns the aggregate report and skip baseline."""
    partial_report = tmp_path / "worker-cost.json"
    skip_writes = []
    config = SimpleNamespace(
        workerinput={"workerid": "gw0"},
        rootpath=tmp_path,
        getoption=lambda name: (
            str(partial_report)
            if name == "--sable-test-cost-report"
            else name == "--sable-report-skip-set"
        ),
        pluginmanager=SimpleNamespace(get_plugin=lambda name: None),
    )
    session = SimpleNamespace(config=config, exitstatus=0)
    monkeypatch.setattr(
        load_guard,
        "_DURATIONS",
        {"bin/test_worker.py::test_partial": 1.0},
    )
    monkeypatch.setattr(
        load_guard,
        "_SKIPS",
        {"bin/test_worker.py::test_partial": "worker-only skip"},
    )
    monkeypatch.setattr(
        load_guard,
        "report_skip_set",
        lambda *args, **kwargs: skip_writes.append((args, kwargs)) or [],
    )

    load_guard.pytest_sessionfinish(session, 0)

    assert not partial_report.exists()
    assert skip_writes == []
    assert session.exitstatus == 0


def test_slow_ordinary_test_and_module_are_both_rejected():
    report = load_guard.build_cost_report(
        {
            "bin/test_fast.py::test_a": 6.0,
            "bin/test_fast.py::test_b": 5.0,
        },
        declared_modules=set(),
        max_test_seconds=10.0,
        max_module_seconds=10.0,
    )

    assert report["violations"] == [{
        "kind": "module",
        "nodeid": "bin/test_fast.py",
        "seconds": 11.0,
        "limit_seconds": 10.0,
    }]


def test_single_test_budget_has_an_independent_failure_direction():
    report = load_guard.build_cost_report(
        {"bin/test_slow.py::test_one": 10.1},
        declared_modules=set(),
        max_test_seconds=10.0,
        max_module_seconds=45.0,
    )

    assert [v["kind"] for v in report["violations"]] == ["test"]


def test_declared_integration_module_is_reported_but_not_rejected():
    durations = {"bin/test_real_integration.py::test_e2e": 120.0}
    report = load_guard.build_cost_report(
        durations,
        declared_modules={"bin/test_real_integration.py"},
    )

    assert report["violations"] == []
    assert report["tests"][0]["declared_heavy"] is True
    assert report["modules"][0]["seconds"] == 120.0


def test_declaration_discovery_requires_tier_name_or_reasoned_marker(tmp_path):
    (tmp_path / "test_named_integration.py").write_text("def test_e2e(): pass\n")
    (tmp_path / "test_reasoned.py").write_text(
        "# sable-test-load: measured-slow -- exercises a real external clock\n"
    )
    (tmp_path / "test_empty_suppression.py").write_text(
        "# sable-test-load: measured-slow\n"
    )

    declared = load_guard.declared_heavy_modules(
        {
            "test_named_integration.py",
            "test_reasoned.py",
            "test_empty_suppression.py",
        },
        repo_root=Path(tmp_path),
    )

    assert declared == {"test_named_integration.py", "test_reasoned.py"}


def test_skip_set_delta_is_reported_not_just_counted():
    baseline = {
        "bin/test_a.py::test_still_skipped": "needs bd",
        "bin/test_b.py::test_now_running": "needs docker",
    }
    current = {
        "bin/test_a.py::test_still_skipped": "needs bd",
        "bin/test_c.py::test_newly_skipped": "missing fixture",
    }

    report = "\n".join(load_guard.format_skip_set_report(baseline, current))

    assert (
        "ADDED skip: bin/test_c.py::test_newly_skipped — missing fixture"
        in report
    )
    assert (
        "REMOVED skip: bin/test_b.py::test_now_running — needs docker"
        in report
    )
    assert "CURRENT skip: bin/test_a.py::test_still_skipped — needs bd" in report
    assert "CURRENT skip: bin/test_c.py::test_newly_skipped — missing fixture" in report

    unchanged = "\n".join(load_guard.format_skip_set_report(current, current))
    assert "skip set unchanged" in unchanged
    assert "ADDED skip:" not in unchanged
    assert "REMOVED skip:" not in unchanged


def test_expected_xfail_is_not_misreported_as_a_skip(monkeypatch):
    isolated_durations = {}
    isolated_skips = {}
    monkeypatch.setattr(load_guard, "_DURATIONS", isolated_durations)
    monkeypatch.setattr(load_guard, "_SKIPS", isolated_skips)
    report = SimpleNamespace(
        nodeid="bin/test_x.py::test_expected_failure",
        duration=0.01,
        skipped=True,
        wasxfail="known defect",
        longrepr=("bin/test_x.py", 10, "reason"),
    )

    load_guard.pytest_runtest_logreport(report)

    assert report.nodeid not in isolated_skips


def test_collection_level_import_skip_is_reported(monkeypatch):
    isolated_skips = {}
    monkeypatch.setattr(load_guard, "_SKIPS", isolated_skips)
    report = SimpleNamespace(
        nodeid="bin/test_optional_dependency.py",
        skipped=True,
        longrepr=(
            "bin/test_optional_dependency.py",
            3,
            "Skipped: optional dependency unavailable",
        ),
    )

    load_guard.pytest_collectreport(report)

    assert isolated_skips == {
        report.nodeid: "optional dependency unavailable",
    }


def test_primary_outcome_counts_always_include_pass_fail_and_skip():
    report = lambda nodeid, **extra: SimpleNamespace(nodeid=nodeid, **extra)
    stats = {
        "passed": [report("bin/test_x.py::test_pass")],
        "failed": [],
        "error": [],
        "skipped": [
            report("bin/test_x.py::test_skip"),
            report("bin/test_x.py::test_xfail", wasxfail="known defect"),
        ],
    }

    assert load_guard.primary_outcome_counts(stats) == {
        "passed": 1,
        "failed": 0,
        "skipped": 1,
    }


def test_successful_skip_report_becomes_the_next_runs_baseline(tmp_path):
    state = tmp_path / "pytest-skip-set.json"
    first = {"bin/test_a.py::test_a": "needs bd"}

    first_report = load_guard.report_skip_set(
        first, state_path=state, completed_successfully=True
    )

    assert "no previous successful full run" in "\n".join(first_report)
    assert json.loads(state.read_text())["skips"] == first

    second = {"bin/test_b.py::test_b": "needs docker"}
    second_report = "\n".join(
        load_guard.report_skip_set(
            second, state_path=state, completed_successfully=True
        )
    )
    assert "ADDED skip: bin/test_b.py::test_b — needs docker" in second_report
    assert "REMOVED skip: bin/test_a.py::test_a — needs bd" in second_report
    assert json.loads(state.read_text())["skips"] == second


def test_failed_run_reports_but_does_not_replace_the_last_green_skip_set(tmp_path):
    state = tmp_path / "pytest-skip-set.json"
    baseline = {"bin/test_a.py::test_a": "needs bd"}
    load_guard.report_skip_set(
        baseline, state_path=state, completed_successfully=True
    )

    report = "\n".join(
        load_guard.report_skip_set(
            {"bin/test_b.py::test_b": "needs docker"},
            state_path=state,
            completed_successfully=False,
        )
    )

    assert "baseline not replaced because this run was not successful" in report
    assert "ADDED skip:" not in report
    assert "REMOVED skip:" not in report
    assert json.loads(state.read_text())["skips"] == baseline


def test_non_object_skip_baseline_is_reported_and_recovered(tmp_path):
    state = tmp_path / "pytest-skip-set.json"
    state.write_text("[]\n")
    current = {"bin/test_a.py::test_a": "needs bd"}

    report = "\n".join(
        load_guard.report_skip_set(
            current,
            state_path=state,
            completed_successfully=True,
            run_started_ns=100,
        )
    )

    assert "baseline unreadable" in report
    assert json.loads(state.read_text())["skips"] == current


def test_older_concurrent_run_cannot_replace_a_newer_runs_baseline(tmp_path):
    state = tmp_path / "pytest-skip-set.json"
    newer = {"bin/test_new.py::test_new": "new environment guard"}
    older = {"bin/test_old.py::test_old": "old environment guard"}
    load_guard.report_skip_set(
        newer,
        state_path=state,
        completed_successfully=True,
        run_started_ns=200,
    )

    report = "\n".join(
        load_guard.report_skip_set(
            older,
            state_path=state,
            completed_successfully=True,
            run_started_ns=100,
        )
    )

    assert "newer successful full run finished first" in report
    assert json.loads(state.read_text())["skips"] == newer


def test_default_skip_state_is_repo_scoped_from_a_nested_pytest_root(monkeypatch):
    repo = Path(__file__).resolve().parent.parent
    monkeypatch.delenv("SABLE_PYTEST_SKIP_SET_STATE", raising=False)

    path = load_guard._skip_state_path(
        SimpleNamespace(rootpath=repo / "bin")
    )

    assert path == repo / ".claude" / "sable" / "state" / "pytest-skip-set.json"


def test_documented_and_authoritative_full_runs_enable_skip_identity_reporting():
    repo = Path(__file__).resolve().parent.parent
    docs = (repo / "CLAUDE.md").read_text()
    sealed = (repo / ".github" / "ci" / "run-sealed-verification.sh").read_text()
    snapshot = (repo / ".github" / "workflows" / "green-snapshot.yml").read_text()

    expected_flags = "-q -rs -p no:cacheprovider --sable-report-skip-set"
    assert docs.count(expected_flags) == 2
    assert expected_flags in sealed
    assert expected_flags in snapshot
