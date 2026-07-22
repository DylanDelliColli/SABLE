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

REVISE PASS #2 (SABLE-cmar4.6, ci-verify gate red on run 29936760714) --
test_run_python_suite_disables_ambient_testmon_and_impact_by_working_name
was added after a real (not local-only) gate failure traced to
pytest-testmon and pytest-cov contending for one process-wide
coverage.Coverage instance inside run_python_suite_with_coverage's inner
pytest invocation -- see that function's docstring in
columbo-cost-prefilter.py for the full mechanism, confirmed against the
actual CI log and reproduced locally via a forced --testmon-noselect
(produced `coverage.exceptions.CoverageException: Cannot switch context,
coverage is not started` on every test). Plant-and-fail verdict: the FIRST
fix attempt used `-p no:testmon`, which a direct `--trace-config`
experiment showed is a silent no-op (pytest-testmon's real entry-point
name is `pytest-testmon`, not `testmon`) -- an integration test that only
runs the tool end-to-end without a hostile ambient plugin forced on would
have stayed green against that no-op exactly as this repo's own
integration suite did locally before the real gate caught it, so this
unit test asserts the constructed argv directly. Verified falsifiable:
reverting to the no-op spelling (or omitting the disable flags entirely)
turns this test red; confirmed by temporarily doing so and re-running
this file, then reverting.

REVISE PASS #3 (SABLE-cmar4.6/SABLE-cmar4.8, ci-verify red AGAIN on run
29940963862, byte-identical to the first): revise #2's fix was verified
correct against the reproduction that was possible locally, but the same
empty-durations symptom recurred in CI on the very next push, and
`result.returncode` / `result.stderr` were never inspected -- a dead inner
run returned ({}, {}) exactly like a legitimately-quiet one. This is not
polish, it's the prerequisite for diagnosing the still-open CI-only
failure at all (see run_python_suite_with_coverage's docstring).
test_run_python_suite_raises_loud_on_non_tolerated_returncode adds the
guard; test_run_python_suite_tolerates_test_failures_and_bare_pass prove
the two codes that must NOT raise. Plant-and-fail verified by hand:
commenting out the `if result.returncode not in _INNER_RUN_OK_EXIT_CODES`
raise in columbo-cost-prefilter.py and re-running this file turns
test_run_python_suite_raises_loud_on_non_tolerated_returncode red
(fake_run's returncode=3 case then returns ({}, {}) instead of raising,
same as the bug this closes); reverted before commit.

REVISE PASS (SABLE-cmar4.7) -- the resolution-limit follow-on cmar4.6's
worker found and escalated: pytest's textual `--durations` report
hardcodes 2-decimal formatting, and this repo's real bin/ corpus ties at
that precision (every test reads "0.00s"), which put every test in ONE
duration band and made cross-test subsumption unconditionally empty --
indistinguishable, from the output alone, from a genuine "nothing is
redundant" finding. Two changes, both required (the bead's own framing:
option 2 is not optional even if option 1 lands):

1. Full-precision duration collection. run_python_suite_with_coverage now
   reads report.duration via an in-process pytest plugin
   (parse_duration_log) instead of parsing 2-decimal --durations text
   (parse_pytest_durations, retained -- see its docstring in
   columbo-cost-prefilter.py for why). VERIFIED, not assumed: a direct
   probe against this repo's real bin/test_columbo_prefilter.py (58 tests,
   all "0.00s" in --durations text) showed 58/58 DISTINCT full-precision
   durations, ranging ~0.0002s-0.0025s -- the hypothesis that pytest's
   report objects carry usable precision was checked before committing to
   it (per dispatch note), not taken on faith.
2. Degenerate-single-band signal. build_report now computes
   `python.degenerate_single_band` (helper: _degenerate_single_band) and,
   when true, a human-readable `python.note` -- so an empty
   pruning_candidates list can never be silently read as "measured, found
   nothing" when it actually means "every test tied; nothing was
   comparable". test_build_report_degenerate_signal_fires_on_all_tied_
   durations and its negative control
   test_build_report_degenerate_signal_absent_with_real_spread pin both
   directions; test_rank_python_tests_distinguishes_durations_below_
   2_decimal_precision pins that rank_python_tests itself (unchanged --
   it was always agnostic to the precision of its input) correctly bands
   floats that would have tied under 2-decimal rounding.

See test_columbo_cost_prefilter_integration.py's module docstring for how
the degenerate signal is proven against real, unmocked coverage data
despite full-precision durations making a genuine real-corpus tie
essentially unreproducible now (which is itself evidence option 1 works).
"""
from __future__ import annotations

import importlib.util
import os
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
# parse_duration_log -- full-precision duration collection (SABLE-cmar4.7)
# ---------------------------------------------------------------------------


def test_parse_duration_log_sums_phases_per_nodeid_at_full_precision(tmp_path):
    log = tmp_path / "durations.jsonl"
    log.write_text(
        '{"nodeid": "bin/test_foo.py::test_a", "duration": 0.0011234}\n'
        '{"nodeid": "bin/test_foo.py::test_a", "duration": 0.0002211}\n'
        '{"nodeid": "bin/test_foo.py::test_b", "duration": 0.0009999}\n'
    )
    durations = ccp.parse_duration_log(log)
    assert durations["bin/test_foo.py::test_a"] == pytest.approx(0.0013445)
    assert durations["bin/test_foo.py::test_b"] == pytest.approx(0.0009999)


def test_parse_duration_log_missing_file_reads_as_no_durations(tmp_path):
    """Consistent with parse_pytest_durations("") == {} -- a log that was
    never written (e.g. the inner run never started) is not a crash."""
    assert ccp.parse_duration_log(tmp_path / "never-written.jsonl") == {}


def test_duration_plugin_source_reads_env_var_by_name():
    """The plugin file is generated from a template at collection-import
    time (see run_python_suite_with_coverage) -- pin that the generated
    source references the real env var name, not a stale/typo'd one that
    would silently read nothing (os.environ[...] would KeyError instead,
    but a typo'd *different* real env var would not)."""
    assert f'os.environ["{ccp._DURATION_LOG_ENV_VAR}"]' in ccp._DURATION_PLUGIN_SOURCE


# ---------------------------------------------------------------------------
# run_python_suite_with_coverage -- ambient plugin isolation (SABLE-cmar4.6
# second revise). The real ci-verify gate red (run 29936760714) traced to
# pytest-testmon and pytest-cov contending for the same process-wide
# coverage.Coverage instance, which silently collapsed this function's
# stdout into something parse_pytest_durations matches nothing in (empty
# durations, empty coverage -- not a raised exception). This test guards
# the constructed argv directly, via a monkeypatched subprocess.run (no
# real subprocess spawned, consistent with this file's synthetic-data-only
# scope), rather than only the end-to-end integration suite, because the
# first fix attempt here used `-p no:testmon` -- confirmed by direct
# `--trace-config` experiment to be a silent no-op (pytest-testmon's
# entry-point name is `pytest-testmon`, not `testmon`) -- and a test that
# only ran the tool end-to-end without a hostile ambient plugin present
# would have stayed green on that no-op fix exactly as the original
# integration suite did before the real gate caught it.
# ---------------------------------------------------------------------------


def test_run_python_suite_disables_ambient_testmon_and_impact_by_working_name(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, cwd, capture_output, text, env):
        captured["args"] = args
        return type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(ccp.subprocess, "run", fake_run)
    ccp.run_python_suite_with_coverage(
        tmp_path, ["bin/test_foo.py"], ["bin"], tmp_path / ".coverage"
    )
    args = captured["args"]
    # The exact working spelling, not the no-op `no:testmon` typo the first
    # fix attempt used (see module note above and the run_python_suite_with_
    # coverage docstring for the --trace-config proof this typo is inert).
    assert "no:pytest-testmon" in args, (
        "must disable pytest-testmon by its real entry-point name -- "
        "'no:testmon' silently does nothing"
    )
    assert "no:testmon" not in args
    assert "no:impact" in args


# ---------------------------------------------------------------------------
# run_python_suite_with_coverage -- full-precision duration plugin wiring
# (SABLE-cmar4.7). The inner run must load the duration-capture plugin by
# module name and be able to import it, and must tell it where to write
# via the env var parse_duration_log later reads from.
# ---------------------------------------------------------------------------


def test_run_python_suite_wires_duration_plugin_into_argv_and_env(monkeypatch, tmp_path):
    captured = {}

    def fake_run(args, cwd, capture_output, text, env):
        captured["args"] = args
        captured["env"] = env
        return type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()

    monkeypatch.setattr(ccp.subprocess, "run", fake_run)
    ccp.run_python_suite_with_coverage(
        tmp_path, ["bin/test_foo.py"], ["bin"], tmp_path / ".coverage"
    )
    args = captured["args"]
    env = captured["env"]
    assert ccp._DURATION_PLUGIN_MODULE_NAME in args
    assert ccp._DURATION_LOG_ENV_VAR in env
    # the plugin module must actually be importable by the inner process
    plugin_dir = Path(env[ccp._DURATION_LOG_ENV_VAR]).parent
    assert (plugin_dir / f"{ccp._DURATION_PLUGIN_MODULE_NAME}.py").is_file()
    assert str(plugin_dir) in env["PYTHONPATH"].split(os.pathsep)


# ---------------------------------------------------------------------------
# run_python_suite_with_coverage -- loud failure on a non-tolerated inner
# returncode (SABLE-cmar4.6 third revise / SABLE-cmar4.8). Before this, a
# dead inner run (crash, interrupt, usage error, no tests collected) was
# indistinguishable from a legitimately quiet one: both produced ({}, {})
# because result.returncode/result.stderr were never inspected. See
# InnerPytestRunFailed and _INNER_RUN_OK_EXIT_CODES in
# columbo-cost-prefilter.py.
# ---------------------------------------------------------------------------


def _fake_run_with_returncode(returncode, stdout="", stderr=""):
    captured = {}

    def fake_run(args, cwd, capture_output, text, env):
        captured["args"] = args
        return type(
            "Result", (), {"stdout": stdout, "stderr": stderr, "returncode": returncode}
        )()

    return fake_run, captured


def test_run_python_suite_raises_loud_on_non_tolerated_returncode(monkeypatch, tmp_path):
    """Reproduces the exact class of failure that made runs 29936760714 and
    29940963862 undiagnosable: the inner pytest exits 3 (INTERNALERROR)
    with a real crash on stderr, and that must surface as a raised
    exception carrying the returncode and stderr -- never as a silent
    ({}, {})."""
    fake_run, _captured = _fake_run_with_returncode(
        returncode=3,
        stdout="internal error summary\n",
        stderr="INTERNALERROR> IndexError: list index out of range\n",
    )
    monkeypatch.setattr(ccp.subprocess, "run", fake_run)
    with pytest.raises(ccp.InnerPytestRunFailed) as excinfo:
        ccp.run_python_suite_with_coverage(
            tmp_path, ["bin/test_foo.py"], ["bin"], tmp_path / ".coverage"
        )
    assert excinfo.value.returncode == 3
    assert "INTERNALERROR" in str(excinfo.value)
    assert "IndexError" in str(excinfo.value)


@pytest.mark.parametrize("returncode", [0, 1])
def test_run_python_suite_tolerates_bare_pass_and_test_failures(monkeypatch, tmp_path, returncode):
    """Codes 0 (all passed) and 1 (some tests failed) both mean the inner
    run genuinely executed every test body and produced real durations/
    coverage -- these must NOT raise."""
    fake_run, _captured = _fake_run_with_returncode(returncode=returncode)
    monkeypatch.setattr(ccp.subprocess, "run", fake_run)
    # must not raise
    ccp.run_python_suite_with_coverage(
        tmp_path, ["bin/test_foo.py"], ["bin"], tmp_path / ".coverage"
    )


@pytest.mark.parametrize("returncode", [2, 4, 5])
def test_run_python_suite_raises_on_every_other_non_tolerated_code(monkeypatch, tmp_path, returncode):
    """Interrupted (2), usage error (4), and no-tests-collected (5) are all
    outcomes where the inner run did not produce trustworthy timing/
    coverage data -- each must raise, not just the returncode==3 case."""
    fake_run, _captured = _fake_run_with_returncode(returncode=returncode, stderr="boom")
    monkeypatch.setattr(ccp.subprocess, "run", fake_run)
    with pytest.raises(ccp.InnerPytestRunFailed) as excinfo:
        ccp.run_python_suite_with_coverage(
            tmp_path, ["bin/test_foo.py"], ["bin"], tmp_path / ".coverage"
        )
    assert excinfo.value.returncode == returncode


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


def test_rank_python_tests_distinguishes_durations_below_2_decimal_precision():
    """SABLE-cmar4.7: rank_python_tests was never the bug (it bands by
    float equality, which already handles arbitrary precision) -- the bug
    was upstream, in a duration SOURCE (pytest's --durations text) that
    rounded to 2 decimals before this function ever saw the data. Pin the
    property directly: two durations that would both round to "0.00s"
    (0.0011s and 0.0022s) but are genuinely distinct at full precision must
    land in different bands, so the slower one's non-overlapping coverage
    is correctly NOT credited to the faster one, and a slower test whose
    coverage IS a strict subset of a "tied-at-2-decimals" faster test's
    coverage is correctly identified as subsumed."""
    durations = {"t_faster": 0.0011, "t_slower_subset": 0.0022}
    coverage_map = {
        "t_faster": {_line("m.py", 1), _line("m.py", 2)},
        "t_slower_subset": {_line("m.py", 1)},
    }
    records = ccp.rank_python_tests(durations, coverage_map)
    by_id = {r["nodeid"]: r for r in records}
    assert by_id["t_faster"]["unique_count"] == 2
    assert by_id["t_slower_subset"]["unique_count"] == 0
    assert by_id["t_slower_subset"]["subsumed"] is True
    # negative control: at 2-decimal rounding both would read "0.00s" and
    # (per the equal-duration guarantee above) neither could ever subsume
    # the other -- confirming the distinction above depends on genuinely
    # reading the sub-hundredth-second difference, not an artifact of the
    # fixture's other values.
    rounded = {k: round(v, 2) for k, v in durations.items()}
    assert len(set(rounded.values())) == 1
    rounded_records = ccp.rank_python_tests(rounded, coverage_map)
    rounded_by_id = {r["nodeid"]: r for r in rounded_records}
    assert rounded_by_id["t_slower_subset"]["subsumed"] is False


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
# build_report -- degenerate_single_band signal (SABLE-cmar4.7). An empty
# pruning_candidates list must never be silently readable as "measured,
# nothing redundant" when it actually means "every test tied; nothing was
# comparable" -- see _degenerate_single_band's docstring.
# ---------------------------------------------------------------------------


def test_build_report_degenerate_signal_fires_on_all_tied_durations():
    """THE LIVE CASE (SABLE-cmar4.7's finding): every duration identical,
    overlapping coverage that would otherwise look prunable. Even though
    there IS coverage overlap here, the report must say the run could not
    measure subsumption, not that it measured zero."""
    python_records = ccp.rank_python_tests(
        {"t_a": 1.0, "t_b": 1.0, "t_c": 1.0},
        {
            "t_a": {_line("m.py", 1), _line("m.py", 2)},
            "t_b": {_line("m.py", 1)},
            "t_c": {_line("m.py", 2)},
        },
    )
    report = ccp.build_report(python_records, ccp.rank_shell_suites({}))
    assert report["python"]["degenerate_single_band"] is True
    assert report["python"]["pruning_candidates"] == []
    assert "note" in report["python"]
    assert "3" in report["python"]["note"]


def test_build_report_degenerate_signal_absent_with_real_spread():
    """NEGATIVE CONTROL: a corpus with a genuine duration spread must NOT
    emit the degenerate signal, even when it happens to have zero pruning
    candidates -- otherwise the signal becomes an always-on banner that
    stops distinguishing anything (the exact failure mode the negative
    control in the bead spec exists to catch)."""
    python_records = ccp.rank_python_tests(
        {"t_a": 0.1, "t_b": 0.2, "t_c": 0.3},
        {
            "t_a": {_line("m.py", 1)},
            "t_b": {_line("m.py", 2)},
            "t_c": {_line("m.py", 3)},
        },
    )
    report = ccp.build_report(python_records, ccp.rank_shell_suites({}))
    assert report["python"]["pruning_candidates"] == []
    assert report["python"]["degenerate_single_band"] is False
    assert "note" not in report["python"]


def test_build_report_degenerate_signal_absent_with_single_or_zero_tests():
    """A single test (or none) has nothing to tie WITH -- not the
    ambiguous state this signal exists to name."""
    one = ccp.build_report(
        ccp.rank_python_tests({"t_a": 1.0}, {"t_a": {_line("m.py", 1)}}),
        ccp.rank_shell_suites({}),
    )
    assert one["python"]["degenerate_single_band"] is False
    zero = ccp.build_report(ccp.rank_python_tests({}, {}), ccp.rank_shell_suites({}))
    assert zero["python"]["degenerate_single_band"] is False


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


def test_format_text_surfaces_degenerate_note():
    report = ccp.build_report(
        ccp.rank_python_tests(
            {"t_a": 1.0, "t_b": 1.0},
            {"t_a": {_line("m.py", 1)}, "t_b": {_line("m.py", 1)}},
        ),
        ccp.rank_shell_suites({}),
    )
    text = ccp.format_text(report)
    assert "python pruning candidates: 0" in text
    assert report["python"]["note"] in text


def test_format_text_omits_degenerate_note_with_real_spread():
    """NEGATIVE CONTROL for the formatter itself: a non-degenerate report
    (even with zero candidates) must not print a NOTE line at all."""
    report = ccp.build_report(
        ccp.rank_python_tests(
            {"t_a": 0.1, "t_b": 0.2},
            {"t_a": {_line("m.py", 1)}, "t_b": {_line("m.py", 2)}},
        ),
        ccp.rank_shell_suites({}),
    )
    text = ccp.format_text(report)
    assert "NOTE:" not in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
