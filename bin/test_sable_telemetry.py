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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
