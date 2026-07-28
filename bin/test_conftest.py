"""Unit tests for the SABLE pytest load-budget adapter."""

from pathlib import Path

import conftest as load_guard


def test_new_session_discards_prior_in_process_measurements(monkeypatch):
    # Replace the map rather than clearing the outer pytest session's live
    # plugin state while this unit test itself is being measured.
    isolated = {"bin/test_old.py::test_old": 123.0}
    monkeypatch.setattr(load_guard, "_DURATIONS", isolated)

    load_guard.pytest_sessionstart(None)

    assert isolated == {}


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
