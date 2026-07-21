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


def main(argv: Optional[List[str]] = None) -> int:
    repo_root = Path(__file__).resolve().parent.parent
    base_ref = "HEAD"
    args = argv if argv is not None else sys.argv[1:]
    for arg in args:
        if arg.startswith("--base="):
            base_ref = arg.split("=", 1)[1]
    return run_impact_tier(repo_root, base_ref)


if __name__ == "__main__":
    raise SystemExit(main())
