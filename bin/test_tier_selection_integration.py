#!/usr/bin/env python3
"""Integration tests for bin/tier_selection.py (SABLE-cmar4.3).

Real temporary git repo, real pytest-testmon and pytest-impact subprocess
runs via tier_selection.build_impact_tier_plan's default (real) collector —
no mocked pytest. Self-skips (SABLE-59zu clean-room contract: missing dep is
a skip, never a false-fail/false-pass) when either plugin isn't importable in
this interpreter.
"""
import importlib.util
import subprocess
import sys
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


# --- cache-warm regression: real full bin/ suite, real testmon crash ---------
# (SABLE-cmar4.3 second revise, mandated by chuck's CI root-cause on preview
# 4a46439 / branch 2795ee2). This is deliberately NOT the synthetic
# fixture_repo above: the defect is a property of THIS repo's actual bin/
# layout (~23 extensionless python executables loaded in-process via
# SourceFileLoader by real bin/test_*.py suites), which a fresh temp repo
# cannot reproduce. Red today by construction before the cmar4.3 second
# revise landed -- exit 3 with every test passing; run_cache_warm's
# classify_cache_warm_outcome must turn that into exit 0.

_THIS_FILE_RELATIVE = "bin/" + Path(__file__).name


def test_real_repo_full_suite_testmon_noselect_crash_is_tolerated():
    # --ignore=<this file> avoids the nested pytest run recursing into the
    # test that is currently invoking it (this suite runs bin/ broadly).
    repo_root = Path(__file__).resolve().parent.parent
    rc = ts.run_cache_warm(repo_root, extra_pytest_args=[f"--ignore={_THIS_FILE_RELATIVE}"])
    assert rc == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
