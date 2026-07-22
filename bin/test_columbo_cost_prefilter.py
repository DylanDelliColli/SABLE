#!/usr/bin/env python3
"""Unit tests for bin/columbo-cost-prefilter.py (SABLE-cmar4.6).

Pure logic over synthetic duration/coverage data -- no subprocess calls,
no real coverage.py collection. The real end-to-end pipeline (real pytest
run, real coverage db, real shell suite timing) is exercised with no
mocking in test_columbo_cost_prefilter_integration.py.

Plant-and-fail verdict (dispatch note requirement, recorded here rather
than only in the session summary): test_slow_uniquely_covering_test_is_
never_proposed is the guard the dispatch flagged as most likely to be
quietly weakened into an always-passing assertion. It was verified to
bite by temporarily mutating rank_python_tests' subsumption condition
from `len(unique) == 0` to `len(unique) <= 1` (an off-by-one that would
let a test with exactly one unique line slip through) and re-running this
file: the mutation turned this test red (the planted test's synthetic
unique line count is 1), confirming the assertion is load-bearing. The
mutation was reverted before this file was committed.

REVISE PASS (SABLE-cmar4.6, second dispatch) -- two more findings, both
verified by actually running the mutation, not by inspection alone:

1. test_equal_duration_tests_do_not_subsume_each_other's original fixture
   used DISJOINT coverage (t_a -> m.py:1, t_b -> m.py:2), a shape neither
   tie handling nor its absence can ever fail on -- disjoint sets can't
   subsume each other regardless of iteration order, so the test protected
   nothing. It was rebuilt with OVERLAPPING coverage (t_b's set a strict
   subset of t_a's) and the negative control was run for real: reverting
   rank_python_tests to the pre-fix `sorted(durations.items(), key=(duration,
   nodeid))` + single-pass-accumulation logic turns it red (t_b's
   unique_count drops from 1 to 0, subsumed flips to True) -- confirmed by
   temporarily reinstating that logic and re-running this file, then
   reverting. This is what exposed the original bug: rank_python_tests'
   docstring promised equal-duration ties never subsume each other, but the
   code broke ties by nodeid and accumulated as it went, so the
   alphabetically-earlier of two tied tests always "won". Fixed by
   processing equal-duration tests in bands: every test in a band computes
   `unique` against the `faster_union` as it stood before the band started,
   and the band's coverage is folded in only after the whole band is
   scored -- see rank_python_tests' docstring.

2. Defect-3 audit -- asked of every test in this file: "would this fixture
   still pass if the subtraction (`covered - faster_union`) rank_python_tests
   performs were deleted outright (replaced with `unique = covered`)?" Run
   for real (mutate, run, revert), not reasoned about in the abstract.
   FOUR tests catch full deletion of the subtraction:
   test_slow_uniquely_covering_test_is_never_proposed,
   test_genuinely_subsumed_slow_test_is_proposed,
   test_python_pruning_candidates_never_includes_nonempty_unique (the one
   the dispatch flagged for particular attention), and
   test_format_text_flags_prune_candidates. TWO tests pass either way, and
   that's inherent to what they check rather than a gap:
   test_fastest_test_is_never_trivially_subsumed_when_it_has_coverage and
   test_zero_coverage_test_is_trivially_subsumed both put their subject test
   in the position where `faster_union` is still empty (fastest-of-all, or
   zero-coverage so nothing to subtract from), so `covered - {}` and
   `covered` are the same value -- these tests exercise real, distinct edge
   cases (no faster peers exist; empty coverage is trivially subsumed) but
   cannot, by their fixture's own shape, distinguish "subtraction happened"
   from "subtraction was skipped". Not fixed: they are testing what their
   docstrings say they test, and a third check on the same axis as the four
   above would be redundant. The remaining tests in this file
   (parse_pytest_durations coverage, ranking-order, shell-half,
   build_report/format sanity) each name a different property and were
   checked against deletion of the mechanism they name, not the subsumption
   subtraction -- no further gaps found.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
PREFILTER_PATH = SCRIPT_DIR / "columbo-cost-prefilter.py"
spec = importlib.util.spec_from_file_location("columbo_cost_prefilter", PREFILTER_PATH)
ccp = importlib.util.module_from_spec(spec)
sys.modules["columbo_cost_prefilter"] = ccp
spec.loader.exec_module(ccp)


# ---------------------------------------------------------------------------
# parse_pytest_durations
# ---------------------------------------------------------------------------


def test_parse_durations_sums_setup_call_teardown_per_nodeid():
    text = (
        "0.01s setup    bin/test_foo.py::test_a\n"
        "0.02s call     bin/test_foo.py::test_a\n"
        "0.00s teardown bin/test_foo.py::test_a\n"
        "0.05s call     bin/test_foo.py::test_b\n"
    )
    durations = ccp.parse_pytest_durations(text)
    assert durations["bin/test_foo.py::test_a"] == pytest.approx(0.03)
    assert durations["bin/test_foo.py::test_b"] == pytest.approx(0.05)


def test_parse_durations_ignores_non_duration_lines():
    text = (
        "============ test session starts ============\n"
        "collected 2 items\n"
        "0.01s call     bin/test_foo.py::test_a\n"
        "2 passed in 0.10s\n"
    )
    durations = ccp.parse_pytest_durations(text)
    assert durations == {"bin/test_foo.py::test_a": pytest.approx(0.01)}


def test_parse_durations_empty_report():
    assert ccp.parse_pytest_durations("") == {}


# ---------------------------------------------------------------------------
# rank_python_tests / python_pruning_candidates -- subsumption core
# ---------------------------------------------------------------------------


def _line(f, n):
    return (f, n)


def test_slow_uniquely_covering_test_is_never_proposed():
    """NEGATIVE CONTROL: the slowest test in the set covers one line no
    other (faster) test covers. It must never appear as a pruning
    candidate, no matter how slow it is relative to everything else."""
    durations = {
        "t_fast_a": 0.01,
        "t_fast_b": 0.02,
        "t_slow_unique": 10.0,
    }
    coverage_map = {
        "t_fast_a": {_line("m.py", 1), _line("m.py", 2)},
        "t_fast_b": {_line("m.py", 1), _line("m.py", 3)},
        # covers everything the fast tests cover, PLUS one line nobody
        # else touches -- genuinely irreplaceable despite being 500x slower.
        "t_slow_unique": {_line("m.py", 1), _line("m.py", 2), _line("m.py", 3), _line("m.py", 99)},
    }
    records = ccp.rank_python_tests(durations, coverage_map)
    candidates = ccp.python_pruning_candidates(records)
    candidate_ids = {c["nodeid"] for c in candidates}
    assert "t_slow_unique" not in candidate_ids

    slow_record = next(r for r in records if r["nodeid"] == "t_slow_unique")
    assert slow_record["unique_count"] == 1
    assert slow_record["subsumed"] is False


def test_genuinely_subsumed_slow_test_is_proposed():
    """POSITIVE CONTROL: a slow test whose entire coverage is a strict
    subset of what strictly-faster tests already cover MUST be proposed --
    otherwise the guard above proves nothing (a checker that never
    proposes anything also passes the negative control trivially)."""
    durations = {
        "t_fast_a": 0.01,
        "t_fast_b": 0.02,
        "t_slow_redundant": 10.0,
    }
    coverage_map = {
        "t_fast_a": {_line("m.py", 1), _line("m.py", 2)},
        "t_fast_b": {_line("m.py", 3), _line("m.py", 4)},
        # every line t_slow_redundant touches is already covered by a
        # strictly faster test -- zero unique contribution.
        "t_slow_redundant": {_line("m.py", 1), _line("m.py", 3)},
    }
    records = ccp.rank_python_tests(durations, coverage_map)
    candidates = ccp.python_pruning_candidates(records)
    candidate_ids = {c["nodeid"] for c in candidates}
    assert "t_slow_redundant" in candidate_ids

    slow_record = next(r for r in records if r["nodeid"] == "t_slow_redundant")
    assert slow_record["unique_count"] == 0
    assert slow_record["subsumed"] is True


def test_fastest_test_is_never_trivially_subsumed_when_it_has_coverage():
    """The fastest test has no faster tests to be subsumed by -- its own
    coverage is definitionally its full unique contribution."""
    durations = {"t_only_fast": 0.01, "t_slower": 1.0}
    coverage_map = {
        "t_only_fast": {_line("m.py", 1)},
        "t_slower": {_line("m.py", 1)},
    }
    records = ccp.rank_python_tests(durations, coverage_map)
    fastest = next(r for r in records if r["nodeid"] == "t_only_fast")
    assert fastest["unique_count"] == 1
    assert fastest["subsumed"] is False


def test_zero_coverage_test_is_trivially_subsumed():
    """A test with no recorded coverage at all (e.g. it never touched
    instrumented code) has an empty unique set by construction -- flagged
    as a candidate, which is correct under this tool's narrow lens (it
    measures code-coverage contribution only; the caller still reviews
    the bead before deleting anything)."""
    durations = {"t_fast": 0.01, "t_no_coverage": 5.0}
    coverage_map = {"t_fast": {_line("m.py", 1)}}
    records = ccp.rank_python_tests(durations, coverage_map)
    no_cov = next(r for r in records if r["nodeid"] == "t_no_coverage")
    assert no_cov["covered_count"] == 0
    assert no_cov["subsumed"] is True


def test_equal_duration_tests_do_not_subsume_each_other():
    """Ties are not 'faster than': t_b's coverage is a strict SUBSET of
    t_a's, the only fixture shape that can actually exercise this
    guarantee (disjoint coverage can never subsume regardless of tie
    handling). Under the naive `sorted(durations.items(), key=(duration,
    nodeid))` + single-pass-accumulation bug this replaces, "t_a" sorts
    before "t_b" alphabetically, enters `faster_union` first, and t_b's
    subset coverage then reads as fully subsumed -- exactly the
    arbitrary-tie-order outcome this test guards against. Negative control
    verified by hand: reverting rank_python_tests to that naive
    implementation turns this test red (t_b's unique_count becomes 0,
    subsumed becomes True) -- see SABLE-cmar4.6 revise note."""
    durations = {"t_a": 1.0, "t_b": 1.0}
    coverage_map = {
        "t_a": {_line("m.py", 1), _line("m.py", 2)},
        "t_b": {_line("m.py", 1)},
    }
    records = ccp.rank_python_tests(durations, coverage_map)
    by_id = {r["nodeid"]: r for r in records}
    assert by_id["t_a"]["unique_count"] == 2
    assert by_id["t_a"]["subsumed"] is False
    assert by_id["t_b"]["unique_count"] == 1
    assert by_id["t_b"]["subsumed"] is False


def test_ranked_output_sorted_slowest_first():
    durations = {"t_a": 0.5, "t_b": 5.0, "t_c": 0.1}
    coverage_map = {k: {_line("m.py", i)} for i, k in enumerate(durations)}
    records = ccp.rank_python_tests(durations, coverage_map)
    assert [r["nodeid"] for r in records] == ["t_b", "t_a", "t_c"]


def test_python_pruning_candidates_never_includes_nonempty_unique():
    """Structural invariant, checked directly against the filter function
    (not just via the specific fixtures above): every returned candidate
    has unique_count == 0."""
    durations = {"t_a": 0.1, "t_b": 0.2, "t_c": 0.3}
    coverage_map = {
        "t_a": {_line("m.py", 1)},
        "t_b": {_line("m.py", 1)},          # fully subsumed by t_a
        "t_c": {_line("m.py", 1), _line("m.py", 2)},  # covers a new line
    }
    records = ccp.rank_python_tests(durations, coverage_map)
    candidates = ccp.python_pruning_candidates(records)
    assert all(c["unique_count"] == 0 for c in candidates)
    assert {c["nodeid"] for c in candidates} == {"t_b"}


# ---------------------------------------------------------------------------
# rank_shell_suites -- advisory only, structurally cannot look proven
# ---------------------------------------------------------------------------


def test_shell_suites_ranked_by_duration_descending():
    durations = {"suite_a.sh": 1.0, "suite_b.sh": 5.0, "suite_c.sh": 0.5}
    records = ccp.rank_shell_suites(durations)
    assert [r["suite"] for r in records] == ["suite_b.sh", "suite_a.sh", "suite_c.sh"]


def test_shell_suites_always_marked_advisory():
    records = ccp.rank_shell_suites({"suite_a.sh": 1.0})
    assert records[0]["advisory"] is True


def test_shell_suites_never_carry_a_subsumed_or_prune_field():
    """The shell half must be structurally incapable of looking like a
    proven prune -- no 'subsumed', 'pruning_candidate', or 'unique_count'
    key anywhere in a shell record, even though the python record shape
    (rank_python_tests) has exactly these fields."""
    records = ccp.rank_shell_suites({"suite_a.sh": 1.0, "suite_b.sh": 2.0})
    forbidden_keys = {"subsumed", "pruning_candidate", "unique_count", "covered_count"}
    for r in records:
        assert forbidden_keys.isdisjoint(r.keys())


# ---------------------------------------------------------------------------
# build_report -- assembly + shell advisory_only invariant
# ---------------------------------------------------------------------------


def test_build_report_shell_section_always_advisory_only():
    python_records = ccp.rank_python_tests(
        {"t_a": 0.1}, {"t_a": {_line("m.py", 1)}}
    )
    shell_records = ccp.rank_shell_suites({"suite_a.sh": 1.0})
    report = ccp.build_report(python_records, shell_records)
    assert report["shell"]["advisory_only"] is True
    assert "pruning_candidates" not in report["shell"]


def test_build_report_python_pruning_candidates_subset_of_ranked():
    python_records = ccp.rank_python_tests(
        {"t_a": 0.1, "t_b": 0.2},
        {"t_a": {_line("m.py", 1)}, "t_b": {_line("m.py", 1)}},
    )
    shell_records = ccp.rank_shell_suites({})
    report = ccp.build_report(python_records, shell_records)
    ranked_ids = {r["nodeid"] for r in report["python"]["ranked"]}
    candidate_ids = {r["nodeid"] for r in report["python"]["pruning_candidates"]}
    assert candidate_ids <= ranked_ids


# ---------------------------------------------------------------------------
# format_json / format_text -- basic shape sanity
# ---------------------------------------------------------------------------


def test_format_json_round_trips():
    import json

    report = ccp.build_report(
        ccp.rank_python_tests({"t_a": 0.1}, {"t_a": {_line("m.py", 1)}}),
        ccp.rank_shell_suites({"suite_a.sh": 1.0}),
    )
    parsed = json.loads(ccp.format_json(report))
    assert parsed["shell"]["advisory_only"] is True
    assert parsed["python"]["ranked"][0]["nodeid"] == "t_a"


def test_format_text_flags_prune_candidates():
    report = ccp.build_report(
        ccp.rank_python_tests(
            {"t_a": 0.1, "t_b": 0.2},
            {"t_a": {_line("m.py", 1)}, "t_b": {_line("m.py", 1)}},
        ),
        ccp.rank_shell_suites({}),
    )
    text = ccp.format_text(report)
    assert "PRUNE-CANDIDATE" in text
    assert "ADVISORY" in text.upper()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
