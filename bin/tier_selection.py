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

.testmondata lifecycle -- the real decision in this bead (dispatch note,
optimus, SABLE-cmar4.3): CI runners are ephemeral, so the coverage map ships
as a GitHub Actions cache artifact keyed on ref+SHA with branch-prefix
restore-keys (see .github/workflows/ci-verify.yml) and pytest-testmon
incrementally updates it on every run. On a cache MISS -- no prior
.testmondata restored, e.g. a brand-new branch or an evicted cache -- this
module falls back to a conservative FULL run of bin/, and deliberately does
NOT consult pytest-impact for a partial selection in that case either: a
silently empty testmon map paired with a narrow fixture-only selector could
select close to nothing while "passing" by not running -- exactly the
silent-green failure class SABLE-7v3z and this epic exist to eliminate.
Cache miss means full run, full stop.
"""
from __future__ import annotations

import re
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


# --- .testmondata cache-warm classification (SABLE-cmar4.3 second revise) ----
# ci-verify.yml runs the FULL bin/ suite a second time with --testmon-noselect
# purely to keep .testmondata warm for this module's selector (see module
# docstring). pytest-testmon 2.2.0 has a real, reproduced defect: its own
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


def run_cache_warm(repo_root: Path, extra_pytest_args: Optional[List[str]] = None) -> int:
    """Actually execute the full bin/ suite with --testmon-noselect (mirrors
    ci-verify.yml's cache-warm step exactly) and apply
    classify_cache_warm_outcome to the result, returning 0 for a real pass or
    a tolerated known-crash, and the real returncode for anything else.

    extra_pytest_args exists only for this module's own integration test,
    which runs this function against bin/ from a test IN bin/ -- it needs
    --ignore=<this file> to avoid the nested pytest run recursing into
    itself. ci-verify.yml's real invocation passes none.
    """
    result = subprocess.run(
        [
            sys.executable, "-m", "pytest", "bin/", "-q", "-p", "no:cacheprovider",
            "--testmon-noselect", *(extra_pytest_args or []),
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


def main(argv: Optional[List[str]] = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    args = argv if argv is not None else sys.argv[1:]
    if "--cache-warm" in args:
        return run_cache_warm(repo_root)
    base_ref = "HEAD"
    for arg in args:
        if arg.startswith("--base="):
            base_ref = arg.split("=", 1)[1]
    return run_impact_tier(repo_root, base_ref)


if __name__ == "__main__":
    raise SystemExit(main())
