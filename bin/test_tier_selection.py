#!/usr/bin/env python3
"""Unit tests for bin/tier_selection.py (SABLE-cmar4.3).

Pure logic + an injected `collector` seam standing in for the real
pytest-testmon / pytest-impact collect-only subprocess calls (those are
exercised for real, with no mocking, in test_tier_selection_integration.py).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tier_selection as ts  # noqa: E402


# --- parse_collect_only_nodeids ----------------------------------------------

def test_parse_collect_only_nodeids_extracts_ids():
    output = (
        "bin/test_x.py::test_a\n"
        "bin/test_x.py::test_b\n"
        "bin/sub/test_y.py::TestC::test_d\n"
    )
    assert ts.parse_collect_only_nodeids(output) == [
        "bin/test_x.py::test_a",
        "bin/test_x.py::test_b",
        "bin/sub/test_y.py::TestC::test_d",
    ]


def test_parse_collect_only_nodeids_ignores_summary_and_blank_lines():
    output = (
        "\n"
        "bin/test_x.py::test_a\n"
        "\n"
        "3 tests collected in 0.02s\n"
    )
    assert ts.parse_collect_only_nodeids(output) == ["bin/test_x.py::test_a"]


def test_parse_collect_only_nodeids_empty_output():
    assert ts.parse_collect_only_nodeids("") == []
    assert ts.parse_collect_only_nodeids("no tests ran in 0.00s\n") == []


# --- build_impact_tier_plan --------------------------------------------------

def test_cache_miss_falls_back_to_full_run(tmp_path):
    # No .testmondata present at all -- the definitive cache-miss case.
    assert not ts.testmondata_path(tmp_path).exists()

    def collector_should_not_be_called(repo_root, extra_args):
        raise AssertionError("collector must not run on a cache miss")

    plan = ts.build_impact_tier_plan(tmp_path, collector=collector_should_not_be_called)

    assert plan.mode == "full"
    assert plan.argv == ["bin/", "-q", "-p", "no:cacheprovider"]
    assert "cache miss" in plan.reason


def test_cache_hit_unions_testmon_and_impact_changed_file_selections(tmp_path):
    ts.testmondata_path(tmp_path).write_text("{}")

    def fake_collector(repo_root, extra_args):
        if "--testmon" in extra_args:
            return ts.CollectResult(ids=["bin/test_mod.py::test_touches_changed_file"], returncode=0)
        assert any(a.startswith("--impact") for a in extra_args)
        return ts.CollectResult(ids=["bin/test_conftest_dependent.py::test_uses_fixture"], returncode=0)

    plan = ts.build_impact_tier_plan(tmp_path, collector=fake_collector)

    assert plan.mode == "selected"
    assert plan.argv == [
        "bin/test_conftest_dependent.py::test_uses_fixture",
        "bin/test_mod.py::test_touches_changed_file",
        "-q",
    ]
    assert "testmon=1" in plan.reason
    assert "impact=1" in plan.reason


def test_cache_hit_overlapping_selections_deduplicate(tmp_path):
    ts.testmondata_path(tmp_path).write_text("{}")

    def fake_collector(repo_root, extra_args):
        return ts.CollectResult(ids=["bin/test_mod.py::test_shared"], returncode=0)

    plan = ts.build_impact_tier_plan(tmp_path, collector=fake_collector)

    assert plan.mode == "selected"
    assert plan.argv == ["bin/test_mod.py::test_shared", "-q"]


def test_cache_hit_both_selectors_empty_returns_none_mode(tmp_path):
    ts.testmondata_path(tmp_path).write_text("{}")

    def empty_collector(repo_root, extra_args):
        return ts.CollectResult(ids=[], returncode=0)

    plan = ts.build_impact_tier_plan(tmp_path, collector=empty_collector)

    assert plan.mode == "none"
    assert plan.argv == []


# --- collector failure must fall back to FULL, never "none" (SABLE-cmar4.3 revise) --

def test_collector_usage_error_falls_back_to_full_run(tmp_path):
    # A missing/incompatible plugin looks like a pytest usage error (exit 4):
    # empty stdout, same as a legitimately-empty selection. Must NOT be read
    # as "nothing impacted" -- that would run zero tests and report success.
    ts.testmondata_path(tmp_path).write_text("{}")

    def failing_collector(repo_root, extra_args):
        return ts.CollectResult(ids=[], returncode=4)

    plan = ts.build_impact_tier_plan(tmp_path, collector=failing_collector)

    assert plan.mode == "full"
    assert plan.argv == ["bin/", "-q", "-p", "no:cacheprovider"]
    assert "exit 4" in plan.reason


def test_collector_exit5_still_means_none(tmp_path):
    # Regression guard against overcorrecting: pytest's own "no tests
    # collected" code (5) is a LEGITIMATE empty selection, not a failure.
    ts.testmondata_path(tmp_path).write_text("{}")

    def empty_collector(repo_root, extra_args):
        return ts.CollectResult(ids=[], returncode=5)

    plan = ts.build_impact_tier_plan(tmp_path, collector=empty_collector)

    assert plan.mode == "none"
    assert plan.argv == []


# --- run_impact_tier ----------------------------------------------------------

def test_run_impact_tier_none_mode_skips_subprocess(tmp_path, monkeypatch):
    ts.testmondata_path(tmp_path).write_text("{}")
    monkeypatch.setattr(ts, "build_impact_tier_plan", lambda *a, **k: ts.ImpactTierPlan("none", [], "nothing"))

    calls = []
    monkeypatch.setattr(ts.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    rc = ts.run_impact_tier(tmp_path)

    assert rc == 0
    assert calls == []


def test_run_impact_tier_full_mode_invokes_pytest_with_full_argv(tmp_path, monkeypatch):
    monkeypatch.setattr(
        ts, "build_impact_tier_plan",
        lambda *a, **k: ts.ImpactTierPlan("full", ["bin/", "-q"], "cache miss"),
    )

    captured = {}

    class FakeCompletedProcess:
        returncode = 3

    def fake_run(argv, cwd):
        captured["argv"] = argv
        captured["cwd"] = cwd
        return FakeCompletedProcess()

    monkeypatch.setattr(ts.subprocess, "run", fake_run)

    rc = ts.run_impact_tier(tmp_path)

    assert rc == 3
    assert captured["argv"] == [sys.executable, "-m", "pytest", "bin/", "-q"]
    assert captured["cwd"] == tmp_path


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
