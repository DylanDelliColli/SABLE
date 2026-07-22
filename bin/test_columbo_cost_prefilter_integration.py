#!/usr/bin/env python3
"""Integration tests for bin/columbo-cost-prefilter.py (SABLE-cmar4.6).

Real pytest + real coverage.py collection against a real slice of THIS
repo's test tree (bin/test_columbo_prefilter.py, 58 tests over
columbo-prefilter.py, plus bin/test_sable_fixture_tripwire_integration.py,
6 tests over sable-fixture-tripwire), and real wall-clock timing of two
real, fast hooks/test/*.sh suites. No mocked coverage, no mocked
subprocess, no synthetic fixture repo -- per the dispatch note, mocking
coverage here would defeat the entire bead.

Scope note: the python targets are two real, fast (~1s combined) suites
rather than the full bin/ tree (which takes several minutes and, per
SABLE-cmar4.3, has an unrelated pytest-testmon crash class on this repo's
extensionless executables -- irrelevant here since this tool uses plain
pytest-cov, not testmon, but the full-suite runtime alone would make this
test too slow for routine use). "Real coverage data against the real repo
test tree" is satisfied by running real tests against real source with a
real coverage.py context db -- the scope restriction only bounds
wall-clock cost, not realism.

REVISE PASS (SABLE-cmar4.6, second dispatch): the second target
(test_sable_fixture_tripwire_integration.py) was added because
bin/test_columbo_prefilter.py's real durations, at the 2-decimal
precision pytest's textual `--durations` report emits, ALL round to
0.00s -- every one of its 58 tests ties. Once rank_python_tests'
equal-duration-band fix landed (see test_columbo_cost_prefilter.py's
module docstring), a single all-tied corpus produces ZERO cross-test
subtraction by construction (there is no strictly-faster band for any of
them to be subtracted against), which starved
test_real_pipeline_subtraction_actually_uses_the_overlap of the real
non-degenerate duration spread it needs to prove the subtraction
mechanism actually fires on real data. This was caught by actually
running the suite against real data, not assumed -- see that test's
history. test_sable_fixture_tripwire_integration.py's 6 tests span
0.04s-0.33s (real subprocess calls to git/bash), giving the combined
corpus genuine duration separation.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util

SCRIPT_DIR = Path(__file__).resolve().parent
PREFILTER_PATH = SCRIPT_DIR / "columbo-cost-prefilter.py"
spec = importlib.util.spec_from_file_location("columbo_cost_prefilter", PREFILTER_PATH)
ccp = importlib.util.module_from_spec(spec)
sys.modules["columbo_cost_prefilter"] = ccp
spec.loader.exec_module(ccp)

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Python half: real pytest + real coverage.py against a real repo suite
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_python_run(tmp_path_factory):
    """One real pytest run over two real repo test files with real
    per-test coverage contexts, shared across the python-half assertions
    below so the (real, non-trivial) subprocess cost is paid once.
    test_sable_fixture_tripwire_integration.py is included alongside
    test_columbo_prefilter.py specifically for its real duration spread
    (0.04s-0.33s) -- see module docstring."""
    coverage_file = tmp_path_factory.mktemp("cost-prefilter-cov") / ".coverage"
    durations, coverage_map = ccp.run_python_suite_with_coverage(
        REPO_ROOT,
        ["bin/test_columbo_prefilter.py", "bin/test_sable_fixture_tripwire_integration.py"],
        ["bin"],
        coverage_file,
    )
    return durations, coverage_map


def test_real_run_collects_durations_and_coverage_for_known_tests(real_python_run):
    durations, coverage_map = real_python_run
    known = "bin/test_columbo_prefilter.py::test_argparse_help"
    assert known in durations
    assert durations[known] >= 0.0
    assert known in coverage_map
    assert len(coverage_map[known]) > 0
    # real coverage.py filenames are absolute paths to real source. --cov=bin
    # measures the whole bin/ dir (including the test file that's running),
    # so check the covered set as a whole rather than an arbitrary element --
    # set iteration order is not deterministic across processes.
    covered_files = {f for f, _lineno in coverage_map[known]}
    assert any(f.endswith("columbo-prefilter.py") for f in covered_files)
    for f in covered_files:
        assert Path(f).exists()


def test_real_coverage_has_genuine_overlap_between_tests(real_python_run):
    """Proves the coverage data is real and overlapping (not an artifact
    of each test living in its own island) -- every test in this suite
    imports and exercises shared top-level module code, so the sum of
    per-test coverage sizes must exceed the distinct union. A bug in the
    context-parsing pipeline (e.g. accidentally keying by phase instead
    of nodeid, or failing to strip the '|run' suffix) would silently
    collapse this overlap to near-zero or blow it up to nonsense; this
    assertion would catch either."""
    _durations, coverage_map = real_python_run
    total = sum(len(v) for v in coverage_map.values())
    union = set()
    for v in coverage_map.values():
        union |= v
    assert len(coverage_map) > 10, "expected the full combined test suite to have run"
    assert total > len(union), (
        f"expected real overlapping coverage (sum={total} should exceed "
        f"distinct union={len(union)}) -- got no overlap, which would mean "
        f"the context parsing is broken"
    )


def test_real_pipeline_produces_well_formed_ranked_report(real_python_run):
    durations, coverage_map = real_python_run
    records = ccp.rank_python_tests(durations, coverage_map)
    assert len(records) == len(durations)
    # slowest-first ordering
    for a, b in zip(records, records[1:]):
        assert a["duration"] >= b["duration"]


def test_real_pipeline_zero_candidates_have_nonempty_unique_coverage(real_python_run):
    """The bead's mandated invariant, checked against REAL data rather
    than only the synthetic unit fixtures: every python pruning candidate
    the real pipeline emits has unique_count == 0. Not vacuous -- this
    exercises the real subtraction against real overlapping coverage sets
    built from a real coverage.py db, not hand-typed test doubles."""
    durations, coverage_map = real_python_run
    records = ccp.rank_python_tests(durations, coverage_map)
    candidates = ccp.python_pruning_candidates(records)
    assert all(c["unique_count"] == 0 for c in candidates)


def test_real_pipeline_subtraction_actually_uses_the_overlap(real_python_run):
    """Stronger than the trivial invariant above: at least one real test
    has covered_count > unique_count, proving faster-test coverage was
    genuinely subtracted from a real test's set rather than unique always
    trivially equalling covered (which would happen if faster_union were
    never actually populated -- e.g. a bug that reset it every iteration)."""
    durations, coverage_map = real_python_run
    records = ccp.rank_python_tests(durations, coverage_map)
    assert any(r["covered_count"] > r["unique_count"] for r in records)


# ---------------------------------------------------------------------------
# Shell half: real wall-clock timing, advisory-only, real repo suites
# ---------------------------------------------------------------------------

_FAST_REAL_SUITES = [
    "hooks/test/test-columbo-quick-mode.sh",
    "hooks/test/test-sable-plan-tiers.sh",
]


@pytest.fixture(scope="module")
def real_shell_run():
    for rel in _FAST_REAL_SUITES:
        assert (REPO_ROOT / rel).is_file(), f"fixture suite missing: {rel}"
    durations = {
        suite: ccp.measure_shell_suite_duration(REPO_ROOT / suite, cwd=REPO_ROOT, timeout=60)
        for suite in _FAST_REAL_SUITES
    }
    return durations


def test_real_shell_suites_measured_with_positive_duration(real_shell_run):
    for suite in _FAST_REAL_SUITES:
        assert real_shell_run[suite] > 0.0


def test_real_shell_suites_are_advisory_only_never_a_proven_prune(real_shell_run):
    records = ccp.rank_shell_suites(real_shell_run)
    assert len(records) == len(_FAST_REAL_SUITES)
    forbidden_keys = {"subsumed", "pruning_candidate", "unique_count", "covered_count"}
    for r in records:
        assert r["advisory"] is True
        assert forbidden_keys.isdisjoint(r.keys())


# ---------------------------------------------------------------------------
# End-to-end: build_report over real python + real shell data together
# ---------------------------------------------------------------------------


def test_end_to_end_report_shell_section_stays_advisory_only(real_python_run, real_shell_run):
    durations, coverage_map = real_python_run
    python_records = ccp.rank_python_tests(durations, coverage_map)
    shell_records = ccp.rank_shell_suites(real_shell_run)
    report = ccp.build_report(python_records, shell_records)

    assert report["shell"]["advisory_only"] is True
    assert "pruning_candidates" not in report["shell"]
    assert all(r["unique_count"] == 0 for r in report["python"]["pruning_candidates"])

    # text and JSON formatters both handle the real, non-trivial report
    text = ccp.format_text(report)
    assert "PRUNE-CANDIDATE" in text or "python pruning candidates: 0" in text
    assert "ADVISORY" in text.upper()

    import json
    parsed = json.loads(ccp.format_json(report))
    assert parsed["shell"]["advisory_only"] is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
