#!/usr/bin/env python3
"""Unit tests for sable-telemetry foundation (SABLE-8b41.1): CLI arg parsing,
the typed MetricRecord, and the single-source origin: taxonomy constant.
Source-adapter and aggregation logic land in later SABLE-8b41 children —
this covers only the skeleton this bead builds.
"""
import importlib.util
import sys
from datetime import timedelta, timezone
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sable_telemetry_lib as lib  # noqa: E402
import sable_telemetry_bd_source as bd_source  # noqa: E402
import sable_telemetry_git_source as git_source  # noqa: E402
import sable_telemetry_gh_source as gh_source  # noqa: E402

_LOADER = SourceFileLoader(
    "sable_telemetry_cli", str(Path(__file__).resolve().parent / "sable-telemetry")
)
_SPEC = importlib.util.spec_from_loader("sable_telemetry_cli", _LOADER)
cli = importlib.util.module_from_spec(_SPEC)
_LOADER.exec_module(cli)


def test_cli_parses_shift_and_trend_subcommands():
    parser = cli.build_parser()

    ns = parser.parse_args(["--shift", "--since", "2026-07-20", "--json"])
    assert ns.shift is True
    assert ns.since == "2026-07-20"
    assert ns.json is True
    assert ns.trend is None

    ns2 = parser.parse_args(["--trend", "30d"])
    assert ns2.trend == "30d"
    assert ns2.shift is False

    ns3 = parser.parse_args(["--trend"])
    assert ns3.trend == "7d"  # bare --trend defaults to a 7-day window


def test_origin_taxonomy_constant_matches_spec():
    assert lib.ORIGIN_LABELS == (
        "planned", "dogfood", "recurrence", "cross-fleet", "operator", "followup",
    )
    assert lib.origin_labels() == lib.ORIGIN_LABELS


def test_metric_record_rejects_unknown_origin():
    with pytest.raises(ValueError):
        lib.MetricRecord(name="closes_per_hour", value=6.0, unit="count/hr", origin="bogus")


def test_metric_record_to_dict_omits_absent_fields():
    m = lib.MetricRecord(name="closes_per_hour", value=6.0, unit="count/hr")
    assert lib.metric_to_dict(m) == {"name": "closes_per_hour", "value": 6.0, "unit": "count/hr"}


def test_build_shift_report_composes_real_metrics_from_bd_records():
    beads = [
        bd_source.BeadRecord(
            id="SABLE-a", status="closed",
            created_at="2026-07-20T09:00:00Z",
            closed_at="2026-07-20T10:00:00Z",
            started_at="2026-07-20T09:30:00Z",
        ),
        bd_source.BeadRecord(
            id="SABLE-b", status="open",
            created_at="2026-07-20T11:00:00Z",
            closed_at=None, started_at=None,
        ),
    ]
    merges = [git_source.MergeEvent(bead_id="SABLE-a", sha="aaa1111",
                                     committed_at="2026-07-20T10:05:00Z")]

    report = lib.build_shift_report(beads, merges, since="2026-07-20T00:00:00Z",
                                     tz=timezone.utc)

    by_name = {m.name: m for m in report.metrics}
    assert report.since == "2026-07-20T00:00:00Z"
    assert by_name["closes"].value == 1
    assert by_name["intake"].value == 2
    assert by_name["net_burn"].value == -1
    assert by_name["cycle_split_dispatched"].value == 1
    assert "had dispatch timestamps" in by_name["cycle_split_dispatched"].meta["note"]


def test_build_shift_report_with_no_activity_still_surfaces_denominator_note():
    # An empty corpus must still surface the zero-value metrics and the
    # denominator invariant explicitly -- never silently omitted, same
    # philosophy as the S4 gap-day backfill. Also exercises the bare
    # YYYY-MM-DD `since` fallback path (no time component).
    report = lib.build_shift_report(since="2026-07-20")

    by_name = {m.name: m for m in report.metrics}
    assert by_name["closes"].value == 0
    assert by_name["intake"].value == 0
    assert by_name["net_burn"].value == 0
    assert by_name["cycle_split_dispatched"].meta["note"] == (
        "0 of 0 closed beads had dispatch timestamps"
    )


def test_build_trend_report_composes_daily_series_and_total():
    beads = [
        bd_source.BeadRecord(id="SABLE-a", status="closed",
                              created_at="2026-07-19T09:00:00Z",
                              closed_at="2026-07-19T10:00:00Z", started_at=None),
        bd_source.BeadRecord(id="SABLE-b", status="closed",
                              created_at="2026-07-22T09:00:00Z",
                              closed_at="2026-07-22T10:00:00Z", started_at=None),
    ]

    report = lib.build_trend_report("7d", beads, tz=timezone.utc)

    assert report.window == "7d"
    daily = [m for m in report.metrics if m.name == "daily_net_burn"]
    assert [m.meta["date"] for m in daily] == [
        "2026-07-19", "2026-07-20", "2026-07-21", "2026-07-22",
    ]
    total = next(m for m in report.metrics if m.name == "total_net_burn")
    assert total.value == 0  # 2 closes - 2 intake across the whole window


def test_build_trend_report_truncates_series_to_requested_window():
    beads = [
        bd_source.BeadRecord(id=f"SABLE-day{n}", status="closed",
                              created_at=f"2026-07-{10 + n:02d}T09:00:00Z",
                              closed_at=f"2026-07-{10 + n:02d}T10:00:00Z",
                              started_at=None)
        for n in range(10)
    ]

    report = lib.build_trend_report("3d", beads, tz=timezone.utc)

    daily = [m for m in report.metrics if m.name == "daily_net_burn"]
    assert len(daily) == 3
    assert daily[-1].meta["date"] == "2026-07-19"  # the most recent 3 days only


def test_parse_trend_window_rejects_non_day_suffix():
    assert lib.parse_trend_window("7d") == 7
    with pytest.raises(ValueError):
        lib.parse_trend_window("7w")


def test_format_metrics_table_renders_name_value_unit_and_note():
    metrics = (
        lib.MetricRecord(name="closes", value=3.0, unit="count"),
        lib.MetricRecord(name="cycle_split_dispatched", value=2.0, unit="count",
                          meta={"note": "2 of 3 closed beads had dispatch timestamps"}),
    )
    table = lib.format_metrics_table(metrics)
    assert "closes" in table
    assert "3.0" in table
    assert "2 of 3 closed beads had dispatch timestamps" in table


def test_format_metrics_table_empty_is_explicit_not_blank():
    assert lib.format_metrics_table(()) == "(no metrics)"


def test_format_shift_report_human_includes_scope_and_table():
    report = lib.ShiftReport(since="2026-07-20", metrics=(
        lib.MetricRecord(name="closes", value=1.0, unit="count"),
    ))
    rendered = lib.format_shift_report_human(report)
    assert "--shift" in rendered
    assert "2026-07-20" in rendered
    assert "closes" in rendered


def test_format_trend_report_human_includes_window_and_table():
    report = lib.TrendReport(window="7d", metrics=(
        lib.MetricRecord(name="total_net_burn", value=2.0, unit="count"),
    ))
    rendered = lib.format_trend_report_human(report)
    assert "--trend" in rendered
    assert "7d" in rendered
    assert "total_net_burn" in rendered


def test_shift_report_to_dict_json_shape_matches_metrics():
    report = lib.build_shift_report(since="2026-07-20")
    payload = lib.shift_report_to_dict(report)
    assert set(payload.keys()) == {"since", "metrics"}
    assert all(set(m.keys()) >= {"name", "value", "unit"} for m in payload["metrics"])


def test_trend_report_to_dict_json_shape_matches_metrics():
    report = lib.build_trend_report("7d")
    payload = lib.trend_report_to_dict(report)
    assert set(payload.keys()) == {"window", "metrics"}


def test_build_shift_ledger_title_carries_prefix_and_scope():
    report = lib.ShiftReport(since="2026-07-20", metrics=())
    title = lib.build_shift_ledger_title(report)
    assert title.startswith(lib.SHIFT_TELEMETRY_TITLE_PREFIX)
    assert "2026-07-20" in title
    # Must never collide with the human "[SHIFT REPORT] ..." overlay-marker
    # regex -- these are a different artifact (a tool-filed ledger, not a
    # human-authored shift-report bead).
    assert lib.is_shift_report_bead(title) is False


def test_build_shift_ledger_description_includes_table_and_json():
    report = lib.ShiftReport(since="2026-07-20", metrics=(
        lib.MetricRecord(name="closes", value=1.0, unit="count"),
    ))
    description = lib.build_shift_ledger_description(report)
    assert "closes" in description
    assert '"since": "2026-07-20"' in description


def test_file_shift_ledger_bead_rejects_unknown_origin():
    report = lib.ShiftReport(since=None, metrics=())
    with pytest.raises(ValueError):
        lib.file_shift_ledger_bead(report, origin="bogus")


def test_bd_source_query_passes_all_flag():
    args = bd_source.build_bd_list_args()
    assert "--all" in args
    assert "--json" in args

    scoped_args = bd_source.build_bd_list_args(status="closed")
    assert "--all" in scoped_args
    assert "--status" in scoped_args
    assert scoped_args[scoped_args.index("--status") + 1] == "closed"


def test_bd_source_filters_cross_tracker_ids():
    raw = [
        {"id": "SABLE-abcd", "status": "open", "created_at": "2026-07-01T00:00:00Z"},
        {"id": "market-brief-package-0h8k", "status": "open",
         "created_at": "2026-07-01T00:00:00Z"},
    ]
    records = [
        bd_source._to_bead_record(r) for r in raw
        if not bd_source._is_cross_tracker_id(r["id"])
    ]
    assert [r.id for r in records] == ["SABLE-abcd"]


def test_bd_source_marks_missing_started_at_as_none():
    record = bd_source._to_bead_record({
        "id": "SABLE-xyz",
        "status": "closed",
        "created_at": "2026-07-01T00:00:00Z",
        "closed_at": "2026-07-02T00:00:00Z",
    })
    assert record.started_at is None
    assert record.closed_at == "2026-07-02T00:00:00Z"


def test_git_source_merge_preview_subject_regex_extracts_bead_id():
    assert git_source.extract_bead_id_from_subject(
        "ci-verify merge-preview: wk-8b41-3-git-source onto tmux-only (SABLE-8b41.3)"
    ) == "SABLE-8b41.3"

    # disjoint re-verify suffix is stripped, bead id still extracted
    assert git_source.extract_bead_id_from_subject(
        "ci-verify merge-preview: wk-broken onto tmux-only (SABLE-broken1, disjoint re-verify)"
    ) == "SABLE-broken1"

    # push-time kick is a real merge-preview event but carries no bead
    assert git_source.extract_bead_id_from_subject(
        "ci-verify merge-preview: wk-thing onto tmux-only (push-time kick)"
    ) is None


def test_git_source_ignores_non_merge_preview_commit_with_trailing_bead_ref():
    # A human commit that merely carries a trailing (SABLE-xxxx) reference is
    # NOT a merge-preview event and must not be mistaken for one, even though
    # a naive "look for a trailing paren" scan would match it.
    assert git_source.extract_bead_id_from_subject(
        "fix(auth): rotate expired token (SABLE-9182)"
    ) is None
    assert git_source.extract_bead_id_from_subject(
        "docs: update README (SABLE-1111)"
    ) is None


def test_gh_source_preview_ref_regex_extracts_bead_and_sha7():
    assert gh_source.parse_preview_ref(
        "ci-verify/SABLE-c008-16d94ed"
    ) == ("SABLE-c008", "16d94ed")

    # bead ids carry their own dashes/dots (e.g. epic-child ids) -- the
    # trailing 7-hex-char anchor, not the reverse, is what disambiguates.
    assert gh_source.parse_preview_ref(
        "ci-verify/SABLE-8b41.4-a1b2c3d"
    ) == ("SABLE-8b41.4", "a1b2c3d")

    # the redundant post-merge confirmation run's headBranch is plain
    # "tmux-only" -- not a preview ref at all, rejected outright.
    assert gh_source.parse_preview_ref("tmux-only") is None

    # not everything under ci-verify/ with a trailing dash-segment is a
    # valid preview ref -- the suffix must be exactly 7 hex chars.
    assert gh_source.parse_preview_ref("ci-verify/SABLE-c008-notasha") is None


def test_cycle_time_scoped_to_started_at_subset():
    # 3 closed beads: 2 dispatched (started_at present, both merged), 1
    # manager-closed (started_at absent). The architecture invariant: the
    # manager-closed bead must never contribute a CycleSplitEntry, even
    # though it is closed.
    beads = [
        bd_source.BeadRecord(
            id="SABLE-a", status="closed",
            created_at="2026-07-20T00:00:00Z",
            closed_at="2026-07-20T01:00:00Z",
            started_at="2026-07-20T00:30:00Z",
        ),
        bd_source.BeadRecord(
            id="SABLE-b", status="closed",
            created_at="2026-07-20T00:00:00Z",
            closed_at="2026-07-20T02:00:00Z",
            started_at="2026-07-20T01:30:00Z",
        ),
        bd_source.BeadRecord(
            id="SABLE-manager", status="closed",
            created_at="2026-07-20T00:00:00Z",
            closed_at="2026-07-20T03:00:00Z",
            started_at=None,
        ),
    ]
    merges = [
        git_source.MergeEvent(bead_id="SABLE-a", sha="aaa1111",
                              committed_at="2026-07-20T01:05:00Z"),
        git_source.MergeEvent(bead_id="SABLE-b", sha="bbb2222",
                              committed_at="2026-07-20T02:10:00Z"),
    ]

    report = lib.build_cycle_split_report(beads, merges)

    assert report.dispatched_count == 2
    assert report.closed_count == 3
    assert {e.bead_id for e in report.entries} == {"SABLE-a", "SABLE-b"}

    entry_a = next(e for e in report.entries if e.bead_id == "SABLE-a")
    assert entry_a.in_worker_seconds == 30 * 60
    assert entry_a.merge_queue_wait_seconds == 5 * 60


def test_cycle_time_excludes_manager_closed_beads_without_started_at():
    beads = [
        bd_source.BeadRecord(
            id="SABLE-manager", status="closed",
            created_at="2026-07-20T00:00:00Z",
            closed_at="2026-07-20T03:00:00Z",
            started_at=None,
        ),
    ]
    # Even a bead with a matching merge event must be excluded from entries
    # when it lacks started_at -- the scoping guard is on the bd record, not
    # on merge-event presence.
    merges = [
        git_source.MergeEvent(bead_id="SABLE-manager", sha="ccc3333",
                              committed_at="2026-07-20T03:05:00Z"),
    ]

    report = lib.build_cycle_split_report(beads, merges)

    assert report.dispatched_count == 0
    assert report.closed_count == 1
    assert report.entries == ()


def test_denominator_invariant_string_format():
    assert lib.denominator_invariant_string(3, 5) == (
        "3 of 5 closed beads had dispatch timestamps"
    )
    # the exact substring the fingerprint/architecture invariant anchors on
    assert "had dispatch timestamps" in lib.denominator_invariant_string(0, 0)

    beads = [
        bd_source.BeadRecord(
            id="SABLE-a", status="closed",
            created_at="2026-07-20T00:00:00Z",
            closed_at="2026-07-20T01:00:00Z",
            started_at="2026-07-20T00:30:00Z",
        ),
        bd_source.BeadRecord(
            id="SABLE-manager", status="closed",
            created_at="2026-07-20T00:00:00Z",
            closed_at="2026-07-20T03:00:00Z",
            started_at=None,
        ),
    ]
    report = lib.build_cycle_split_report(beads, [])
    assert report.denominator_note == "1 of 2 closed beads had dispatch timestamps"
    assert "had dispatch timestamps" in report.denominator_note


def test_day_bucket_boundary_assignment():
    assert lib.day_bucket("2026-07-21T23:59:59Z", tz=timezone.utc) == "2026-07-21"
    # exact midnight is the START of the new day, not the tail of the old one
    assert lib.day_bucket("2026-07-22T00:00:00Z", tz=timezone.utc) == "2026-07-22"
    assert lib.day_bucket("2026-07-22T00:00:01Z", tz=timezone.utc) == "2026-07-22"


def test_calendar_day_bucket_timezone_convention():
    # HOST-LOCAL bucketing convention (architecture.json "Shift boundary"
    # decision): the same instant buckets to a DIFFERENT calendar day
    # depending on the resolved zone. The explicit `tz` param stands in for
    # datetime.astimezone()'s real host-local resolution (the tz=None
    # production path) so this assertion is deterministic under any CI
    # runner's actual system timezone, while still proving the convention is
    # NOT simply "truncate the UTC timestamp's date".
    edt = timezone(timedelta(hours=-4))
    utc_ts = "2026-07-22T02:30:00Z"  # 10:30pm EDT the PREVIOUS calendar day

    assert lib.day_bucket(utc_ts, tz=timezone.utc) == "2026-07-22"
    assert lib.day_bucket(utc_ts, tz=edt) == "2026-07-21"


def test_net_burn_math():
    assert lib.net_burn(closes=10, intake=7) == 3
    assert lib.net_burn(closes=2, intake=5) == -3
    assert lib.net_burn(closes=0, intake=0) == 0


def test_net_burn_computation_closes_minus_intake():
    beads = [
        bd_source.BeadRecord(id="SABLE-a", status="closed",
                              created_at="2026-07-20T09:00:00Z",
                              closed_at="2026-07-20T10:00:00Z", started_at=None),
        bd_source.BeadRecord(id="SABLE-b", status="closed",
                              created_at="2026-07-19T09:00:00Z",
                              closed_at="2026-07-20T11:00:00Z", started_at=None),
        bd_source.BeadRecord(id="SABLE-c", status="open",
                              created_at="2026-07-20T12:00:00Z",
                              closed_at=None, started_at=None),
    ]

    series = lib.build_daily_burn_series(beads, tz=timezone.utc)
    by_date = {d.date: d for d in series}
    day = by_date["2026-07-20"]

    assert day.closes == 2  # SABLE-a and SABLE-b both closed that day
    assert day.intake == 2  # SABLE-a and SABLE-c created that day
    assert day.net_burn == 0


def test_trend_spine_backfills_zero_activity_gap_days():
    beads = [
        bd_source.BeadRecord(id="SABLE-a", status="closed",
                              created_at="2026-07-19T09:00:00Z",
                              closed_at="2026-07-19T10:00:00Z", started_at=None),
        bd_source.BeadRecord(id="SABLE-b", status="closed",
                              created_at="2026-07-22T09:00:00Z",
                              closed_at="2026-07-22T10:00:00Z", started_at=None),
    ]

    series = lib.build_daily_burn_series(beads, tz=timezone.utc)

    assert [d.date for d in series] == [
        "2026-07-19", "2026-07-20", "2026-07-21", "2026-07-22",
    ]
    gap_days = [d for d in series if d.date in ("2026-07-20", "2026-07-21")]
    assert gap_days  # both gap days present, not skipped
    assert all(d.closes == 0 and d.intake == 0 and d.net_burn == 0 for d in gap_days)


def test_is_shift_report_bead_matches_observed_title_variants():
    assert lib.is_shift_report_bead("[SHIFT REPORT] chuck 2026-07-21 night") is True
    assert lib.is_shift_report_bead("SHIFT REPORT lincoln (cockpit) 2026-07-21") is True
    assert lib.is_shift_report_bead("shift-report: tarzan 2026-07-17") is True
    assert lib.is_shift_report_bead("fix(auth): rotate expired token") is False
    assert lib.is_shift_report_bead(None) is False


def test_shift_report_beads_overlay_as_markers_not_boundaries():
    plain = bd_source.BeadRecord(id="SABLE-plain", status="closed",
                                  created_at="2026-07-20T09:00:00Z",
                                  closed_at="2026-07-20T10:00:00Z", started_at=None,
                                  title="ordinary bug fix")
    without_report = lib.build_daily_burn_series([plain], tz=timezone.utc)

    shift_report = bd_source.BeadRecord(id="SABLE-report", status="open",
                                         created_at="2026-07-20T20:00:00Z",
                                         closed_at=None, started_at=None,
                                         title="[SHIFT REPORT] chuck 2026-07-20")
    with_report = lib.build_daily_burn_series([plain, shift_report], tz=timezone.utc)

    day_without, day_with = without_report[0], with_report[0]
    # the day's boundary/identity and the plain bead's own numbers are
    # unaffected by the shift-report bead's presence -- only the marker
    # list changes, and intake moves exactly like it would for any bead.
    assert day_without.date == day_with.date == "2026-07-20"
    assert day_without.closes == day_with.closes == 1
    assert day_without.shift_report_ids == ()
    assert day_with.shift_report_ids == ("SABLE-report",)
    assert day_with.intake == day_without.intake + 1


def test_gh_source_dedups_post_merge_tmux_only_run():
    raw_runs = [
        {
            "databaseId": 29596385380,
            "headBranch": "ci-verify/SABLE-c008-16d94ed",
            "displayTitle": "ci-verify merge-preview: wk-reap-superseded onto tmux-only (SABLE-c008)",
        },
        {
            "databaseId": 29596766036,
            "headBranch": "tmux-only",
            "displayTitle": "ci-verify merge-preview: wk-reap-superseded onto tmux-only (SABLE-c008)",
        },
    ]

    selected = gh_source.select_preview_runs(raw_runs)

    assert [r["databaseId"] for r in selected] == [29596385380]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
