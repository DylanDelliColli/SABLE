#!/usr/bin/env python3
"""Integration tests for bin/tier_selection.py (SABLE-cmar4.3).

Real temporary git repo, real pytest-testmon and pytest-impact subprocess
runs via tier_selection.build_impact_tier_plan's default (real) collector —
no mocked pytest. Self-skips (SABLE-59zu clean-room contract: missing dep is
a skip, never a false-fail/false-pass) when either plugin isn't importable in
this interpreter.
"""
# sable-test-load: nested-runner -- defining E2E exercises real pytest-testmon selection and fallback
import importlib.util
import os
import signal
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from textwrap import dedent

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tier_selection as ts  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("testmon") is None
    or importlib.util.find_spec("pytest_impact") is None,
    reason="pytest-testmon / pytest-impact not installed",
)


def _run(cwd, *args):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def fixture_repo(tmp_path):
    repo = tmp_path / "repo"
    bin_dir = repo / "bin"
    bin_dir.mkdir(parents=True)

    (bin_dir / "conftest.py").write_text(
        dedent(
            """\
            import pytest

            @pytest.fixture
            def shared_value():
                return 1
            """
        )
    )
    (bin_dir / "module_a.py").write_text(
        dedent(
            """\
            def compute():
                return 41
            """
        )
    )
    (bin_dir / "test_module_a.py").write_text(
        dedent(
            """\
            from module_a import compute

            def test_module_a_computes(shared_value):
                assert compute() + shared_value == 42
            """
        )
    )
    (bin_dir / "test_module_b.py").write_text(
        dedent(
            """\
            def test_module_b_uses_fixture(shared_value):
                assert shared_value == 1
            """
        )
    )

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "baseline")

    # Baseline testmon run: builds .testmondata. First run has no prior data,
    # so it always runs everything -- testmon's own conservative cold start,
    # the same behavior build_impact_tier_plan falls back to on cache miss.
    result = _run(repo, "bin/", "-q", "--testmon")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ts.testmondata_path(repo).exists()

    return repo


def test_changing_one_module_selects_only_its_dependent_test(fixture_repo):
    # A comment-only edit doesn't change testmon's per-line fingerprint (it
    # tracks executed code, not raw text) -- this must be a real content
    # change to be observable as "changed" by either selector.
    (fixture_repo / "bin" / "module_a.py").write_text(
        dedent(
            """\
            def compute():
                return 41 + 0
            """
        )
    )

    plan = ts.build_impact_tier_plan(fixture_repo)

    assert plan.mode == "selected"
    assert plan.argv[:-1] == ["bin/test_module_a.py::test_module_a_computes"]


def test_changing_conftest_selects_broadly(fixture_repo):
    (fixture_repo / "bin" / "conftest.py").write_text(
        dedent(
            """\
            import pytest

            @pytest.fixture
            def shared_value():
                return 1 + 0
            """
        )
    )

    plan = ts.build_impact_tier_plan(fixture_repo)

    assert plan.mode == "selected"
    assert set(plan.argv[:-1]) == {
        "bin/test_module_a.py::test_module_a_computes",
        "bin/test_module_b.py::test_module_b_uses_fixture",
    }


def test_no_changes_selects_nothing(fixture_repo):
    plan = ts.build_impact_tier_plan(fixture_repo)

    assert plan.mode == "none"
    assert plan.argv == []


def test_missing_plugin_falls_back_to_full_run(fixture_repo):
    # Real pytest subprocess, no mocking: an unrecognized selector flag
    # reproduces exactly what a missing/incompatible pytest-testmon or
    # pytest-impact plugin looks like on an ephemeral CI runner -- a usage
    # error (exit 4) with empty stdout, indistinguishable from "nothing
    # impacted" unless the returncode is checked (SABLE-cmar4.3 revise).
    def broken_collector(repo_root, extra_args):
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest", "bin/", "--collect-only", "-q",
                "--impact-nonexistent-flag", *extra_args,
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 4, result.stdout + result.stderr
        return ts.CollectResult(ids=ts.parse_collect_only_nodeids(result.stdout), returncode=result.returncode)

    plan = ts.build_impact_tier_plan(fixture_repo, collector=broken_collector)

    assert plan.mode == "full"
    assert "exit 4" in plan.reason

    result = subprocess.run(
        [sys.executable, "-m", "pytest", *plan.argv],
        cwd=fixture_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


def test_missing_testmondata_falls_back_to_full_run_for_real_repo(fixture_repo):
    ts.testmondata_path(fixture_repo).unlink()

    plan = ts.build_impact_tier_plan(fixture_repo)

    assert plan.mode == "full"
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *plan.argv],
        cwd=fixture_repo,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 passed" in result.stdout


# --- the extensionless-file crash: reachable by EXECUTION, not by collection -
# (SABLE-jd5fj.19)
#
# The invariant "--testmon only ever reaches a --collect-only invocation" is
# asserted structurally in test_tier_selection.py. This is the other half:
# evidence that the invariant is worth having, i.e. that an EXECUTING --testmon
# run on a corpus shaped like this repo's really does crash, while the tier's
# collect-only call on the SAME corpus does not. The two differ in exactly one
# flag, which is the whole point -- an A/B this tight is only possible on a
# fixture small enough to hold both legs.
#
# POSITIVE CONTROL (SABLE-xhrt0): test_positive_control_* below asserts the
# fixture DOES crash under an executing --testmon run. Without it the
# collect-only leg's clean result would be worth nothing -- a corpus incapable
# of crashing proves nothing about --collect-only.
#
# SCOPE (optimus, 2026-07-26): synthetic tmp repo ONLY. The positive control
# deliberately provokes a real INTERNALERROR, so it must never be pointed at
# this repo's real bin/ nor be allowed to write the shared checkout's
# .testmondata. Everything below runs against tmp_path with cwd=tmp_path, one
# test file, ~0.1s per leg.

_EXTENSIONLESS_CRASH_MARKERS = ("INTERNALERROR", "testmon_core.py",
                                "IndexError: list index out of range")


@pytest.fixture
def extensionless_repo(tmp_path):
    """A minimal repo whose bin/ holds ONE extensionless python executable that
    a test loads via SourceFileLoader -- the shape of this repo's own bin/
    (sable-merge-gate, sable-tmux, ... loaded in-process by bin/test_*.py), at
    the smallest size that still exhibits the defect."""
    repo = tmp_path / "repo"
    bin_dir = repo / "bin"
    bin_dir.mkdir(parents=True)

    (bin_dir / "sable-toy").write_text(
        dedent(
            """\
            #!/usr/bin/env python3
            def answer():
                return 42
            """
        )
    )
    (bin_dir / "test_toy.py").write_text(
        dedent(
            """\
            import importlib.machinery
            import importlib.util
            from pathlib import Path

            def _load():
                path = Path(__file__).resolve().parent / "sable-toy"
                loader = importlib.machinery.SourceFileLoader("sable_toy", str(path))
                spec = importlib.util.spec_from_loader(loader.name, loader)
                mod = importlib.util.module_from_spec(spec)
                loader.exec_module(mod)
                return mod

            def test_toy_answer():
                assert _load().answer() == 42
            """
        )
    )

    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _executing_testmon_run(repo):
    """The cache-warm shape: a real, test-EXECUTING --testmon-noselect run.
    Also the only thing that produces genuine coverage fingerprints, so it
    doubles as this fixture's warm-up."""
    return _run(repo, "bin/", "-q", "-p", "no:cacheprovider", "--testmon-noselect")


def test_positive_control_executing_testmon_run_crashes_on_this_fixture(extensionless_repo):
    result = _executing_testmon_run(extensionless_repo)
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    for marker in _EXTENSIONLESS_CRASH_MARKERS:
        assert marker in output, (
            f"{marker!r} absent -- this fixture can no longer exhibit the crash, so "
            f"the collect-only test below has stopped proving anything:\n{output}")
    # The crash fires from a per-test-EXECUTION hook, so it lands AFTER the test
    # itself passed -- which is exactly what makes it read as an innocent RED.
    assert "1 passed" in output, output
    assert "pytest_runtest_logreport" in output, output


def test_cache_warm_tolerates_known_crash_on_minimal_fixture(extensionless_repo, capfd):
    rc = ts.run_cache_warm(extensionless_repo)
    output = "".join(capfd.readouterr())

    assert rc == 0, output
    for marker in _EXTENSIONLESS_CRASH_MARKERS:
        assert marker in output
    assert "KNOWN pytest-testmon extensionless-file crash tolerated" in output


def test_real_cache_warm_cli_reports_signal_death(tmp_path):
    """The real CLI must translate a killed nested pytest into a legible
    signal-specific failure, not Python's opaque negative-SystemExit status."""
    repo = tmp_path / "repo"
    bin_dir = repo / "bin"
    bin_dir.mkdir(parents=True)
    shutil.copy2(Path(ts.__file__), bin_dir / "tier_selection.py")
    child_pid_file = repo / "pytest-child.pid"
    (bin_dir / "test_slow.py").write_text(
        dedent(
            """\
            import os
            import time
            from pathlib import Path

            def test_slow():
                Path(__file__).resolve().parents[1].joinpath(
                    "pytest-child.pid"
                ).write_text(str(os.getpid()))
                time.sleep(30)
            """
        )
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "baseline")

    proc = subprocess.Popen(
        [sys.executable, str(bin_dir / "tier_selection.py"), "--cache-warm"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not child_pid_file.exists() and time.monotonic() < deadline:
            if proc.poll() is not None:
                stdout, stderr = proc.communicate()
                pytest.fail(
                    "cache-warm wrapper exited before its nested pytest ran:\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            time.sleep(0.02)

        assert child_pid_file.exists(), (
            "nested pytest never reached the deliberately slow test")
        child_pid = int(child_pid_file.read_text())
        os.kill(child_pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()

    assert proc.returncode == 128 + signal.SIGTERM, (stdout, stderr)
    assert "killed by signal SIGTERM (15)" in stderr
    assert "test failure" not in stderr.lower()


def test_collect_only_testmon_is_clean_on_the_same_fixture(extensionless_repo):
    # Same corpus, same plugin, ONE flag different -- and this is the real
    # collector the tier calls, not a hand-written argv.
    _executing_testmon_run(extensionless_repo)  # leaves a warm .testmondata
    assert ts.testmondata_path(extensionless_repo).exists()

    result = ts._pytest_collect_only(extensionless_repo, ["--testmon"])

    assert result.returncode in ts._COLLECTOR_OK_EXIT_CODES
    assert result.ids == ["bin/test_toy.py::test_toy_answer"]


def test_impact_tier_completes_on_a_crash_capable_corpus(extensionless_repo, capfd):
    # End to end: the real build_impact_tier_plan + the real executing run it
    # decides on, over a corpus proven one test above to be able to crash.
    _executing_testmon_run(extensionless_repo)
    capfd.readouterr()  # discard the deliberate crash from the warm-up

    rc = ts.run_impact_tier(extensionless_repo)

    output = "".join(capfd.readouterr())
    assert rc == 0, output
    for marker in _EXTENSIONLESS_CRASH_MARKERS:
        assert marker not in output, output


def test_collect_only_never_records_executed_coverage_fingerprints(extensionless_repo):
    """The fingerprint-level half of the promote_lib claim this bead corrects.

    A collect-only --testmon run is NOT a no-op on .testmondata -- it inserts
    the node ids it collected plus a checksum of each test FILE. What it can
    never record is which files a test EXECUTES, because nothing executed. That
    is both why an impact-tier run can never keep the coverage map fresh (only
    the opt-in warm_gate_testmon_cache path can) and why it never
    reaches get_tests_fingerprints, the crash site.
    """
    result = ts._pytest_collect_only(extensionless_repo, ["--testmon"])
    assert result.returncode in ts._COLLECTOR_OK_EXIT_CODES

    db = ts.testmondata_path(extensionless_repo)
    assert db.exists(), "collect-only --testmon wrote no map at all -- the file is absent"
    conn = sqlite3.connect(db)
    try:
        recorded = {row[0] for row in conn.execute("SELECT filename FROM file_fp")}
    finally:
        conn.close()

    # It DID write: the test file's own checksum is there...
    assert "bin/test_toy.py" in recorded, recorded
    # ...but never the extensionless module the test EXECUTES. That absence is
    # the crash's structural unreachability, stated as data rather than prose:
    # get_file() is only ever called for files a test was observed executing.
    assert not any(Path(f).name == "sable-toy" for f in recorded), recorded


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
