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


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
