#!/usr/bin/env python3
"""columbo-cost-prefilter — static-analysis test-cost ranker (SABLE-cmar4.6).

Companion to columbo-prefilter.py's shallowness ranker: instead of ranking
tests by how thin they look, this ranks them by DURATION vs UNIQUE COVERAGE
CONTRIBUTED, and proposes pruning candidates only where that's provable.

Usage:
  bin/columbo-cost-prefilter.py                      # scan bin/ + hooks/test/, text output
  bin/columbo-cost-prefilter.py --json                # machine-readable
  bin/columbo-cost-prefilter.py --python-target bin/test_foo.py --python-target bin/test_bar.py
  bin/columbo-cost-prefilter.py --shell-suite hooks/test/test-a.sh --shell-suite hooks/test/test-b.sh
  bin/columbo-cost-prefilter.py --help

Two halves, two epistemic standards (bead spec, non-negotiable):

  PYTHON half — test granularity, coverage.py data. A real pytest run is
  driven with `--cov-context=test`, which tags every measured line with the
  nodeid of the test that executed it. Tests are ordered fastest-first; a
  test is a PRUNING CANDIDATE only when every line it covers is *already*
  covered by a strictly faster test (its "unique contribution" is the empty
  set). This is a structural guarantee, not a threshold: `subsumed` is
  defined as `unique_count == 0`, so a test with ANY unique coverage can
  never be proposed, no matter how slow it is or how small its unique set.

  SHELL half — SUITE granularity, duration only. There is no line-coverage
  tool wired for the shell suites (kcov/ShellSpec rewrite rejected — cmar4
  TEST-STRATEGY gate, S4.5 residual), so "fully subsumed" is UNPROVABLE
  here. Shell suites are ranked by wall-clock duration alone and marked
  `advisory_only=True`. This tool NEVER computes or emits a `subsumed` /
  `pruning_candidate` field for a shell suite, and the caller (Columbo's
  cost-audit mode) must never auto-file a shell suite as a prune candidate
  from this data — advisory ranking only, human judgment required.

Coverage-context format (confirmed against a live pytest-cov 7.1.0 / py
coverage 7.15.2 capture, not guessed): pytest-cov's dynamic `test` context
writes ONE flat context per test, `"<nodeid>|run"` — not split into
per-phase (setup/call/teardown) contexts. The `|run` suffix is stripped to
recover the nodeid, which is joined against the `--durations` report's
nodeid column (same format: `bin/test_foo.py::test_bar`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Python half — duration parsing
# ---------------------------------------------------------------------------

# Matches one line of `pytest --durations=0 --durations-min=0.0` output,
# e.g. "0.12s call     bin/test_foo.py::test_bar". Three lines (setup, call,
# teardown) are emitted per test; costs are summed per nodeid below since
# fixture setup cost is part of what pruning the test would actually save.
_DURATION_LINE = re.compile(r"^\s*([\d.]+)s\s+(setup|call|teardown)\s+(.+?)\s*$")


def parse_pytest_durations(text: str) -> dict[str, float]:
    """Parse a pytest `--durations=0 --durations-min=0.0` textual report,
    summing setup+call+teardown per node id into one total-cost figure."""
    durations: dict[str, float] = {}
    for line in text.splitlines():
        m = _DURATION_LINE.match(line)
        if not m:
            continue
        dur, _phase, nodeid = m.groups()
        durations[nodeid] = durations.get(nodeid, 0.0) + float(dur)
    return durations


# ---------------------------------------------------------------------------
# Python half — per-test coverage
# ---------------------------------------------------------------------------


def load_python_test_coverage(coverage_data_file: Path) -> dict[str, set[tuple[str, int]]]:
    """Load a coverage.py sqlite db collected with `--cov-context=test` and
    return {test_nodeid: {(filename, lineno), ...}}.

    The empty context ("" — lines executed outside any test context, e.g.
    module import at collection time) is not attributable to a single test
    and is dropped.
    """
    from coverage import CoverageData  # local import: optional dependency

    data = CoverageData(basename=str(coverage_data_file))
    data.read()
    coverage_map: dict[str, set[tuple[str, int]]] = {}
    for filename in data.measured_files():
        contexts_by_line = data.contexts_by_lineno(filename)
        for lineno, contexts in contexts_by_line.items():
            for ctx in contexts:
                if not ctx:
                    continue
                nodeid = ctx.rsplit("|", 1)[0]
                coverage_map.setdefault(nodeid, set()).add((filename, lineno))
    return coverage_map


# Exit codes that mean the inner pytest run itself functioned -- durations
# and coverage were genuinely produced -- even if the run's verdict wasn't a
# clean pass. 0 (all tests passed) and 1 (some tests failed) both still walk
# every test body and emit a real --durations report and real per-test
# coverage contexts, so this tool's duration/coverage measurement is valid
# either way. Anything else (2 interrupted, 3 internal error -- the exact
# INTERNALERROR class that crashed this run in ci-verify run 29936760714 and
# 29940963862, 4 usage error, 5 no tests collected) means the run did NOT
# produce trustworthy output, and treating its (empty) stdout as "zero slow
# tests" would be exactly the silent-empty-result failure SABLE-cmar4.7 and
# SABLE-cmar4.8 exist to prevent (SABLE-cmar4.6 third revise).
_INNER_RUN_OK_EXIT_CODES = frozenset({0, 1})


class InnerPytestRunFailed(RuntimeError):
    """Raised when run_python_suite_with_coverage's inner pytest invocation
    exits with a code outside _INNER_RUN_OK_EXIT_CODES -- i.e. the run
    itself is suspect (crashed, was interrupted, hit a usage error, or
    collected nothing), not merely "some tests failed". Before this existed,
    that condition silently produced ({}, {}) -- a returncode/stderr that was
    never inspected (verified absent at 67e3a13, af95ffa, and cdbf58e) --
    which is precisely why three ci-verify reds (runs 29936760714 and
    29940963862 for this same empty-durations symptom) were undiagnosable
    from CI's own output. Carries the returncode plus a bounded stdout/stderr
    tail so the real cause prints instead of a bare KeyError/AssertionError
    against an empty dict downstream."""

    _TAIL_LINES = 40

    def __init__(self, returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        stdout_tail = "\n".join(stdout.splitlines()[-self._TAIL_LINES:])
        stderr_tail = "\n".join(stderr.splitlines()[-self._TAIL_LINES:])
        super().__init__(
            f"inner pytest run exited {returncode} (not in "
            f"{sorted(_INNER_RUN_OK_EXIT_CODES)} -- run did not produce "
            f"trustworthy duration/coverage output)\n"
            f"--- stdout tail ({self._TAIL_LINES} lines) ---\n{stdout_tail}\n"
            f"--- stderr tail ({self._TAIL_LINES} lines) ---\n{stderr_tail}"
        )


def run_python_suite_with_coverage(
    repo_root: Path,
    test_targets: list[str],
    cov_targets: list[str],
    coverage_data_file: Path,
) -> tuple[dict[str, float], dict[str, set[tuple[str, int]]]]:
    """Real pytest invocation: collects per-test duration AND per-test line
    coverage (pytest-cov dynamic contexts) in a single run, so callers never
    have to run the target suite twice to get both signals.

    Isolated from ambient pytest plugins that have no business in a
    duration/coverage measurement run (SABLE-cmar4.6 second revise,
    confirmed against the real ci-verify red -- run 29936760714, not
    guessed). Two independent findings, both verified live in this repo:

    1. The disable spelling matters. pytest-testmon registers as a pytest11
       entry point under the name `pytest-testmon` (NOT the module name
       `testmon`) -- `-p no:testmon` is silently a no-op: `--trace-config`
       still shows `testmon.pytest_testmon` registered with that spelling,
       identically to passing no `-p` flag at all. Only `-p no:pytest-testmon`
       actually removes it (confirmed: with it present, even an explicit
       `--testmon-noselect` becomes an "unrecognized arguments" error,
       because the plugin whose `pytest_addoption` would have registered
       that flag never loads).

    2. Why exclusion is necessary at all, not just cosmetic: this run passes
       no --testmon* flag of its own, so testmon's OWN CLI-gated
       collection/selection hooks (testmon/configure.py's
       `_get_notestmon_reasons`) should stay inert here in isolation -- and
       do, when this command runs alone. But pytest-testmon and pytest-cov
       both drive a single process-wide `coverage.Coverage` instance, and
       the real ci-verify gate's full `bin/` session ALSO collects
       test_tier_selection_integration.py::
       test_real_repo_full_suite_testmon_noselect_crash_is_tolerated, which
       spawns a real `--testmon-noselect` subprocess against this same repo
       root that re-collects and re-runs this suite's own tests. Reproduced
       directly: forcing `--testmon-noselect` onto this exact command
       alongside its real `--cov-context=test --cov=bin` flags produces
       `coverage.exceptions.CoverageException: Cannot switch context,
       coverage is not started` on every test, and separately, pytest-testmon
       2.2.0's own extensionless-file bug (IndexError in
       testmon_core.py:93's `filename.rsplit(".", 1)[1]`) is the exact
       INTERNALERROR the CI log shows. Either failure mode collapses this
       function's stdout into something `parse_pytest_durations` matches
       nothing in, which is why the real gate saw `real_python_run = ({},
       {})` -- an empty duration dict and empty coverage map, not a raised
       exception, because this function never inspected returncode/stderr.
       `-p no:pytest-testmon` removes the shared-Coverage-instance conflict
       at its root by ensuring testmon can never attach here, regardless of
       what triggers it elsewhere in the same host session.

    `-p no:impact` (pytest-impact) is excluded for the same defense-in-depth
    reason even though no crash was attributed to it -- it has no business
    in a coverage-measurement run either, and costs nothing to exclude.

    Third revise (SABLE-cmar4.6/SABLE-cmar4.8): the two exclusions above were
    verified to fix the nested full-bin/-suite reproduction locally, but
    ci-verify went RED on the exact same empty-durations symptom AGAIN on the
    next push (run 29940963862, byte-identical to the first). The inner
    run's `result.returncode` and `result.stderr` were never inspected up to
    that point, so whatever is failing in CI's environment -- unconfirmed,
    that is the open question this instrument exists to answer -- was
    collapsing silently into a normal-looking ({}, {}) return instead of
    surfacing. See InnerPytestRunFailed and _INNER_RUN_OK_EXIT_CODES above:
    a non-tolerated returncode now raises with the real stdout/stderr tail
    instead of being swallowed."""
    env = dict(os.environ)
    env["COVERAGE_FILE"] = str(coverage_data_file)
    args = [
        sys.executable, "-m", "pytest", *test_targets, "-q",
        "--durations=0", "--durations-min=0.0",
        "--cov-context=test",
        *[f"--cov={t}" for t in cov_targets],
        "--cov-report=",
        "-p", "no:cacheprovider",
        "-p", "no:pytest-testmon",
        "-p", "no:impact",
    ]
    result = subprocess.run(args, cwd=repo_root, capture_output=True, text=True, env=env)
    if result.returncode not in _INNER_RUN_OK_EXIT_CODES:
        raise InnerPytestRunFailed(result.returncode, result.stdout, result.stderr)
    durations = parse_pytest_durations(result.stdout)
    coverage_map = load_python_test_coverage(coverage_data_file)
    return durations, coverage_map


# ---------------------------------------------------------------------------
# Python half — subsumption ranking
# ---------------------------------------------------------------------------


def rank_python_tests(
    durations: dict[str, float],
    coverage_map: dict[str, set[tuple[str, int]]],
) -> list[dict]:
    """Order tests fastest-first and compute each one's UNIQUE contribution
    -- the lines it covers that no strictly-faster test already covers.

    Equal-duration tests never subsume one another (strict `<` only): tests
    are processed in BANDS of equal duration, slowest-band-last. Every test
    in a band computes its unique contribution against `faster_union` as it
    stood BEFORE that band started -- i.e. against only strictly-faster
    tests -- and the band's own coverage is folded into `faster_union` only
    after the whole band has been scored. This makes the strict-`<` promise
    a property of the algorithm, not of dict/sort iteration order: two
    equal-duration tests can never subsume each other even when one's
    coverage happens to be a subset of the other's. Conservative by
    construction.

    Returns records sorted slowest-first (the order a human triaging cost
    wants to see), each: {nodeid, duration, covered_count, unique_count,
    subsumed}. `subsumed` is exactly `unique_count == 0` -- see module
    docstring for why that's the ONLY definition of a pruning candidate.
    """
    bands: dict[float, list[str]] = {}
    for nodeid, duration in durations.items():
        bands.setdefault(duration, []).append(nodeid)

    faster_union: set[tuple[str, int]] = set()
    records: list[dict] = []
    for duration in sorted(bands):
        band_nodeids = sorted(bands[duration])
        band_covered: dict[str, set[tuple[str, int]]] = {}
        for nodeid in band_nodeids:
            covered = coverage_map.get(nodeid, set())
            unique = covered - faster_union
            band_covered[nodeid] = covered
            records.append({
                "nodeid": nodeid,
                "duration": duration,
                "covered_count": len(covered),
                "unique_count": len(unique),
                "subsumed": len(unique) == 0,
            })
        for covered in band_covered.values():
            faster_union |= covered
    records.sort(key=lambda r: (-r["duration"], r["nodeid"]))
    return records


def python_pruning_candidates(records: list[dict]) -> list[dict]:
    """The ONLY python tests this tool will ever propose for pruning: those
    proven fully subsumed by strictly-faster tests. `unique_count == 0` is
    the sole gate -- there is no duration threshold, no score, nothing a
    caller could weaken into a heuristic. A test with any unique coverage,
    however slow, structurally cannot appear here."""
    return [r for r in records if r["subsumed"]]


# ---------------------------------------------------------------------------
# Shell half — duration-only, advisory
# ---------------------------------------------------------------------------


def measure_shell_suite_duration(
    suite_path: Path, cwd: Path, timeout: Optional[float] = None
) -> float:
    """Real wall-clock measurement of one shell suite. No coverage signal
    exists for this half (see module docstring) -- duration is the only
    input rank_shell_suites has to work with."""
    start = time.monotonic()
    subprocess.run(
        ["bash", str(suite_path)], cwd=cwd, capture_output=True, timeout=timeout
    )
    return time.monotonic() - start


def rank_shell_suites(durations: dict[str, float]) -> list[dict]:
    """Duration-ranked ADVISORY list. Every record carries `advisory=True`
    and NO OTHER FIELD -- in particular no `subsumed` / `pruning_candidate`
    key -- so a caller cannot accidentally treat a shell entry as a proven
    prune the way a python `subsumed=True` entry is. 'Fully subsumed' is
    unprovable at suite granularity (accepted residual, cmar4 S4.5); this
    function structurally cannot emit anything that looks like a proof."""
    records = [{"suite": name, "duration": d, "advisory": True} for name, d in durations.items()]
    records.sort(key=lambda r: (-r["duration"], r["suite"]))
    return records


# ---------------------------------------------------------------------------
# Report assembly + output formatting
# ---------------------------------------------------------------------------


def build_report(
    python_records: list[dict],
    shell_records: list[dict],
) -> dict:
    return {
        "python": {
            "ranked": python_records,
            "pruning_candidates": python_pruning_candidates(python_records),
        },
        "shell": {
            "ranked": shell_records,
            "advisory_only": True,
        },
    }


def format_json(report: dict) -> str:
    return json.dumps(report, indent=2)


def format_text(report: dict) -> str:
    lines: list[str] = []
    lines.append("== python (test granularity, coverage-proven) ==")
    for r in report["python"]["ranked"]:
        flag = "PRUNE-CANDIDATE" if r["subsumed"] else "-"
        lines.append(
            f"{r['nodeid']:<70}  dur={r['duration']:.3f}s  "
            f"covered={r['covered_count']}  unique={r['unique_count']}  {flag}"
        )
    lines.append("")
    lines.append(
        f"python pruning candidates: {len(report['python']['pruning_candidates'])}"
    )
    lines.append("")
    lines.append("== shell (suite granularity, duration-only, ADVISORY) ==")
    for r in report["shell"]["ranked"]:
        lines.append(f"{r['suite']:<70}  dur={r['duration']:.3f}s  advisory")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _default_shell_suites(repo_root: Path) -> list[str]:
    test_dir = repo_root / "hooks" / "test"
    if not test_dir.is_dir():
        return []
    return sorted(
        str(p.relative_to(repo_root)) for p in test_dir.glob("test-*.sh")
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="columbo-cost-prefilter",
        description="Rank tests by duration vs unique coverage contributed.",
    )
    p.add_argument(
        "--python-target", action="append", dest="python_targets",
        help="Python test file/dir to run (repeatable). Default: bin/.",
    )
    p.add_argument(
        "--cov-target", action="append", dest="cov_targets",
        help="Coverage measurement scope for --cov (repeatable). Default: bin.",
    )
    p.add_argument(
        "--shell-suite", action="append", dest="shell_suites",
        help="Shell suite path to time (repeatable). Default: hooks/test/test-*.sh.",
    )
    p.add_argument(
        "--shell-timeout", type=float, default=None,
        help="Per-suite timeout in seconds for shell suite measurement.",
    )
    p.add_argument(
        "--coverage-file", default=None,
        help="Where to write the coverage.py data file (default: temp file).",
    )
    p.add_argument(
        "--repo-root", default=None,
        help="Repo root to run from (default: this script's grandparent dir).",
    )
    p.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve() if args.repo_root else REPO_ROOT
    python_targets = args.python_targets or ["bin/"]
    cov_targets = args.cov_targets or ["bin"]
    shell_suites = args.shell_suites or _default_shell_suites(repo_root)

    coverage_data_file = (
        Path(args.coverage_file)
        if args.coverage_file
        else Path(tempfile.mkdtemp(prefix="columbo-cost-prefilter-")) / ".coverage"
    )

    durations, coverage_map = run_python_suite_with_coverage(
        repo_root, python_targets, cov_targets, coverage_data_file
    )
    python_records = rank_python_tests(durations, coverage_map)

    shell_durations = {
        suite: measure_shell_suite_duration(
            repo_root / suite, cwd=repo_root, timeout=args.shell_timeout
        )
        for suite in shell_suites
    }
    shell_records = rank_shell_suites(shell_durations)

    report = build_report(python_records, shell_records)
    print(format_json(report) if args.json else format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
