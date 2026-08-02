#!/usr/bin/env python3
"""Unit tests for the bounded xdist benchmark sampler."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
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


def test_overlap_commands_pin_n2_python_and_the_serial_shell_allowlist(tmp_path):
    python, shell = benchmark.overlap_commands(tmp_path)

    assert python[-3:] == ["-n", "2", "--dist=loadscope"]
    assert "auto" not in python
    assert "4" not in python[-3:]
    assert shell == [
        "bash", ".github/ci/shell-run-set.sh", "--profile",
        str(tmp_path / "shell.tsv"),
    ]


def test_shell_profile_parser_requires_the_exact_catalog_and_keeps_fail_status(tmp_path):
    profile = tmp_path / "shell.tsv"
    profile.write_text(
        "suite\tstatus\tseconds\n"
        "test-a.sh\tpass\t1.25\n"
        "test-b.sh\tfail:17\t2.50\n"
    )

    assert benchmark.parse_shell_profile(
        profile, ("test-a.sh", "test-b.sh"),
    ) == {
        "test-a.sh": {"status": "pass", "seconds": 1.25},
        "test-b.sh": {"status": "fail:17", "seconds": 2.5},
    }
    with pytest.raises(benchmark.BenchmarkError, match="missing.*test-c.sh"):
        benchmark.parse_shell_profile(
            profile, ("test-a.sh", "test-b.sh", "test-c.sh"),
        )


def _overlap_record(rep=1, *, wall=500.0, python_digest="python",
                    shell_digest="shell", python_rc=0, shell_rc=0,
                    timed_out=False, violation_count=0,
                    max_module=120.0):
    record = {
        **_record(
            rep, wall=wall, outcome=python_digest,
            max_module=max_module,
        ),
        "python_returncode": python_rc,
        "shell_returncode": shell_rc,
        "timed_out": timed_out,
        "violation_count": violation_count,
        "shell_digest": shell_digest,
        "shell_all_passed": shell_rc == 0,
    }
    record["returncode"] = 124 if timed_out else (
        0 if python_rc == shell_rc == 0 else 1
    )
    return record


def test_overlap_evaluation_requires_exact_repetition_and_sub_600_p95():
    reference = _record(1, wall=515.0, outcome="python", max_module=140.0)
    records = [
        _overlap_record(1, wall=540.0),
        _overlap_record(2, wall=550.0),
    ]

    ok, reasons, summary = benchmark.evaluate_overlap(
        records, reference, broad_target_seconds=600.0,
        tail_ratio=1.5, tail_absolute_seconds=5.0,
    )
    assert ok, reasons
    assert summary["wall_p95_seconds"] == 550.0

    too_slow = [*records[:1], _overlap_record(2, wall=601.0)]
    ok, reasons, _ = benchmark.evaluate_overlap(
        too_slow, reference, broad_target_seconds=600.0,
        tail_ratio=1.5, tail_absolute_seconds=5.0,
    )
    assert not ok
    assert any("600" in reason and "p95" in reason for reason in reasons)

    drifted = [*records[:1], _overlap_record(2, shell_digest="different")]
    ok, reasons, _ = benchmark.evaluate_overlap(
        drifted, reference, broad_target_seconds=600.0,
        tail_ratio=1.5, tail_absolute_seconds=5.0,
    )
    assert not ok
    assert any("shell identity" in reason for reason in reasons)


def test_overlap_once_really_starts_both_lanes_and_records_both_verdicts(
        tmp_path, monkeypatch):
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
    python_start = root / "python.start"
    shell_start = root / "shell.start"

    monkeypatch.setattr(
        benchmark, "collection_command",
        lambda width: [sys.executable, "-c", "print('bin/test_x.py::test_x')"],
    )
    monkeypatch.setattr(benchmark, "shell_catalog", lambda _repo: ("test-x.sh",))

    def writer_script(run_dir, lane):
        start_path = python_start if lane == "python" else shell_start
        prefix = (
            "from pathlib import Path; import os,time; "
            f"p=Path({str(start_path)!r}); "
            "p.write_text(f'{os.getpid()} {time.monotonic()}\\n'); "
            "time.sleep(.25); "
        )
        if lane == "python":
            cost = {
                "tests": [{"nodeid": "bin/test_x.py::test_x", "seconds": .1}],
                "modules": [{"module": "bin/test_x.py", "seconds": .1}],
                "violations": [],
            }
            junit = (
                "<testsuites><testsuite><testcase classname='bin.test_x' "
                "name='test_x'/></testsuite></testsuites>"
            )
            prefix += (
                f"Path({str(run_dir / 'cost.json')!r}).write_text({json.dumps(cost)!r}); "
                f"Path({str(run_dir / 'junit.xml')!r}).write_text({junit!r})"
            )
        else:
            prefix += (
                f"Path({str(run_dir / 'shell.tsv')!r}).write_text("
                "'suite\\tstatus\\tseconds\\ntest-x.sh\\tpass\\t0.25\\n')"
            )
        return [sys.executable, "-c", prefix]

    monkeypatch.setattr(
        benchmark, "overlap_commands",
        lambda run_dir: (
            writer_script(run_dir, "python"),
            writer_script(run_dir, "shell"),
        ),
    )

    record = benchmark.run_overlap_once(repo, root, 1, 10, head)

    stamps = [
        float(python_start.read_text().split()[1]),
        float(shell_start.read_text().split()[1]),
    ]
    assert len(stamps) == 2
    assert max(stamps) - min(stamps) < .15
    assert record["wall_seconds"] < .5
    assert record["python_returncode"] == 0
    assert record["shell_returncode"] == 0
    assert record["outcome_counts"]["passed"] == 1
    assert record["shell_all_passed"]
    assert record["dirty_after"] == ""


def test_overlap_timeout_reaps_both_process_groups(tmp_path, monkeypatch):
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
    python_pid = root / "python.pid"
    shell_pid = root / "shell.pid"

    monkeypatch.setattr(
        benchmark, "collection_command",
        lambda width: [sys.executable, "-c", "print('bin/test_x.py::test_x')"],
    )
    monkeypatch.setattr(benchmark, "shell_catalog", lambda _repo: ("test-x.sh",))
    def sleeper(pid_path):
        return (
            "from pathlib import Path; import os,time; "
            f"Path({str(pid_path)!r}).write_text(str(os.getpid())); "
            "time.sleep(30)"
        )
    monkeypatch.setattr(
        benchmark, "overlap_commands",
        lambda _run_dir: (
            [sys.executable, "-c", sleeper(python_pid)],
            [sys.executable, "-c", sleeper(shell_pid)],
        ),
    )

    record = benchmark.run_overlap_once(repo, root, 1, .2, head)

    assert record["timed_out"]
    assert record["python_returncode"] == 124
    assert record["shell_returncode"] == 124
    for raw in (python_pid.read_text(), shell_pid.read_text()):
        with pytest.raises(ProcessLookupError):
            os.kill(int(raw), 0)


def test_overlap_experiment_runs_one_reference_then_repeated_candidate(tmp_path, monkeypatch):
    reference = _record(1, wall=510.0, outcome="python", max_module=140.0)
    overlap = {
        1: _overlap_record(1, wall=540.0),
        2: _overlap_record(2, wall=550.0),
    }
    calls = []
    monkeypatch.setattr(benchmark, "_require_clean", lambda _repo: "a" * 40)
    monkeypatch.setattr(benchmark.importlib.util, "find_spec", lambda _name: object())

    def fake_reference(_repo, _root, width, repetition, _timeout, _head):
        calls.append(("reference", width, repetition))
        return reference

    def fake_overlap(_repo, _root, repetition, _timeout, _head):
        calls.append(("overlap", repetition))
        return overlap[repetition]

    monkeypatch.setattr(benchmark, "run_once", fake_reference)
    monkeypatch.setattr(benchmark, "run_overlap_once", fake_overlap)
    monkeypatch.setattr(benchmark, "environment_record", lambda: {"test": True})

    rc, result = benchmark.run_overlap_experiment(
        tmp_path, tmp_path, repetitions=2, timeout_seconds=1000,
        tail_ratio=1.5, tail_absolute_seconds=5,
        broad_target_seconds=600,
    )

    assert rc == 0
    assert result["verdict"] == "GO"
    assert result["overlap"]["wall_p95_seconds"] == 550.0
    assert calls == [("reference", "2", 1), ("overlap", 1), ("overlap", 2)]
    assert json.loads((tmp_path / "summary.json").read_text())["verdict"] == "GO"


def test_overlap_experiment_stops_after_the_first_red_lane(tmp_path, monkeypatch):
    reference = _record(1, wall=510.0, outcome="python", max_module=140.0)
    calls = []
    monkeypatch.setattr(benchmark, "_require_clean", lambda _repo: "a" * 40)
    monkeypatch.setattr(benchmark.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(benchmark, "run_once", lambda *_a, **_k: reference)

    def red_overlap(_repo, _root, repetition, _timeout, _head):
        calls.append(repetition)
        return _overlap_record(
            repetition, python_digest="python", shell_rc=17,
        )

    monkeypatch.setattr(benchmark, "run_overlap_once", red_overlap)
    monkeypatch.setattr(benchmark, "environment_record", lambda: {"test": True})

    rc, result = benchmark.run_overlap_experiment(
        tmp_path, tmp_path, repetitions=3, timeout_seconds=1000,
        tail_ratio=1.5, tail_absolute_seconds=5,
        broad_target_seconds=600,
    )

    assert rc == 3
    assert result["verdict"] == "NO-GO"
    assert calls == [1]
    assert any("shell lane rc=17" in reason for reason in result["reasons"])
