#!/usr/bin/env python3
"""Integration tests for SABLE-hauwa — impact-scoping the coverage floor's
diff-cover run. Real temporary git repos, real pytest-testmon / pytest-impact
subprocess runs via tier_selection.run_diff_cover_scope's default (real)
collector, and (for the end-to-end tests) the REAL .github/ci/diff-cover-
gate.sh script copied into a synthetic repo — never the live repo's own bin/,
which would recursively re-run this repo's own full suite from inside itself
(the exact load-manufacturing trap SABLE-owix4's dispatch flagged; see
bin/test_coverage_floor_integration.py's _init_synthetic_repo for the same
pattern this file reuses).

Self-skips (SABLE-59zu clean-room contract) when pytest-testmon / pytest-
impact aren't importable in this interpreter.

bin/test_tier_selection.py covers build_diff_cover_scope_plan's PURE decision
table directly (injected ImpactTierPlan, no subprocess). This file proves the
real, wired-up path: tier_selection.run_diff_cover_scope end to end, and the
real diff-cover-gate.sh end to end for the line-1400 shape.
"""
# sable-test-load: nested-runner -- defining E2E exercises real pytest selector and gate fallbacks
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = REPO_ROOT / ".github" / "ci" / "diff-cover-gate.sh"
TIER_SELECTION = REPO_ROOT / "bin" / "tier_selection.py"

sys.path.insert(0, str(REPO_ROOT / "bin"))
import tier_selection as ts  # noqa: E402

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("testmon") is None
    or importlib.util.find_spec("pytest_impact") is None,
    reason="pytest-testmon / pytest-impact not installed",
)


def _run(cwd, *args, timeout=None):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *args],
        cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def _git(cwd, *args, check=True):
    cp = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, capture_output=True, text=True,
    )
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stdout}{cp.stderr}")
    return cp.stdout.strip()


@pytest.fixture
def fixture_repo(tmp_path):
    """Two independent module/test pairs sharing a conftest fixture — the
    same shape as test_tier_selection_integration.py's fixture_repo, plus a
    baseline testmon warm run so build_impact_tier_plan gets a real cache
    hit (mirroring what _stage_testmondata would otherwise have to copy in
    from a primary tree)."""
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

    result = _run(repo, "bin/", "-q", "--testmon")
    assert result.returncode == 0, result.stdout + result.stderr
    assert ts.testmondata_path(repo).exists()

    return repo


# --- run_diff_cover_scope: real end-to-end, positive shape -------------------

def test_scopes_to_the_touched_file_when_only_a_test_file_changes(fixture_repo):
    base_sha = _git(fixture_repo, "rev-parse", "HEAD")
    (fixture_repo / "bin" / "test_module_a.py").write_text(
        dedent(
            """\
            from module_a import compute

            def test_module_a_computes(shared_value):
                assert compute() + shared_value == 42
                assert compute() == 41
            """
        )
    )
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-q", "-m", "strengthen the assertion")

    plan = ts.run_diff_cover_scope(fixture_repo, base_sha)

    assert plan.mode == "scoped", plan.reason
    assert plan.test_paths == ["bin/test_module_a.py"]


def test_scoped_run_and_full_run_agree_on_pytest_outcome(fixture_repo):
    # UNIT/parity spec: the scoped selection must exercise the same tests
    # (and therefore report the same pass/fail + coverage) as a full run
    # would for the lines this diff touches.
    base_sha = _git(fixture_repo, "rev-parse", "HEAD")
    (fixture_repo / "bin" / "module_a.py").write_text(
        dedent(
            """\
            def compute():
                return 41 + 0
            """
        )
    )
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-q", "-m", "no-op refactor of compute()")

    plan = ts.run_diff_cover_scope(fixture_repo, base_sha)
    assert plan.mode == "scoped", plan.reason

    scoped_result = _run(fixture_repo, *plan.test_paths, "-q", "-p", "no:cacheprovider")
    full_result = _run(fixture_repo, "bin/", "-q", "-p", "no:cacheprovider")

    assert scoped_result.returncode == 0, scoped_result.stdout + scoped_result.stderr
    assert full_result.returncode == 0, full_result.stdout + full_result.stderr
    assert "1 passed" in scoped_result.stdout
    assert "2 passed" in full_result.stdout


# --- NEGATIVE CONTROL, real (SABLE-hauwa, non-negotiable) --------------------

def test_negative_control_deleted_test_file_fails_closed_to_full(fixture_repo):
    # A diff that deletes the ONLY test covering some behavior can never be
    # re-collected — its coverage is, by construction, coming from a suite
    # this run cannot run. Must fail closed, not silently proceed scoped
    # (which would make coverage.xml simply omit whatever that file alone
    # covered, reading as a false miss on nothing at all — deceptively
    # "clean" instead of honestly "we can't tell").
    base_sha = _git(fixture_repo, "rev-parse", "HEAD")
    (fixture_repo / "bin" / "test_module_b.py").unlink()
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-q", "-m", "delete test_module_b.py")

    plan = ts.run_diff_cover_scope(fixture_repo, base_sha)

    assert plan.mode == "full", plan.reason
    assert plan.test_paths == []


def test_negative_control_broken_collector_fails_closed_to_full(fixture_repo):
    # Real subprocess, no mocking: an unrecognized selector flag reproduces
    # exactly what a missing/incompatible pytest-impact plugin looks like on
    # an ephemeral runner — a usage error, indistinguishable from "nothing
    # impacted" unless the returncode is checked (mirrors
    # test_tier_selection_integration.py's test_missing_plugin_falls_back_
    # to_full_run for build_impact_tier_plan; this proves the SAME guarantee
    # holds for build_diff_cover_scope_plan's caller).
    base_sha = _git(fixture_repo, "rev-parse", "HEAD")
    (fixture_repo / "bin" / "test_module_a.py").write_text(
        dedent(
            """\
            from module_a import compute

            def test_module_a_computes(shared_value):
                assert compute() + shared_value == 42
                assert compute() == 41
            """
        )
    )
    _git(fixture_repo, "add", "-A")
    _git(fixture_repo, "commit", "-q", "-m", "strengthen the assertion")

    def broken_collector(repo_root, extra_args):
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "bin/", "--collect-only", "-q",
             "--impact-nonexistent-flag", *extra_args],
            cwd=repo_root, capture_output=True, text=True,
        )
        assert result.returncode == 4, result.stdout + result.stderr
        return ts.CollectResult(ids=ts.parse_collect_only_nodeids(result.stdout),
                                returncode=result.returncode)

    plan = ts.run_diff_cover_scope(fixture_repo, base_sha, collector=broken_collector)

    assert plan.mode == "full"
    assert "exit 4" in plan.reason


# --- _stage_testmondata (SABLE-hauwa) -----------------------------------------

def test_stage_testmondata_copies_from_primary_tree_into_a_linked_worktree(fixture_repo):
    # Real `git worktree add`: reproduces sable_gate_promote_lib.
    # run_coverage_floor_check's exact topology — a throwaway checkout that
    # shares fixture_repo's .git but starts with no .testmondata of its own
    # (gitignored, so a linked worktree never inherits it). Without
    # _stage_testmondata, build_impact_tier_plan would read this as a cache
    # miss every time and this fix would never fire in the one place it
    # matters.
    linked = fixture_repo.parent / "linked-worktree"
    add = subprocess.run(
        ["git", "worktree", "add", "--detach", str(linked), "HEAD"],
        cwd=fixture_repo, capture_output=True, text=True,
    )
    assert add.returncode == 0, add.stdout + add.stderr
    try:
        assert not ts.testmondata_path(linked).exists()

        ts._stage_testmondata(linked)

        assert ts.testmondata_path(linked).exists()
        assert ts.testmondata_path(linked).read_bytes() == ts.testmondata_path(fixture_repo).read_bytes()
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(linked)], cwd=fixture_repo)
        subprocess.run(["git", "worktree", "prune"], cwd=fixture_repo)


def test_stage_testmondata_is_a_noop_when_repo_already_has_its_own(fixture_repo):
    # Guards against clobbering a real cache with a copy — should never
    # happen given the caller-side existence check, but this is the one
    # function that could do it, so it earns its own direct proof.
    original = ts.testmondata_path(fixture_repo).read_bytes()

    ts._stage_testmondata(fixture_repo)

    assert ts.testmondata_path(fixture_repo).read_bytes() == original


# --- real end-to-end through .github/ci/diff-cover-gate.sh -------------------
# Mirrors bin/test_coverage_floor_integration.py's _init_synthetic_repo
# pattern exactly (never touches the live repo's own bin/), extended to also
# carry bin/tier_selection.py so the scoped path can actually engage.

def _init_synthetic_repo(root: Path):
    (root / "bin").mkdir(parents=True)
    ci_dir = root / ".github" / "ci"
    ci_dir.mkdir(parents=True)
    (ci_dir / "diff-cover-gate.sh").write_text(GATE_SCRIPT.read_text())
    os.chmod(ci_dir / "diff-cover-gate.sh", 0o755)
    (root / "bin" / "tier_selection.py").write_text(TIER_SELECTION.read_text())
    _git(root, "init", "-q")
    _git(root, "config", "commit.gpgsign", "false")
    return root


def _run_gate(repo, base_sha, timeout=180):
    return subprocess.run(
        ["bash", str(repo / ".github" / "ci" / "diff-cover-gate.sh"), base_sha],
        cwd=repo, capture_output=True, text=True, timeout=timeout,
    )


def test_line_1400_shape_real_gate_scoped_matches_real_gate_full(tmp_path):
    # THE be4lo.7/21rug.4 DESIGN CONSTRAINT, proven through the REAL gate
    # script: a `pytest.skip(...)` call ADDED inside an existing test body.
    # The scoped run and the full run must reach the SAME verdict — the
    # skip line is "covered" by nothing more than the test being collected
    # and run far enough to hit it, and that must survive scoping.
    repo = _init_synthetic_repo(tmp_path / "synthetic-repo")

    (repo / "bin" / "foo.py").write_text("def foo(x):\n    return x\n")
    (repo / "bin" / "test_foo.py").write_text(
        dedent(
            """\
            import sys, os
            sys.path.insert(0, os.path.dirname(__file__))
            from foo import foo

            def test_foo():
                assert foo(1) == 1

            def test_rehearsal():
                pass
            """
        )
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base: foo() and two tests")
    base_sha = _git(repo, "rev-parse", "HEAD")

    result = _run(repo, "bin/", "-q", "--testmon")
    assert result.returncode == 0, result.stdout + result.stderr

    (repo / "bin" / "test_foo.py").write_text(
        dedent(
            """\
            import sys, os
            import pytest
            sys.path.insert(0, os.path.dirname(__file__))
            from foo import foo

            def test_foo():
                assert foo(1) == 1

            def test_rehearsal():
                pytest.skip("opt-in only")
            """
        )
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tip: add an inline pytest.skip guard")

    scoped_cp = _run_gate(repo, base_sha)
    assert scoped_cp.returncode == 0, (
        f"scoped real gate denied the line-1400 shape — "
        f"output:\n{scoped_cp.stdout}{scoped_cp.stderr}")

    (repo / "bin" / "tier_selection.py").unlink()
    full_cp = _run_gate(repo, base_sha)
    assert full_cp.returncode == 0, (
        f"full real gate (selector removed) denied the SAME diff the scoped "
        f"run allowed — output:\n{full_cp.stdout}{full_cp.stderr}")


def test_synthetic_genuine_pruning_pair_still_denies_scoped_through_the_real_gate(tmp_path):
    # POSITIVE CONTROL, scoped path engaged (companion to SABLE-owix4's
    # equivalent test, which predates this fix and never carries
    # tier_selection.py): scoping must not weaken the real coverage-delta
    # check. Base: a covered function and its only test. Tip: the function
    # grows a genuinely-uncovered branch AND its only test is deleted with
    # no replacement anywhere — the real gate must still deny.
    repo = _init_synthetic_repo(tmp_path / "synthetic-repo")

    (repo / "bin" / "foo.py").write_text("def foo(x):\n    return x\n")
    (repo / "bin" / "test_foo.py").write_text(
        dedent(
            """\
            import sys, os
            sys.path.insert(0, os.path.dirname(__file__))
            from foo import foo

            def test_foo():
                assert foo(1) == 1
            """
        )
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base: foo() and its only test")
    base_sha = _git(repo, "rev-parse", "HEAD")

    result = _run(repo, "bin/", "-q", "--testmon")
    assert result.returncode == 0, result.stdout + result.stderr

    (repo / "bin" / "foo.py").write_text(
        dedent(
            """\
            def foo(x):
                if x < 0:
                    return -x
                return x
            """
        )
    )
    (repo / "bin" / "test_foo.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tip: add uncovered branch, delete the only test")

    cp = _run_gate(repo, base_sha)

    assert "tier_selection: diff-cover-scope full" in cp.stderr, (
        "the deleting-test pruning shape did not visibly resolve to the real "
        f"full-run fallback:\n{cp.stdout}{cp.stderr}")
    assert cp.returncode != 0, (
        f"scoped real gate ALLOWED a genuine pruning diff (new uncovered "
        f"branch, only covering test deleted) — output:\n{cp.stdout}{cp.stderr}")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
