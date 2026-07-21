#!/usr/bin/env python3
"""Unit tests for bin/sable_merge_report_lib.py (SABLE-jd5fj.7).

Pure-function tests only: percentile/latency math, the disjoint-promotion
subject parser, the semantic-break counting rule, the push->CI join, and the
success-metric bar. No subprocess, no real git/gh/bd -- those seams get the
real-git integration coverage in test_merge_report_integration.py.
"""
import math

import sable_merge_report_lib as rl


# --- percentile / latency_stats ----------------------------------------------

def test_percentile_empty_is_none():
    assert rl.percentile([], 50) is None


def test_percentile_single_value():
    assert rl.percentile([42.0], 50) == 42.0
    assert rl.percentile([42.0], 90) == 42.0


def test_percentile_median_odd_count():
    assert rl.percentile([1.0, 3.0, 2.0], 50) == 2.0


def test_percentile_median_even_count_interpolates():
    # sorted [1,2,3,4] -> median at rank 1.5 -> interpolate 2 and 3 -> 2.5
    assert rl.percentile([4.0, 1.0, 3.0, 2.0], 50) == 2.5


def test_percentile_p90_matches_known_value():
    # sorted 1..10, p90 index = 0.9*9 = 8.1 -> interpolate between values[8]=9, values[9]=10
    values = [float(i) for i in range(1, 11)]
    assert rl.percentile(values, 90) == 9.1


def test_percentile_rejects_out_of_range_pct():
    try:
        rl.percentile([1.0], 101)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_latency_stats_reports_n_median_p90():
    stats = rl.latency_stats([10.0, 20.0, 30.0, 40.0])
    assert stats["n"] == 4
    assert stats["median"] == 25.0


def test_latency_stats_empty():
    stats = rl.latency_stats([])
    assert stats == {"n": 0, "median": None, "p90": None}


# --- rule_of_three_bound ------------------------------------------------------

def test_rule_of_three_bound_matches_nueh3_headline():
    # the exact figure the nueh3 doc cites for 0/126
    assert math.isclose(rl.rule_of_three_bound(126), 3.0 / 126, rel_tol=1e-9)


def test_rule_of_three_bound_zero_n_is_none():
    assert rl.rule_of_three_bound(0) is None


# --- red_rate ------------------------------------------------------------------

def test_red_rate_counts_failures_and_excludes_cancelled():
    # 2 success, 1 failure, 1 cancelled -> cancelled excluded from denominator
    conclusions = ["success", "success", "failure", "cancelled"]
    result = rl.red_rate(conclusions)
    assert result == {"n": 3, "red": 1, "rate": 1.0 / 3.0}


def test_red_rate_unrecognized_conclusion_counts_as_red():
    # classify_conclusion's deliberate conservative default (sable_gate_classify_lib)
    result = rl.red_rate(["success", "some_unknown_conclusion"])
    assert result == {"n": 2, "red": 1, "rate": 0.5}


def test_red_rate_empty_is_none_rate():
    assert rl.red_rate([]) == {"n": 0, "red": 0, "rate": None}


# --- parse_promotion_subject ---------------------------------------------------

def test_parse_promotion_subject_with_bead():
    p = rl.parse_promotion_subject(
        "723e3e2", "2026-07-21T15:20:00+00:00",
        "ci-verify merge-preview: wk-disjoint-promote onto tmux-only (SABLE-jd5fj.4)")
    assert p is not None
    assert p.branch == "wk-disjoint-promote"
    assert p.bead == "SABLE-jd5fj.4"
    assert p.disjoint is False


def test_parse_promotion_subject_push_time_kick_has_no_bead():
    p = rl.parse_promotion_subject(
        "aeea30e", "2026-07-21T15:20:00+00:00",
        "ci-verify merge-preview: wk-tdd-evidence-key onto tmux-only (push-time kick)")
    assert p is not None
    assert p.bead is None
    assert p.disjoint is False


def test_parse_promotion_subject_disjoint_reverify_is_flagged():
    p = rl.parse_promotion_subject(
        "deadbee", "2026-07-21T15:20:00+00:00",
        "ci-verify merge-preview: wk-thing onto tmux-only (SABLE-abc12, disjoint re-verify)")
    assert p is not None
    assert p.bead == "SABLE-abc12"
    assert p.disjoint is True


def test_parse_promotion_subject_rejects_non_promotion_commits():
    assert rl.parse_promotion_subject("x", "y", "fix(provenance): unrelated commit") is None


# --- count_semantic_breaks (THE flagship counting rule) -----------------------

def _promo(sha, disjoint=True, bead="SABLE-x"):
    return rl.PromotionCommit(sha, "2026-07-21T00:00:00+00:00", "subject", "branch", bead, disjoint)


def test_semantic_break_detected_when_same_sha_reds_within_window():
    promo = _promo("aaa111")
    epochs = {"aaa111": 1000.0}
    runs = [rl.BaseRun("aaa111", 1500.0, "failure")]  # 500s later, within a 1h window
    result = rl.count_semantic_breaks([promo], runs, epochs, window_seconds=3600.0)
    assert result["disjoint_promotions"] == 1
    assert result["breaks"] == 1
    assert result["rate"] == 1.0
    assert result["rule_of_three_bound"] is None  # only reported when breaks == 0


def test_semantic_break_not_counted_outside_window():
    promo = _promo("aaa111")
    epochs = {"aaa111": 1000.0}
    runs = [rl.BaseRun("aaa111", 1000.0 + 7200.0, "failure")]  # 2h later, window is 1h
    result = rl.count_semantic_breaks([promo], runs, epochs, window_seconds=3600.0)
    assert result["breaks"] == 0
    assert result["rate"] == 0.0
    assert math.isclose(result["rule_of_three_bound"], 3.0)


def test_semantic_break_not_counted_before_the_promotion_landed():
    # a red run on the same SHA BEFORE it landed cannot be evidence about it
    promo = _promo("aaa111")
    epochs = {"aaa111": 1000.0}
    runs = [rl.BaseRun("aaa111", 500.0, "failure")]
    result = rl.count_semantic_breaks([promo], runs, epochs, window_seconds=3600.0)
    assert result["breaks"] == 0


def test_semantic_break_ignores_non_disjoint_promotions():
    ordinary = _promo("bbb222", disjoint=False)
    epochs = {"bbb222": 1000.0}
    runs = [rl.BaseRun("bbb222", 1200.0, "failure")]
    result = rl.count_semantic_breaks([ordinary], runs, epochs, window_seconds=3600.0)
    assert result["disjoint_promotions"] == 0
    assert result["breaks"] == 0
    assert result["rate"] is None


def test_semantic_break_ignores_runs_on_a_different_sha():
    promo = _promo("aaa111")
    epochs = {"aaa111": 1000.0}
    runs = [rl.BaseRun("ccc333", 1200.0, "failure")]
    result = rl.count_semantic_breaks([promo], runs, epochs, window_seconds=3600.0)
    assert result["breaks"] == 0


def test_semantic_break_green_run_is_not_a_break():
    promo = _promo("aaa111")
    epochs = {"aaa111": 1000.0}
    runs = [rl.BaseRun("aaa111", 1200.0, "success")]
    result = rl.count_semantic_breaks([promo], runs, epochs, window_seconds=3600.0)
    assert result["breaks"] == 0


def test_semantic_break_cancelled_run_is_not_a_break():
    # SABLE-sc24: cancelled is RETRY, never RED
    promo = _promo("aaa111")
    epochs = {"aaa111": 1000.0}
    runs = [rl.BaseRun("aaa111", 1200.0, "cancelled")]
    result = rl.count_semantic_breaks([promo], runs, epochs, window_seconds=3600.0)
    assert result["breaks"] == 0


def test_semantic_break_no_disjoint_promotions_at_all_reports_none_rate():
    result = rl.count_semantic_breaks([], [], {}, window_seconds=3600.0)
    assert result == {"disjoint_promotions": 0, "breaks": 0, "rate": None,
                      "rule_of_three_bound": None, "break_details": []}


# --- join_push_to_ci ------------------------------------------------------------

def test_join_matches_nearest_subsequent_run_within_gap():
    push = rl.PushEvent("wk-thing", 1000.0)
    run = rl.PreviewRun("ci-verify/wk-thing-abc1234", 1010.0, 1330.0, "success")
    latencies = rl.join_push_to_ci([push], [run], max_gap_seconds=120.0)
    assert latencies == [330.0]


def test_join_ignores_runs_outside_the_gap():
    push = rl.PushEvent("wk-thing", 1000.0)
    run = rl.PreviewRun("ci-verify/wk-thing-abc1234", 1200.0, 1500.0, "success")  # 200s gap > 120
    assert rl.join_push_to_ci([push], [run], max_gap_seconds=120.0) == []


def test_join_ignores_non_preview_runs():
    push = rl.PushEvent("wk-thing", 1000.0)
    run = rl.PreviewRun("tmux-only", 1010.0, 1330.0, "success")
    assert rl.join_push_to_ci([push], [run], max_gap_seconds=120.0) == []


def test_join_ignores_pending_runs():
    push = rl.PushEvent("wk-thing", 1000.0)
    run = rl.PreviewRun("ci-verify/wk-thing-abc1234", 1010.0, None, "success")
    assert rl.join_push_to_ci([push], [run], max_gap_seconds=120.0) == []


def test_join_never_double_books_one_run_to_two_pushes():
    push_a = rl.PushEvent("wk-a", 1000.0)
    push_b = rl.PushEvent("wk-b", 1005.0)
    only_run = rl.PreviewRun("ci-verify/only-abc1234", 1010.0, 1200.0, "success")
    latencies = rl.join_push_to_ci([push_a, push_b], [only_run], max_gap_seconds=120.0)
    assert len(latencies) == 1  # only one push gets the one available run


# --- evaluate_success (the jd5fj.7 acceptance bar) -----------------------------

def test_evaluate_success_meets_bar():
    result = rl.evaluate_success(current_median=300.0, baseline_median=1513.0,
                                 current_red_rate=0.05, baseline_red_rate=0.10)
    assert result["speedup"] > 5.0
    assert result["meets_bar"] is True


def test_evaluate_success_fails_on_worse_red_rate():
    result = rl.evaluate_success(current_median=300.0, baseline_median=1513.0,
                                 current_red_rate=0.20, baseline_red_rate=0.10)
    assert result["meets_bar"] is False
    assert result["red_rate_ok"] is False


def test_evaluate_success_fails_on_insufficient_speedup():
    result = rl.evaluate_success(current_median=1000.0, baseline_median=1513.0,
                                 current_red_rate=0.05, baseline_red_rate=0.10)
    assert result["meets_bar"] is False


def test_evaluate_success_undecidable_without_latency_data():
    result = rl.evaluate_success(None, 1513.0, 0.05, 0.10)
    assert result["meets_bar"] is None


def test_evaluate_success_undecidable_without_red_rate_data():
    result = rl.evaluate_success(300.0, 1513.0, None, 0.10)
    assert result["speedup"] is not None
    assert result["meets_bar"] is None


# --- parse_notify_log -----------------------------------------------------------

def test_parse_notify_log_extracts_confirmed_lines():
    text = (
        "2026-07-16T14:40:44Z pid=82327 name=tarzan branch=? | INVOKED cwd=/x cmd=[git push]\n"
        "2026-07-16T14:40:45Z pid=82327 name=tarzan branch=wk-prepush-timeout | "
        "CONFIRMED local=9c328861e9689dac0c920be3bcea018a7ddd1f9d "
        "remote=9c328861e9689dac0c920be3bcea018a7ddd1f9d attempts=1\n"
        "2026-07-16T14:40:47Z pid=82327 name=tarzan branch=wk-prepush-timeout | WORKER-LAND-MSG sent -> tarzan\n"
    )
    pushes = rl.parse_notify_log(text)
    assert len(pushes) == 1
    assert pushes[0].branch == "wk-prepush-timeout"


def test_parse_notify_log_respects_since_epoch():
    text = (
        "2026-07-16T14:40:45Z pid=1 name=t branch=early | CONFIRMED local=a remote=a attempts=1\n"
        "2026-07-20T14:40:45Z pid=1 name=t branch=late | CONFIRMED local=b remote=b attempts=1\n"
    )
    since_epoch = rl._iso_to_epoch("2026-07-18T00:00:00+00:00")
    pushes = rl.parse_notify_log(text, since_epoch=since_epoch)
    assert [p.branch for p in pushes] == ["late"]
