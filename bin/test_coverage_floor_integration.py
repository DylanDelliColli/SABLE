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

SABLE-a4i8h adds a second real-repo pair to the same synthetic harness, this
time driven through the merge gate's OWN entry point
(sable_gate_promote_lib.assert_coverage_floor) rather than the gate script
alone, because the defect it covers lived in the PRE-filter that decides
whether the script is ever run at all:

  * SKIPIF-ONLY PRUNING, DENIED — a tip whose ONLY pruning signal is a
    `@pytest.mark.skipif` decorator (no test function removed, no test file
    deleted), which erases the coverage of a source branch added in the same
    commit. Pre-fix this scored NOT-pruning, so assert_coverage_floor
    returned without consulting any check and the diff promoted with no
    event. Must now raise GateError(EXIT_COVERAGE_FLOOR).

  * SKIPIF-ONLY, NON-PRUNING SIBLING, ALLOWED — the same harness with an
    ordinary test added and no marker anywhere: assert_coverage_floor must
    return silently and must not run the (slow, real) check at all. Without
    this leg the deny above is equally consistent with a floor that now
    denies everything, which is the SABLE-r5pfw erosion the fix must not
    cause.
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
import sable_gate_classify_lib as classify  # noqa: E402
import sable_gate_promote_lib as promote_lib  # noqa: E402

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


def _local_clone(tmp_path):
    """A local clone of THIS repo (`git clone --local` only READS from
    REPO_ROOT — cloning is not in sable-fixture-tripwire's MUTATING set)
    so every worktree add/remove/prune that follows runs against an
    ISOLATED fixture, never the live repo this dispatch is running out of.
    A mutating `git worktree` op with cwd bound to the real repo root —
    even one a test intends to clean up after itself — is exactly the
    escape-hatch shape SABLE-0ssz.2's tripwire exists to ban (three prior
    P0s shared that mechanism); routing through a throwaway clone first
    means a bug in this test's cleanup can only ever corrupt the clone."""
    clone = tmp_path / "repo-clone"
    cp = _run(["git", "clone", "--local", "-q", str(REPO_ROOT), str(clone)],
              cwd=tmp_path)
    assert cp.returncode == 0, f"local clone of REPO_ROOT failed: {cp.stdout}"
    return clone


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
    clone = _local_clone(tmp_path)
    worktree = tmp_path / "be4lo7-tip"
    add = _run(["git", "worktree", "add", "--detach", str(worktree), BE4LO7_TIP],
              cwd=clone)
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
        _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=clone)
        _run(["git", "worktree", "prune"], cwd=clone)


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


# --------------------------------------------------------------------------
# SABLE-a4i8h — skipif-only pruning, through the gate's own entry point
# --------------------------------------------------------------------------

def _commit_skipif_only_pair(repo: Path):
    """Build a base/tip pair whose ONLY pruning signal is a newly-added
    `@pytest.mark.skipif`. The tip also grows an uncovered branch in the
    source the skipped test was the sole cover for, so the skip genuinely
    erases coverage — which is the whole reason this shape must reach the
    real check instead of being waved through by the pre-filter."""
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

    # tip: foo() grows an uncovered branch, and its only test is SKIPPED —
    # not removed, not renamed, not deleted. The skipif is the only signal.
    (repo / "bin" / "foo.py").write_text(
        "def foo(x):\n"
        "    if x < 0:\n"
        "        return -x\n"
        "    return x\n")
    (repo / "bin" / "test_foo.py").write_text(
        "import sys, os\n"
        "import pytest\n"
        "sys.path.insert(0, os.path.dirname(__file__))\n"
        "from foo import foo\n"
        "\n"
        '@pytest.mark.skipif(True, reason="env-gated, like the bd/dolt guards")\n'
        "def test_foo():\n"
        "    assert foo(1) == 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tip: add uncovered branch, skipif its only test")
    return base_sha, _git(repo, "rev-parse", "HEAD")


def test_skipif_only_pruning_diff_is_denied_through_the_real_gate(tmp_path, monkeypatch):
    """*** THE INTEGRATION LEG OF SABLE-a4i8h. *** REAL git repo, REAL `git
    diff` (no hand-built diff string anywhere in this test), REAL
    diff-cover-gate.sh with real pytest + coverage.py + diff-cover, driven
    through the merge gate's own entry point — sable_gate_promote_lib.
    assert_coverage_floor, the function promote() actually calls.

    The defect lived in the PRE-filter, so testing the gate SCRIPT alone
    could never have caught it: pre-fix, detect_pruning scored this diff
    NOT-pruning, assert_coverage_floor returned without running any check,
    and the branch promoted with its coverage silently erased. The floor
    must now DENY with EXIT_COVERAGE_FLOOR (27), and the deny message must
    NAME the marker's file and line — the acceptance criterion, asserted
    end-to-end on the string a seat would actually read."""
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "300")
    repo = _init_synthetic_repo(tmp_path / "skipif-only-repo")
    base_sha, tip_sha = _commit_skipif_only_pair(repo)

    diff_text = _git(repo, "diff", f"{base_sha}...{tip_sha}")
    signal = cf.detect_pruning(diff_text)
    assert signal.is_pruning, (
        f"a real skipif-only pruning diff scored NOT-pruning — this is the "
        f"exact fail-open: {diff_text}")
    # The skipif is the ONLY signal: no def count change, no deleted file.
    assert signal.net_test_function_delta == 0
    assert signal.deleted_test_files == []
    assert signal.newly_skipped_markers == 1

    marker = signal.skip_markers[0]
    assert marker.path == "bin/test_foo.py"
    assert marker.marker == "pytest.mark.skipif"
    # The reported line number is checked against the REAL post-image, so a
    # plausible-but-wrong line cannot pass: read the file at the tip and
    # confirm the named line is the marker it claims.
    tip_lines = (repo / "bin" / "test_foo.py").read_text().splitlines()
    assert tip_lines[marker.line - 1].strip() == marker.text

    with pytest.raises(classify.GateError) as excinfo:
        promote_lib.assert_coverage_floor(str(repo), "SABLE-a4i8h",
                                          base_sha, tip_sha, None)
    assert excinfo.value.code == classify.EXIT_COVERAGE_FLOOR
    message = str(excinfo.value)
    # DENIED-BECAUSE-MEASURED, not denied-because-unmeasurable. Both outcomes
    # exit 27, so a test that asserted only the code would pass just as
    # happily if diff-cover never ran (missing script, failed worktree add,
    # timeout) — which would make this leg a green light for a path that was
    # never exercised, the same silent-instrument shape as the defect itself.
    assert "check FAILED" in message, (
        f"the floor denied, but not because the real check ran and failed — "
        f"this leg proves nothing about the real path. message: {message}")
    assert "no coverage-delta check" not in message
    assert "bin/test_foo.py:6" in message, (
        f"the deny must NAME the marker's file and line — a count with no "
        f"operands is what cost a seat a hand-diff. message: {message}")
    assert "skipif" in message


def test_a_non_pruning_diff_still_allows_through_the_real_gate(tmp_path, monkeypatch):
    """KNOWN-POSITIVE / LOAD-BEARING NEGATIVE CONTROL, same real path. The
    deny above is equally consistent with a floor that now denies
    everything, and a gate that fires on ordinary test edits trains the seat
    to reach for --coverage-override by reflex (SABLE-r5pfw) — strictly
    worse than the under-match this bead fixes. Same harness, same entry
    point, tip adds an ordinary test and no marker: assert_coverage_floor
    must return silently.

    It must also do so WITHOUT running the real check. That is asserted, not
    assumed: run_coverage_floor_check is monkeypatched to explode, so if the
    pre-filter ever starts classifying a plain test addition as pruning this
    test fails loudly instead of merely getting slower."""
    monkeypatch.setenv("SABLE_MG_COVERAGE_FLOOR_TIMEOUT", "300")
    repo = _init_synthetic_repo(tmp_path / "non-pruning-repo")
    (repo / "bin" / "foo.py").write_text("def foo(x):\n    return x\n")
    (repo / "bin" / "test_foo.py").write_text(
        "import sys, os\n"
        "sys.path.insert(0, os.path.dirname(__file__))\n"
        "from foo import foo\n"
        "\n"
        "def test_foo():\n"
        "    assert foo(1) == 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")

    with (repo / "bin" / "test_foo.py").open("a") as fh:
        fh.write("\ndef test_foo_negative():\n    assert foo(-1) == -1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tip: add an ordinary test, no markers")
    tip_sha = _git(repo, "rev-parse", "HEAD")

    def _explode(*a, **kw):
        raise AssertionError(
            "the coverage-delta check ran on a diff that adds a test and "
            "prunes nothing — the pre-filter has broadened into a gate that "
            "fires on everything (SABLE-r5pfw)")

    monkeypatch.setattr(promote_lib, "run_coverage_floor_check", _explode)
    signal = cf.detect_pruning(_git(repo, "diff", f"{base_sha}...{tip_sha}"))
    assert not signal.is_pruning, signal.reasons
    promote_lib.assert_coverage_floor(str(repo), "SABLE-a4i8h",
                                      base_sha, tip_sha, None)


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
