#!/usr/bin/env python3
"""Unit contracts for the scoped same-host contention plant."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import sable_test_contention_lib as plant

CLI = Path(__file__).resolve().parent / "sable-test-contention"


def _tiny_catalog(tmp_path: Path) -> dict[str, plant.Workload]:
    script = (
        "print('1 passed in 0.01s', flush=True)"
    )
    command = (sys.executable, "-c", script)
    return {
        name: plant.Workload(
            name=name,
            command=command,
            resource_class=(
                "host-heavy" if name in {"tmux", "real-bd"} else "ordinary"
            ),
            required_tools=(),
            expected_passed=1,
            timeout_seconds=2,
            boundary=f"fixture {name}",
        )
        for name in ("cheap-unit", "subprocess-heavy", "tmux", "real-bd")
    }


def test_catalog_is_scoped_to_one_module_and_never_the_sealed_suite():
    catalog = plant.scenario_catalog(python_executable="python")
    plant.validate_catalog(catalog)

    for workload in catalog.values():
        paths = [
            token for token in workload.command
            if token.startswith("bin/test_") and token.endswith(".py")
        ]
        assert len(paths) == 1
        assert "bin/" not in workload.command
        assert ".github/ci/shell-run-set.sh" not in workload.command


def test_cli_exposes_only_the_scoped_scenario_surface():
    result = subprocess.run(
        [sys.executable, str(CLI), "--help"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "--scenario" in result.stdout
    assert "mixed" in result.stdout
    assert "sealed" in result.stdout


def test_catalog_rejects_a_broad_pytest_command():
    catalog = plant.scenario_catalog(python_executable="python")
    broad = dict(catalog)
    broad["cheap-unit"] = plant.Workload(
        name="cheap-unit",
        command=("python", "-m", "pytest", "bin/"),
        resource_class="ordinary",
        required_tools=(),
        expected_passed=1,
        timeout_seconds=1,
        boundary="invalid fixture",
    )
    with pytest.raises(plant.PlantError, match="one scoped"):
        plant.validate_catalog(broad)


def test_mixed_fifteen_is_representative_and_deterministic():
    assignments = plant.assignments_for("mixed", 15)
    names = [assignment.workload.name for assignment in assignments]
    assert names == list(plant._MIXED_PATTERN)
    assert {name: names.count(name) for name in set(names)} == {
        "cheap-unit": 6,
        "subprocess-heavy": 4,
        "tmux": 3,
        "real-bd": 2,
    }


def test_time_record_and_pytest_summary_preserve_original_status():
    resources = plant.parse_time_record(
        "sable-time-v1\t3.14\t1.2\t0.3\t42000\t17\t8\t2\t0\t90\t4\t12\n"
    )
    assert resources["elapsed_seconds"] == 3.14
    assert resources["exit_code"] == 17
    assert resources["max_rss_kib"] == 42000

    summary = plant.parse_pytest_summary("7 passed in 2.50s\n")
    assert summary == {
        "passed": 7,
        "failed": 0,
        "skipped": 0,
        "errors": 0,
        "xfailed": 0,
        "xpassed": 0,
        "warnings": 0,
        "seconds": 2.5,
    }
    failed_first = plant.parse_pytest_summary(
        "1 failed, 6 passed, 2 warnings in 3.25s\n"
    )
    assert failed_first["failed"] == 1
    assert failed_first["passed"] == 6
    assert failed_first["warnings"] == 2


@pytest.mark.parametrize(
    "record",
    [
        "",
        "garbage",
        "sable-time-v1\t1\t2\n",
        "sable-time-v1\tbad\t1\t1\t1\t0\t0\t0\t0\t0\t0\t0\n",
    ],
)
def test_malformed_resource_records_fail_closed(record):
    with pytest.raises(plant.PlantError):
        plant.parse_time_record(record)


def test_collision_events_remain_attributable_to_log_lines():
    text = (
        "ordinary output\n"
        "database is locked while opening Dolt working set\n"
        "error connecting to /tmp/tmux-1/socket (Address already in use)\n"
    )
    events = plant.detect_collisions(text)
    assert [(event["kind"], event["line"]) for event in events] == [
        ("dolt-lock", 2),
        ("tmux-socket", 3),
    ]


def test_shared_resource_delta_is_bounded_but_preserves_new_names():
    git = {"status_porcelain_v1": []}
    before = {"paths": {"/tmp/example": ["old", "same"]}, "git": git}
    after = {"paths": {"/tmp/example": ["same", "new"]}, "git": git}

    delta = plant.shared_resource_delta(before, after)

    assert delta["paths"]["/tmp/example"]["before_count"] == 2
    assert delta["paths"]["/tmp/example"]["after_count"] == 2
    assert delta["paths"]["/tmp/example"]["added"] == ["new"]
    assert delta["paths"]["/tmp/example"]["removed"] == ["old"]
    assert "before" not in delta["paths"]["/tmp/example"]


def test_host_summary_reports_pressure_and_capacity():
    pressure0 = {
        key: {
            "some": {"avg10": 0.0, "total": 100},
            "full": {"avg10": 0.0, "total": 10},
        }
        for key in ("cpu", "memory", "io")
    }
    pressure1 = {
        key: {
            "some": {"avg10": 1.0, "total": 500},
            "full": {"avg10": 0.5, "total": 30},
        }
        for key in ("cpu", "memory", "io")
    }
    samples = [
        plant._HostSample(
            offset_seconds=0,
            cpu_busy_percent=None,
            ctxt_delta=None,
            processes_delta=None,
            procs_running=1,
            procs_blocked=0,
            mem_total_bytes=1000,
            mem_available_bytes=800,
            load1=0.1,
            load5=0.1,
            load15=0.1,
            pressure=pressure0,
        ),
        plant._HostSample(
            offset_seconds=1,
            cpu_busy_percent=75,
            ctxt_delta=20,
            processes_delta=4,
            procs_running=8,
            procs_blocked=2,
            mem_total_bytes=1000,
            mem_available_bytes=300,
            load1=4.0,
            load5=1.0,
            load15=0.2,
            pressure=pressure1,
        ),
    ]
    summary = plant.summarize_host_samples(samples)
    assert summary["cpu_busy_percent_peak"] == 75
    assert summary["memory_used_bytes_peak"] == 700
    assert summary["pressure_delta"]["io"]["full_total_usec"] == 20


def test_parent_cleanup_targets_the_recorded_process_identity():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
    )
    try:
        fields = Path(f"/proc/{process.pid}/stat").read_text().rsplit(
            ")", 1
        )[1].split()
        record = {
            "pid": process.pid,
            "starttime_ticks": int(fields[19]),
        }

        cleanup = plant._cleanup_process_records(
            [record], term_grace_seconds=0.2
        )

        process.wait(timeout=2)
        assert cleanup["attempted_pids"] == [process.pid]
        assert cleanup["remaining_pids"] == []
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_small_green_plant_captures_every_worker_independently(tmp_path):
    report_path = tmp_path / "report.json"
    artifacts = tmp_path / "artifacts"
    report = plant.run_plant(
        repo_root=Path(__file__).resolve().parent.parent,
        scenario="cheap-unit",
        workers=3,
        parallelism=3,
        report_path=report_path,
        artifacts_dir=artifacts,
        sample_interval=0.01,
        catalog=_tiny_catalog(tmp_path),
    )

    assert report["verdict"]["status"] == "passed"
    assert report["summary"]["passed"] == 3
    assert [row["index"] for row in report["workers"]] == [1, 2, 3]
    assert all(row["log_sha256"] for row in report["workers"])
    assert report["summary"]["resource_records"] == 3
    assert json.loads(report_path.read_text())["experiment_id"] == report["experiment_id"]


def test_planted_failure_names_the_worker_and_propagates_red(tmp_path):
    report = plant.run_plant(
        repo_root=Path(__file__).resolve().parent.parent,
        scenario="cheap-unit",
        workers=3,
        parallelism=3,
        report_path=tmp_path / "failure.json",
        artifacts_dir=tmp_path / "failure-artifacts",
        sample_interval=0.01,
        plant_failure=2,
        catalog=_tiny_catalog(tmp_path),
    )

    assert report["verdict"]["status"] == "failed"
    failed = report["workers"][1]
    assert failed["index"] == 2
    assert failed["exit_code"] == 17
    assert failed["status"] == "failed"
    assert failed["planted"] == "failure"
    assert report["summary"]["passed"] == 2


def test_planted_timeout_is_killed_and_missing_time_record_is_not_green(tmp_path):
    report = plant.run_plant(
        repo_root=Path(__file__).resolve().parent.parent,
        scenario="cheap-unit",
        workers=1,
        parallelism=1,
        report_path=tmp_path / "timeout.json",
        artifacts_dir=tmp_path / "timeout-artifacts",
        sample_interval=0.01,
        plant_timeout=1,
        timeout_override=0.05,
        catalog=_tiny_catalog(tmp_path),
    )

    worker = report["workers"][0]
    assert report["verdict"]["status"] == "failed"
    assert worker["status"] == "timeout"
    assert worker["timed_out"] is True
    assert worker["resources"] is None
    assert worker["malformed_reasons"]
    assert report["summary"]["aggregate_user_seconds"] is None
    assert report["summary"]["aggregate_system_seconds"] is None


def test_host_heavy_slot_does_not_block_an_ordinary_worker():
    catalog = plant.scenario_catalog(python_executable="python")
    heavy = plant.Assignment(1, catalog["tmux"])
    other_heavy = plant.Assignment(2, catalog["real-bd"])
    ordinary = plant.Assignment(3, catalog["cheap-unit"])
    running = [
        plant._RunningWorker(
            assignment=heavy,
            process=None,
            command=[],
            launch_monotonic=0,
            launch_offset=0,
            timeout_seconds=1,
            log_path=Path("log"),
            resource_path=Path("resource"),
            log_handle=None,
            planted=None,
        )
    ]

    assert not plant._can_launch(
        other_heavy, running, parallelism=3, heavy_slots=1
    )
    assert plant._can_launch(
        ordinary, running, parallelism=3, heavy_slots=1
    )


def test_existing_report_is_never_overwritten(tmp_path):
    report = tmp_path / "existing.json"
    report.write_text("keep me")
    with pytest.raises(plant.PlantError, match="already exists"):
        plant.run_plant(
            repo_root=Path(__file__).resolve().parent.parent,
            scenario="cheap-unit",
            workers=1,
            parallelism=1,
            report_path=report,
            artifacts_dir=tmp_path / "artifacts",
            catalog=_tiny_catalog(tmp_path),
        )
    assert report.read_text() == "keep me"
