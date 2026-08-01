#!/usr/bin/env python3
"""Unit tests for the bounded xdist benchmark sampler."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


TARGET = Path(__file__).with_name("sable-xdist-benchmark.py")
SPEC = importlib.util.spec_from_file_location("sable_xdist_benchmark", TARGET)
benchmark = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(benchmark)


def test_width_commands_are_bounded_and_loadscope_only(tmp_path):
    assert benchmark.width_args("serial") == []
    assert benchmark.width_args("2") == ["-n", "2", "--dist=loadscope"]
    assert benchmark.width_args("4") == ["-n", "4", "--dist=loadscope"]
    with pytest.raises(benchmark.BenchmarkError):
        benchmark.width_args("auto")

    command = benchmark.pytest_command(tmp_path, "4")
    assert "--dist=loadscope" in command
    assert "auto" not in command
    assert "--sable-report-skip-set" in command
    assert any(arg.startswith("--sable-test-cost-report=") for arg in command)
    assert any(arg.startswith("--junitxml=") for arg in command)


def test_collection_identity_is_exact_sorted_and_ignores_summary_noise():
    output = (
        "bin/test_b.py::test_z\n"
        "bin/test_a.py::test_a[param]\n"
        "\n2 tests collected in 0.01s\n"
    )
    assert benchmark.collection_identities(output) == [
        "bin/test_a.py::test_a[param]",
        "bin/test_b.py::test_z",
    ]


def test_junit_parser_distinguishes_primary_outcomes_and_xfail(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """<?xml version='1.0' encoding='utf-8'?>
<testsuites><testsuite>
  <testcase classname="bin.test_a" name="test_pass" />
  <testcase classname="bin.test_a" name="test_fail"><failure /></testcase>
  <testcase classname="bin.test_a" name="test_error"><error /></testcase>
  <testcase classname="bin.test_a" name="test_skip"><skipped type="pytest.skip" /></testcase>
  <testcase classname="bin.test_a" name="test_xfail"><skipped type="pytest.xfail" /></testcase>
</testsuite></testsuites>
"""
    )

    assert benchmark.parse_junit(junit) == {
        "bin.test_a::test_error": "error",
        "bin.test_a::test_fail": "failed",
        "bin.test_a::test_pass": "passed",
        "bin.test_a::test_skip": "skipped",
        "bin.test_a::test_xfail": "xfail",
    }


def _record(rep=1, *, wall=100.0, outcome="same", collection="same",
            rc=0, timed_out=False, test_p95=2.0, max_test=4.0,
            module_p95=8.0, max_module=10.0):
    return {
        "repetition": rep,
        "collection_returncode": 0,
        "returncode": rc,
        "timed_out": timed_out,
        "artifact_error": "",
        "violation_count": 0,
        "source_moved": False,
        "dirty_after": "",
        "collection_digest": collection,
        "outcome_digest": outcome,
        "outcome_counts": {"passed": 10, "failed": 0, "error": 0,
                           "skipped": 1, "xfail": 0},
        "wall_seconds": wall,
        "average_cpu_cores": 1.0,
        "load_1m_p95": 2.0,
        "test_p95_seconds": test_p95,
        "max_test_seconds": max_test,
        "module_p95_seconds": module_p95,
        "max_module_seconds": max_module,
    }


def test_stage_requires_repeated_identity_equivalence_and_improvement():
    serial_records = [_record(1, wall=100), _record(2, wall=102)]
    ok, reasons, serial = benchmark.evaluate_stage(
        "serial", serial_records, None, None,
        min_improvement=.10, tail_ratio=1.5, tail_absolute_seconds=5,
    )
    assert ok and reasons == []

    parallel = [_record(1, wall=70), _record(2, wall=72)]
    ok, reasons, summary = benchmark.evaluate_stage(
        "2", parallel, serial_records[0], serial,
        min_improvement=.10, tail_ratio=1.5, tail_absolute_seconds=5,
    )
    assert ok and reasons == []
    assert summary["wall_median_seconds"] == 71

    drifted = [_record(1, wall=70), _record(2, wall=71, outcome="different")]
    ok, reasons, _ = benchmark.evaluate_stage(
        "2", drifted, serial_records[0], serial,
        min_improvement=.10, tail_ratio=1.5, tail_absolute_seconds=5,
    )
    assert not ok
    assert any("outcome identity diverged" in reason for reason in reasons)


def test_stage_stops_on_material_tail_regression_but_ignores_tiny_noise():
    previous = benchmark.width_summary(
        "serial", [_record(1), _record(2)],
    )
    bad = [_record(1, wall=60, max_test=20), _record(2, wall=61, max_test=22)]
    ok, reasons, _ = benchmark.evaluate_stage(
        "2", bad, _record(), previous,
        min_improvement=.10, tail_ratio=1.5, tail_absolute_seconds=5,
    )
    assert not ok
    assert any("max_test_seconds" in reason for reason in reasons)

    tiny = [_record(1, wall=60, test_p95=3.2), _record(2, wall=61, test_p95=3.2)]
    ok, reasons, _ = benchmark.evaluate_stage(
        "2", tiny, _record(), previous,
        min_improvement=.10, tail_ratio=1.5, tail_absolute_seconds=5,
    )
    assert ok, reasons


def test_broad_seat_is_nonblocking_and_names_the_holder(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@sable.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f").write_text("x\n")
    subprocess.run(["git", "add", "f"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)

    with benchmark.broad_seat(repo):
        with pytest.raises(benchmark.BenchmarkError, match="seat is busy"):
            with benchmark.broad_seat(repo):
                pass


def test_artifact_root_refuses_paths_outside_git_common_dir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=repo, check=True)
    with pytest.raises(benchmark.BenchmarkError, match="git-common-dir"):
        benchmark.artifact_root(repo, str(tmp_path / "outside"), "a" * 40)


def test_summary_json_values_are_finite_and_serializable():
    summary = benchmark.width_summary("serial", [_record(1), _record(2)])
    json.dumps(summary, allow_nan=False)


def test_run_once_records_real_process_collection_outcomes_and_costs(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@sable.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f").write_text("x\n")
    subprocess.run(["git", "add", "f"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    head = benchmark.git_head(repo)
    root = repo / ".git" / "artifacts"
    root.mkdir()

    monkeypatch.setattr(
        benchmark,
        "collection_command",
        lambda width: [
            sys.executable, "-c", "print('bin/test_x.py::test_x')",
        ],
    )

    def fake_pytest_command(run_dir, width):
        cost = {
            "tests": [{"nodeid": "bin/test_x.py::test_x", "seconds": 0.1}],
            "modules": [{"module": "bin/test_x.py", "seconds": 0.1}],
            "violations": [],
        }
        junit = (
            "<testsuites><testsuite><testcase classname='bin.test_x' "
            "name='test_x'/></testsuite></testsuites>"
        )
        script = (
            "from pathlib import Path; "
            f"Path({str(run_dir / 'cost.json')!r}).write_text({json.dumps(cost)!r}); "
            f"Path({str(run_dir / 'junit.xml')!r}).write_text({junit!r})"
        )
        return [sys.executable, "-c", script]

    monkeypatch.setattr(benchmark, "pytest_command", fake_pytest_command)

    record = benchmark.run_once(repo, root, "serial", 1, 10, head)

    assert record["returncode"] == 0
    assert record["collection_identities"] == ["bin/test_x.py::test_x"]
    assert record["outcomes"] == {"bin.test_x::test_x": "passed"}
    assert record["test_rows"] == 1
    assert record["module_rows"] == 1
    assert record["dirty_after"] == ""
