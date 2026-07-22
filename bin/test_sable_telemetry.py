#!/usr/bin/env python3
"""Unit tests for sable-telemetry foundation (SABLE-8b41.1): CLI arg parsing,
the typed MetricRecord, and the single-source origin: taxonomy constant.
Source-adapter and aggregation logic land in later SABLE-8b41 children —
this covers only the skeleton this bead builds.
"""
import importlib.util
import sys
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


def test_build_shift_and_trend_reports_are_empty_stubs():
    shift = lib.build_shift_report(since="2026-07-20")
    assert shift.since == "2026-07-20"
    assert shift.metrics == ()

    trend = lib.build_trend_report("14d")
    assert trend.window == "14d"
    assert trend.metrics == ()


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
