#!/usr/bin/env python3
"""Impact-tier pytest selector for bin/ (SABLE-cmar4.3).

pytest-testmon (line-coverage diff) and pytest-impact (fixture/conftest/hook
diff) are wired as two INDEPENDENT selectors whose SELECTED node-id sets are
unioned, never intersected. Passing both --testmon and --impact to the same
pytest invocation would AND their deselection decisions instead (each plugin's
pytest_collection_modifyitems hook filters the items list the previous one
already filtered), silently under-selecting relative to either tool alone.
pytest-impact's own README documents exactly this collect-then-union recipe
under "Composing with import-graph tools" -- this module is that recipe,
generalized to its sibling selector (pytest-testmon) instead of an import-graph
tool.

.testmondata lifecycle: this selector serves the local merge-preview impact
tier. Sealed-candidate CI does not use it and always runs the complete suite.
Operators can refresh the local map explicitly with `sable-merge-gate
warm-testmon-cache`. On a cache MISS this module falls back to a conservative
FULL run of bin/, and deliberately does NOT consult pytest-impact for a
partial selection in that case either: a
silently empty testmon map paired with a narrow fixture-only selector could
select close to nothing while "passing" by not running -- exactly the
silent-green failure class SABLE-7v3z and this epic exist to eliminate.
Cache miss means full run, full stop.

DIFF-COVER SCOPING (SABLE-hauwa): build_diff_cover_scope_plan / run_diff_cover_
scope below are a SEPARATE consumer of this module's machinery, added to let
.github/ci/diff-cover-gate.sh scope the pytest+coverage.py run it needs for
patch-coverage measurement to the diff's own footprint instead of paying the
full bin/ suite's ~887s every time. See build_diff_cover_scope_plan's own
docstring for why this needs an ADDITIONAL guarantee on top of
build_impact_tier_plan's existing cache-miss/collector-failure safety net:
a selection good enough to decide "does the test suite still pass" is not
automatically good enough to decide "is this exact diff line covered" --
lines covered only by a suite this selection left out would silently read as
uncovered (a false miss), which is worse than the slow full run this exists
to avoid.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

TESTMON_DATAFILE = ".testmondata"

# Exit codes `pytest --collect-only` itself treats as a successful collection
# outcome: 0 (ids collected) and 5 (collection ran, legitimately selected
# nothing -- pytest's own "no tests ran" code). Anything else (4 usage error,
# 3 internal error, 2 interrupted, ...) means the collector process itself is
# broken -- e.g. a missing/incompatible pytest-testmon or pytest-impact plugin
# on an ephemeral CI runner -- and must NOT be read as "nothing impacted".
_COLLECTOR_OK_EXIT_CODES = frozenset({0, 5})


class CollectResult(NamedTuple):
    ids: List[str]
    returncode: int


Collector = Callable[[Path, List[str]], CollectResult]


def testmondata_path(repo_root: Path) -> Path:
    """Where pytest-testmon's coverage map lives for this repo."""
    return Path(repo_root) / TESTMON_DATAFILE


def parse_collect_only_nodeids(output: str) -> List[str]:
    """Pure parser for `pytest --collect-only -q` stdout.

    Each collected test prints as one line containing '::'; the trailing
    summary line ("N tests collected in Ns") and blank lines are dropped.
    """
    ids = []
    for line in output.splitlines():
        line = line.strip()
        if "::" in line:
            ids.append(line)
    return ids


def _pytest_collect_only(repo_root: Path, extra_args: List[str]) -> CollectResult:
    """Real collector: shells out to `pytest bin/ --collect-only -q <extra_args>`
    and parses the selected node ids. Never executes any test body.

    --collect-only IS LOAD-BEARING, NOT INCIDENTAL (SABLE-jd5fj.19). This is the
    only invocation that ever receives --testmon (build_impact_tier_plan's
    cache-hit branch below), and pytest-testmon's extensionless-file crash lives
    in a per-test-EXECUTION hook -- so collecting is what keeps that crash
    unreachable, on a bin/ full of extensionless executables. The invariant is
    "--testmon only ever reaches a --collect-only run", NOT "--testmon is never
    passed"; test_tier_selection.py asserts it both statically and at runtime,
    with controls in both polarities. run_cache_warm is the one sanctioned
    exception (it takes the crash deliberately, and tolerates it).

    Surfaces the subprocess returncode alongside the parsed ids -- a failed
    collector (missing plugin, usage error, internal error) prints an empty
    node-id list just like a legitimately-empty selection, and the two are
    indistinguishable without the returncode. See build_impact_tier_plan.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "bin/", "--collect-only", "-q", *extra_args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return CollectResult(ids=parse_collect_only_nodeids(result.stdout), returncode=result.returncode)


class ImpactTierPlan(NamedTuple):
    mode: str  # "full" | "selected" | "none"
    argv: List[str]  # args to hand to `python -m pytest`
    reason: str


def build_impact_tier_plan(
    repo_root: Path,
    base_ref: str = "HEAD",
    collector: Collector = _pytest_collect_only,
) -> ImpactTierPlan:
    """Decide the pytest invocation for the bin/ impact tier.

    cache miss -> ("full", full-suite argv) -- see module docstring.
    cache hit  -> union pytest-testmon's and pytest-impact's collect-only
                  selections; ("selected", explicit node ids), or
                  ("none", []) if both independently agree nothing changed,
                  or ("full", ...) if either collector process itself failed
                  (see _COLLECTOR_OK_EXIT_CODES) -- a broken collector must
                  never be mistaken for a legitimately-empty selection.
    """
    if not testmondata_path(repo_root).exists():
        return ImpactTierPlan(
            mode="full",
            argv=["bin/", "-q", "-p", "no:cacheprovider"],
            reason="testmon cache miss (.testmondata absent) -- conservative full run",
        )

    testmon_result = collector(repo_root, ["--testmon"])
    impact_result = collector(repo_root, ["--impact", f"--impact-base={base_ref}"])

    failures = []
    if testmon_result.returncode not in _COLLECTOR_OK_EXIT_CODES:
        failures.append(f"pytest-testmon collector exit {testmon_result.returncode}")
    if impact_result.returncode not in _COLLECTOR_OK_EXIT_CODES:
        failures.append(f"pytest-impact collector exit {impact_result.returncode}")

    if failures:
        reason = "; ".join(failures) + " -- conservative full run"
        print(f"tier_selection: COLLECTOR FAILURE, falling back to full run: {reason}", file=sys.stderr)
        return ImpactTierPlan(
            mode="full",
            argv=["bin/", "-q", "-p", "no:cacheprovider"],
            reason=reason,
        )

    testmon_ids = set(testmon_result.ids)
    impact_ids = set(impact_result.ids)
    union_ids = sorted(testmon_ids | impact_ids)

    if not union_ids:
        return ImpactTierPlan(mode="none", argv=[], reason="no impacted tests")

    return ImpactTierPlan(
        mode="selected",
        argv=[*union_ids, "-q"],
        reason=(
            f"{len(union_ids)} impacted test(s) "
            f"(testmon={len(testmon_ids)}, impact={len(impact_ids)})"
        ),
    )


def run_impact_tier(repo_root: Path, base_ref: str = "HEAD") -> int:
    """Build the plan and actually execute it, returning pytest's exit code
    (0 with no subprocess call at all when nothing is impacted)."""
    plan = build_impact_tier_plan(repo_root, base_ref)
    print(f"tier_selection: {plan.mode} -- {plan.reason}", file=sys.stderr)
    if plan.mode == "none":
        return 0
    result = subprocess.run([sys.executable, "-m", "pytest", *plan.argv], cwd=repo_root)
    return result.returncode


# --------------------------------------------------------------------------
# diff-cover coverage-run scoping (SABLE-hauwa) -------------------------------
# --------------------------------------------------------------------------
# Python-only mirror of sable_coverage_floor_lib._TEST_FILE_RE's naming
# convention (bin/test_*.py / bin/*_test.py) -- deliberately duplicated
# rather than imported: this module has zero cross-module dependencies today
# (stdlib + subprocess only), and diff-cover only ever measures the pytest/
# coverage.py half of this repo's suites, so the .sh half of that sibling
# regex could never match anything reachable from here anyway.
_PY_TEST_FILE_RE = re.compile(r'(^|/)(test_[\w]+\.py|[\w]+_test\.py)$')


class DiffCoverScopePlan(NamedTuple):
    mode: str  # "scoped" | "full"
    test_paths: List[str]  # whole test FILE paths (not node ids) to hand to pytest
    reason: str


def build_diff_cover_scope_plan(
    tier_plan: ImpactTierPlan,
    diff_touched_files: Optional[List[str]],
    repo_root: Path,
) -> DiffCoverScopePlan:
    """PURE decision layer: is it PROVEN safe to hand diff-cover-gate.sh a
    scoped pytest invocation instead of the full `pytest bin/`?

    build_impact_tier_plan above already answers "which tests need to
    re-run so the suite's PASS/FAIL verdict stays trustworthy" -- that is a
    weaker question than the one THIS function must answer: "is every line
    this diff touches going to be correctly reported as covered or not".
    A test that isn't selected doesn't corrupt a pass/fail verdict (it
    simply doesn't run, so it can't report a false pass or false fail) but
    DOES corrupt a coverage.xml report -- coverage.py only knows about lines
    the processes it measured actually executed, so any line covered
    exclusively by an unselected test reads as 0% hit. "Faster and wrong is
    worse than slow and right" (SABLE-hauwa dispatch) is why this function
    exists as a layer on top of build_impact_tier_plan rather than a bare
    passthrough of its verdict.

    Two independent guarantees, EITHER of which failing fails CLOSED to a
    full run (mode="full"):

      1. tier_plan itself must be "selected" or "none", not "full" already
         (a passthrough of build_impact_tier_plan's own cache-miss/
         collector-failure safety net -- see its docstring).

      2. EVERY test file the diff itself touches (added, modified, or
         renamed-in) must be a member of the selected file set. This is the
         line-1400 guarantee (be4lo.7/21rug.4): a `pytest.skip(...)` call
         inside a test body is "covered" by nothing more than that test
         file being collected and run far enough to hit it -- a full run
         always does that trivially (every test file it owns is, by
         definition, run); a narrower selection only does if this check
         forces it to. Since a diff-touched test file is directly readable
         off the diff itself (no inference, no trusting a plugin's static
         or coverage-based reasoning), this check is unconditional --
         build_impact_tier_plan's testmon+impact union is trusted for
         OTHER files' transitive dependents, never for the file that is
         itself IN the diff.

      A file the diff touches that no longer exists on disk (deleted, or
      renamed away under a name git didn't fold into one line) can never be
      re-collected -- pytest cannot run a path that isn't there. Guarantee 2
      is then structurally unsatisfiable for it, so this is checked FIRST,
      before ever asking the collector anything: a diff that deletes a test
      file always falls back to full. This is deliberate, not an oversight
      -- a deleted test file is exactly one of the three
      sable_coverage_floor_lib.detect_pruning pruning shapes (its
      deleted_test_files signal), the shape most likely to silently erase
      coverage, and therefore the shape this function refuses to shortcut.

    diff_touched_files being None (not an empty list) means the caller
    could not determine the diff's file list at all (e.g. `git diff`
    itself failed) -- an unknown diff can never be proven covered, so this
    also fails closed unconditionally, before consulting tier_plan.
    """
    if diff_touched_files is None:
        return DiffCoverScopePlan(
            mode="full", test_paths=[],
            reason="could not determine diff-touched files (git diff failed) -- "
                   "cannot prove an unknown diff is covered")

    diff_test_files = sorted(f for f in diff_touched_files if _PY_TEST_FILE_RE.search(f))

    for f in diff_test_files:
        if not (Path(repo_root) / f).is_file():
            return DiffCoverScopePlan(
                mode="full", test_paths=[],
                reason=f"diff touches test file {f!r} that no longer exists at "
                       f"HEAD (deleted or renamed away) -- it cannot be "
                       f"re-collected, so the provably-covers guarantee is "
                       f"unsatisfiable for it")

    if tier_plan.mode == "full":
        return DiffCoverScopePlan(
            mode="full", test_paths=[],
            reason=f"impact-tier plan: {tier_plan.reason}")

    selected_files = sorted({nid.split("::", 1)[0] for nid in tier_plan.argv if "::" in nid})
    missing = [f for f in diff_test_files if f not in selected_files]
    if missing:
        return DiffCoverScopePlan(
            mode="full", test_paths=[],
            reason=f"impact-tier selection omits diff-touched test file(s) "
                   f"{missing} -- cannot prove the selection covers the diff "
                   f"(line-1400 guarantee, SABLE-hauwa)")

    scope_files = sorted(set(selected_files) | set(diff_test_files))
    if not scope_files:
        return DiffCoverScopePlan(
            mode="full", test_paths=[],
            reason="impact-tier plan selected no tests and the diff touches no "
                   "test file -- cannot prove an empty selection covers the diff")

    return DiffCoverScopePlan(
        mode="scoped", test_paths=scope_files,
        reason=f"{len(scope_files)} impact-scoped test file(s) "
               f"(impact-tier selected {len(selected_files)}, diff directly "
               f"touches {len(diff_test_files)})")


def _git_diff_touched_files(repo_root: Path, compare_ref: str) -> Optional[List[str]]:
    """I/O wrapper: paths touched between compare_ref and HEAD, three-dot
    (against their merge-base) -- matches pytest-impact's own --impact-base
    semantics (merge-base(REF, HEAD) unless --impact-no-merge-base) and
    sable_gate_promote_lib.assert_coverage_floor's diff_text computation, so
    "touched" here means exactly what detect_pruning already scanned.

    Returns None -- NOT an empty list -- when git itself fails (bad ref, not
    a git repo, ...): an empty list would read as "the diff touches
    nothing", a checkable fact build_diff_cover_scope_plan could reason
    about; None keeps that distinguishable from "we don't actually know",
    which must fail closed instead."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{compare_ref}...HEAD"],
        cwd=repo_root, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _stage_testmondata(repo_root: Path) -> None:
    """Copy a warm .testmondata from this checkout's PRIMARY working tree
    into repo_root, if repo_root doesn't already have one of its own
    (SABLE-hauwa). .testmondata is gitignored, so a linked `git worktree
    add` checkout -- exactly what sable_gate_promote_lib.
    run_coverage_floor_check creates to run this floor's check -- never
    inherits it just by sharing the primary tree's .git. Without this,
    build_impact_tier_plan reads every promotion as a cache miss and always
    falls back to the full run, silently negating this whole fix in the one
    place it is supposed to fire.

    Read-only against the primary tree (never writes back), so this has no
    interaction with hooks/test/test-impact-tier-serialization.sh's
    concurrent-WRITE concern -- that suite guards multiple runs writing the
    SAME file; this is a one-way copy out of it.

    Best-effort and silent: any failure (not a git repo, `git` missing, the
    primary tree has no .testmondata of its own, ...) just leaves repo_root
    without a cache, which build_impact_tier_plan already treats as an
    ordinary, safe cache miss -- so this function never raises."""
    if testmondata_path(repo_root).exists():
        return
    try:
        common_dir = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=repo_root, capture_output=True, text=True, check=False,
        )
        if common_dir.returncode != 0 or not common_dir.stdout.strip():
            return
        common_dir_path = Path(common_dir.stdout.strip())
        if not common_dir_path.is_absolute():
            common_dir_path = Path(repo_root) / common_dir_path
        primary_tree = common_dir_path.resolve().parent
        if primary_tree == Path(repo_root).resolve():
            return
        primary_cache = primary_tree / TESTMON_DATAFILE
        if primary_cache.is_file():
            shutil.copy2(primary_cache, testmondata_path(repo_root))
    except OSError:
        return


def run_diff_cover_scope(
    repo_root: Path, compare_ref: str, collector: Collector = _pytest_collect_only,
) -> DiffCoverScopePlan:
    """I/O wrapper: stage a warm testmon cache if one exists, build the real
    impact-tier plan, read the real diff-touched-files list, and hand all
    three to the pure build_diff_cover_scope_plan decision above."""
    _stage_testmondata(repo_root)
    tier_plan = build_impact_tier_plan(repo_root, base_ref=compare_ref, collector=collector)
    diff_touched_files = _git_diff_touched_files(repo_root, compare_ref)
    return build_diff_cover_scope_plan(tier_plan, diff_touched_files, repo_root)


def print_diff_cover_scope(repo_root: Path, compare_ref: str) -> int:
    """CLI entry point .github/ci/diff-cover-gate.sh shells out to. Contract:
    stdout's first line is always exactly "scoped" or "full"; on "scoped"
    every following line is one test-file path (repo-root-relative) to hand
    to pytest.

    ALWAYS exits 0 and NEVER lets an exception escape: diff-cover-gate.sh
    runs under `set -e`, so an uncaught crash here would abort the ENTIRE
    coverage check rather than just falling back to the full run it is
    supposed to degrade to -- strictly worse than the 900s this bead exists
    to avoid."""
    try:
        plan = run_diff_cover_scope(repo_root, compare_ref)
    except Exception as exc:  # noqa: BLE001 -- must degrade to full, never crash; see docstring
        print("full")
        print(f"tier_selection: diff-cover-scope crashed, falling back to full "
              f"run: {exc!r}", file=sys.stderr)
        return 0

    print(f"tier_selection: diff-cover-scope {plan.mode} -- {plan.reason}", file=sys.stderr)
    print(plan.mode)
    for path in plan.test_paths:
        print(path)
    return 0


# --- .testmondata cache-warm classification (SABLE-cmar4.3 second revise) ----
# The opt-in local cache warmer runs the full bin/ suite with
# --testmon-noselect. pytest-testmon 2.2.0 has a real, reproduced defect: its own
# SourceTree.get_file() (testmon/testmon_core.py:93) unconditionally does
# `filename.rsplit(".", 1)[1]` to compute an "extension" for every file its
# Coverage() instance measured; a filename with NO DOT makes rsplit return a
# 1-element list and [1] raises IndexError, crashing pytest with INTERNALERROR
# during test teardown -- AFTER every test already passed. This repo's bin/
# has ~23 python executables with a shebang but no .py suffix (bin/sable-msg,
# bin/sable-merge-gate, etc.) that several bin/test_*.py suites load
# in-process via importlib.machinery.SourceFileLoader, so coverage measures
# them and hits this every time.
#
# Confirmed dead end (repro'd directly against a minimal reproduction, not
# just read): pytest-testmon's own Coverage() instance
# (testmon.testmon_core.Testmon.setup_coverage) hardcodes
# include=[repo_root + "/*"] -- unioned in even when a pytest-cov --cov-config
# `source`/`run_include`/omit setting is present -- so there is no ini,
# .coveragerc, nor pytest-cov config surface that narrows what testmon
# measures. 2.2.0 is the latest pytest-testmon release; no upstream fix
# exists. The crash only fires on an ACTUAL (non-collect-only) run with
# --testmon/--testmon-noselect active, so it never affects this module's own
# collect-only collector (_pytest_collect_only) or the "selected" run
# (run_impact_tier does not pass --testmon) -- only a dedicated full-suite
# cache-warm run can hit it.
#
# classify_cache_warm_outcome narrowly tolerates ONLY this exact signature
# with zero reported test failures as a successful warm; anything else (a
# real test failure, a different internal error) still propagates as a real
# failure. A tolerated crash just leaves .testmondata stale for that run --
# the same conservative state build_impact_tier_plan already treats as a
# plain cache miss -- so it degrades cache freshness only, never gate
# correctness.
_KNOWN_TESTMON_EXTENSIONLESS_CRASH_MARKERS = (
    "testmon_core.py",
    'rsplit(".", 1)',
    "IndexError: list index out of range",
)


def classify_cache_warm_outcome(returncode: int, output: str) -> bool:
    """True if a `pytest bin/ --testmon-noselect` cache-warm run should be
    treated as a successful warm; False if it must propagate as a real
    failure. See the module-level comment above for the exact defect this
    carves out and why it is safe to tolerate.
    """
    if returncode == 0:
        return True
    if not all(marker in output for marker in _KNOWN_TESTMON_EXTENSIONLESS_CRASH_MARKERS):
        return False
    if re.search(r"^\d+ failed", output, re.MULTILINE):
        return False
    if re.search(r"^\d+ error", output, re.MULTILINE):
        return False
    return bool(re.search(r"\d+ passed", output))


def run_cache_warm(repo_root: Path) -> int:
    """Execute the opt-in full-suite local cache warm and classify its result.

    Returns 0 for a real pass or a tolerated known crash, and the real
    returncode for anything else.

    The behavior is integration-tested against a minimal crash-capable
    fixture; ordinary test collection never recursively runs this repo's
    complete suite.
    """
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "bin/", "-q", "-p", "no:cacheprovider",
            "--testmon-noselect",
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    sys.stdout.write(output)
    if classify_cache_warm_outcome(result.returncode, output):
        if result.returncode != 0:
            print(
                "tier_selection: KNOWN pytest-testmon extensionless-file crash "
                "tolerated during cache warm (all tests passed) -- .testmondata "
                "left stale for this run",
                file=sys.stderr,
            )
        return 0
    return result.returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tier_selection.py",
        description="Conservative pytest-testmon/pytest-impact gate selector.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--cache-warm", action="store_true",
        help="execute the full suite once to refresh .testmondata",
    )
    mode.add_argument(
        "--diff-cover-scope", metavar="COMPARE_REF",
        help="print the safe coverage scope for COMPARE_REF",
    )
    parser.add_argument(
        "--base", default="HEAD",
        help="comparison ref for impact selection (default: HEAD)",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    args = build_parser().parse_args(argv)
    if args.cache_warm:
        return run_cache_warm(repo_root)
    if args.diff_cover_scope:
        return print_diff_cover_scope(repo_root, args.diff_cover_scope)
    return run_impact_tier(repo_root, args.base)


if __name__ == "__main__":
    raise SystemExit(main())
