#!/usr/bin/env python3
"""Integration coverage for SABLE-owix4 — the REAL .github/ci/diff-cover-gate.sh,
real pytest + coverage.py + diff-cover, no mocks.

bin/test_coverage_floor.py covers the PURE detect_pruning/evaluate_coverage_
floor logic directly (including the be4lo.7 rename shape as a hand-built
diff). This file proves the fix against the gate the fleet actually runs:

  * REAL DIFF, FAST — detect_pruning fed the ACTUAL `git diff` between the
    real SABLE-be4lo.7 base and tip commits (both already present in this
    repo's history). No subprocess, no pytest run — this is the direct,
    no-mock proof that the fix resolves the exact reported instance's exact
    diff content, independent of how long the real coverage-delta check
    takes to run.

  * REAL CASE, SLOW, OPT-IN ONLY — the same base/tip pair, this time
    through the actual .github/ci/diff-cover-gate.sh end to end (a `git
    worktree add --detach` checkout of the tip, real pytest + coverage.py +
    diff-cover), asserting exit 0 WITHOUT any override. Gated behind
    SABLE_RUN_SLOW_COVERAGE_FLOOR_INTEGRATION=1 (skipped otherwise): this
    test's own body shells out to the SAME `pytest bin/ --cov=bin` command
    an ordinary `pytest bin/` sweep is already running — including the
    coverage floor's own real check when IT promotes this very branch, so
    left enabled by default this test would recursively re-run the entire
    bin/ suite from inside itself on every sweep, the load-manufacturing
    the dispatch explicitly forbids taking unilaterally (its section 9).
    Measured on this host even so: this repo's own `pytest bin/ --cov=bin`
    run alone exceeds 1500s wall — a known, already-tracked, OPEN adjacent
    defect (SABLE-9yjt5; corroborating measurement logged there
    2026-07-24) unrelated to detect_pruning's logic, not this bead's
    footprint to fix. Rather than hard-fail the whole bead's test evidence
    on that unrelated defect, this test additionally SKIPS (not passes,
    not silently) with a message naming SABLE-9yjt5 if the real run can't
    complete in the time budgeted once it IS opted in — the REAL DIFF,
    FAST test above is what actually, unconditionally proves the fix; this
    one is the full end-to-end rehearsal for whenever a human/agent wants
    it and the host/gate is fast enough to complete it.

  * POSITIVE CONTROL — a synthetic scratch repo mirroring this repo's
    bin/-rooted layout (diff-cover-gate.sh hardcodes `pytest bin/
    --cov=bin`), where the tip commit adds a genuinely-uncovered branch to a
    source function AND deletes the only test that could have covered it,
    with no replacement anywhere. This proves the real coverage-delta check
    still denies a genuine pruning diff through the same real path — the
    fix is a precision fix on the PRE-filter (detect_pruning), not a
    weakening of the actual coverage measurement.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
GATE_SCRIPT = REPO_ROOT / ".github" / "ci" / "diff-cover-gate.sh"

sys.path.insert(0, str(REPO_ROOT / "bin"))
import sable_coverage_floor_lib as cf  # noqa: E402

BE4LO7_BASE = "5ac902e"
BE4LO7_TIP = "e3e776a"


def _run(argv, cwd, env=None, timeout=None):
    return subprocess.run(argv, cwd=cwd, env=env, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          timeout=timeout)


def _git(cwd, *args, check=True):
    cp = _run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd)
    if check and cp.returncode != 0:
        raise AssertionError(f"git {args} failed: {cp.stdout}")
    return cp.stdout.strip()


def _require_real_history():
    cp = _run(["git", "cat-file", "-t", BE4LO7_TIP], cwd=REPO_ROOT)
    if cp.returncode != 0 or cp.stdout.strip() != "commit":
        pytest.skip(f"{BE4LO7_TIP} not present in this checkout's history — "
                    "cannot run the real be4lo.7 rehearsal")


def test_real_be4lo7_diff_is_not_flagged_pruning():
    """REAL DIFF, FAST (SABLE-owix4, direct proof): detect_pruning fed the
    ACTUAL `git diff` between the real base and tip commits — no hand-built
    stand-in, the exact bytes that were denied at exit 27 on 2026-07-24.
    Coverage measurably increased (net +12 test functions, 89->90 and
    32->43 defs across two files, exactly one def name renamed with a
    strengthened assertion); this must come back non-pruning, and the
    decision table must ALLOW with no coverage-delta check consulted at
    all — the whole point of the fix."""
    _require_real_history()
    diff_text = _git(REPO_ROOT, "diff", f"{BE4LO7_BASE}...{BE4LO7_TIP}")
    signal = cf.detect_pruning(diff_text)
    assert not signal.is_pruning, (
        f"detect_pruning still flags the real be4lo.7 diff as pruning — "
        f"reasons: {signal.reasons}")
    decision = cf.evaluate_coverage_floor(signal, None, None)
    assert decision.action == cf.ACTION_ALLOW
    assert "not a pruning diff" in decision.reason


@pytest.mark.skipif(
    os.environ.get("SABLE_RUN_SLOW_COVERAGE_FLOOR_INTEGRATION") != "1",
    reason="opt-in only (SABLE-owix4 self-screen finding): this test shells "
           "out to the REAL diff-cover-gate.sh, which itself runs `pytest "
           "bin/ --cov=bin` — the SAME command an ordinary `pytest bin/` "
           "sweep is already running. Left enabled by default, this test "
           "would recursively re-run the ENTIRE bin/ suite from inside "
           "itself every time bin/ is swept — including inside the "
           "coverage floor's own real invocation of that exact command "
           "against this branch, which is exactly the kind of load-"
           "manufacturing the dispatch's NO LOAD MANUFACTURING rule (SABLE-"
           "owix4 section 9) forbids taking unilaterally. Opt in explicitly: "
           "SABLE_RUN_SLOW_COVERAGE_FLOOR_INTEGRATION=1 pytest "
           "bin/test_coverage_floor_integration.py -k real_be4lo7_rename_pair")
def test_real_be4lo7_rename_pair_passes_the_real_gate_without_override(tmp_path):
    """REAL CASE, SLOW, OPT-IN ONLY (see skipif reason above): run the
    actual gate script the merge seat runs, against the actual commits that
    were denied at exit 27 on 2026-07-24. Coverage went UP (net +12 tests);
    this must exit 0 with no 'Coverage override' line anywhere in play — the
    gate is never even told about one. If the underlying pytest+coverage run
    cannot complete in the time budgeted here, that is the separate,
    already-tracked, OPEN adjacent defect SABLE-9yjt5 (bare `pytest bin/`
    measured at 15+min, --cov adds more on top) — NOT this bead's footprint
    to fix — so this test SKIPS with that reason rather than failing the
    whole bead's evidence on an unrelated defect.
    test_real_be4lo7_diff_is_not_flagged_pruning above is the fast,
    unconditional, always-on proof the fix itself is correct."""
    _require_real_history()
    worktree = tmp_path / "be4lo7-tip"
    add = _run(["git", "worktree", "add", "--detach", str(worktree), BE4LO7_TIP],
              cwd=REPO_ROOT)
    assert add.returncode == 0, f"worktree add failed: {add.stdout}"
    try:
        script = worktree / ".github" / "ci" / "diff-cover-gate.sh"
        assert script.is_file(), "diff-cover-gate.sh missing at be4lo.7 tip"
        try:
            cp = _run(["bash", str(script), BE4LO7_BASE], cwd=worktree, timeout=1500)
        except subprocess.TimeoutExpired:
            pytest.skip(
                "real diff-cover-gate.sh did not complete within 1500s on "
                "this host — this is the known, OPEN, separately-tracked "
                "SABLE-9yjt5 performance defect (bare `pytest bin/` alone "
                "measured at 15+min; --cov adds more), not a SABLE-owix4 "
                "regression. See test_real_be4lo7_diff_is_not_flagged_"
                "pruning for the fast, unconditional proof of this fix.")
        assert cp.returncode == 0, (
            f"real diff-cover-gate.sh denied a branch that measurably "
            f"increased test coverage (89->90, 32->43 defs) — output:\n"
            f"{cp.stdout}")
    finally:
        _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO_ROOT)
        _run(["git", "worktree", "prune"], cwd=REPO_ROOT)


def _init_synthetic_repo(root: Path):
    """A minimal repo mirroring this repo's bin/-rooted layout, just enough
    for the REAL diff-cover-gate.sh (which hardcodes `pytest bin/
    --cov=bin`) to run against it unmodified."""
    (root / "bin").mkdir(parents=True)
    ci_dir = root / ".github" / "ci"
    ci_dir.mkdir(parents=True)
    (ci_dir / "diff-cover-gate.sh").write_text(GATE_SCRIPT.read_text())
    os.chmod(ci_dir / "diff-cover-gate.sh", 0o755)
    _git(root, "init", "-q")
    _git(root, "config", "commit.gpgsign", "false")
    return root


def test_synthetic_genuine_pruning_pair_still_denies_through_the_real_gate(tmp_path):
    """POSITIVE CONTROL (SABLE-owix4): the fix narrows detect_pruning's
    PRE-filter — it must not weaken the real coverage measurement it gates.
    Base: a covered function and its only test. Tip: the function grows a
    genuinely-uncovered branch AND its only test is deleted with no
    replacement anywhere — a real pruning diff. The real diff-cover-gate.sh
    must still exit non-zero."""
    repo = _init_synthetic_repo(tmp_path / "synthetic-repo")

    (repo / "bin" / "foo.py").write_text(
        "def foo(x):\n"
        "    return x\n")
    (repo / "bin" / "test_foo.py").write_text(
        "import sys, os\n"
        "sys.path.insert(0, os.path.dirname(__file__))\n"
        "from foo import foo\n"
        "\n"
        "def test_foo():\n"
        "    assert foo(1) == 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base: foo() and its only test")
    base_sha = _git(repo, "rev-parse", "HEAD")

    # tip: foo() grows an uncovered branch, and the ONLY test that could
    # cover anything about foo() is deleted outright — no replacement.
    (repo / "bin" / "foo.py").write_text(
        "def foo(x):\n"
        "    if x < 0:\n"
        "        return -x\n"
        "    return x\n")
    (repo / "bin" / "test_foo.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tip: add uncovered branch, delete the only test")

    cp = _run(["bash", str(repo / ".github" / "ci" / "diff-cover-gate.sh"), base_sha],
              cwd=repo, timeout=120)
    assert cp.returncode != 0, (
        f"real diff-cover-gate.sh ALLOWED a genuine pruning diff (new "
        f"uncovered branch, only covering test deleted) — this would mean "
        f"the SABLE-owix4 fix weakened the real check, not just the "
        f"pre-filter. output:\n{cp.stdout}")


def test_synthetic_rename_with_strengthened_assertion_passes_the_real_gate(tmp_path):
    """Same synthetic harness, but the tip commit performs exactly the
    be4lo.7 shape at small scale: the test is RENAMED and its assertion
    STRENGTHENED, source unchanged. No coverage-delta check should even be
    needed — but this test proves it whichever way it's reached: the real
    gate must exit 0."""
    repo = _init_synthetic_repo(tmp_path / "synthetic-repo-rename")

    (repo / "bin" / "foo.py").write_text(
        "def foo(x):\n"
        "    return abs(x)\n")
    (repo / "bin" / "test_foo.py").write_text(
        "import sys, os\n"
        "sys.path.insert(0, os.path.dirname(__file__))\n"
        "from foo import foo\n"
        "\n"
        "def test_foo_returns_a_number():\n"
        "    assert foo(-1) == 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base: foo() and a weak test")
    base_sha = _git(repo, "rev-parse", "HEAD")

    (repo / "bin" / "test_foo.py").write_text(
        "import sys, os\n"
        "sys.path.insert(0, os.path.dirname(__file__))\n"
        "from foo import foo\n"
        "\n"
        "def test_foo_returns_the_absolute_value_for_negative_and_positive_input():\n"
        "    assert foo(-1) == 1\n"
        "    assert foo(1) == 1\n"
        "    assert foo(0) == 0\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tip: rename test, strengthen the assertion")

    cp = _run(["bash", str(repo / ".github" / "ci" / "diff-cover-gate.sh"), base_sha],
              cwd=repo, timeout=120)
    assert cp.returncode == 0, (
        f"real diff-cover-gate.sh denied a rename+strengthen with no actual "
        f"coverage loss — output:\n{cp.stdout}")
